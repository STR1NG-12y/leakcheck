"""语义模糊筛选（可选模块，用向量模型）。

定位
----
它是**线索生成器**，不是证据层：
- 输入：一段外发内容 + 已建好的敏感资料块向量
- 输出：按余弦相似度排序的若干块，明确标注"仅供参考，不是泄露证据"
- 不做自动阻断，不参与 A/B 判定

三种后端
--------
1. ``sentence_transformers``（推荐，装了就能用 BGE / m3e 等）
2. ``onnxruntime`` + ``tokenizers``（更轻，需要自己准备 .onnx 模型）
3. ``hashing``（内置降级：字符二元组哈希，**没有语义能力**，仅用于演示/测试）

没有安装任何模型时，界面和命令行都会明确提示"未启用"。
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .config import Config
from .normalize import normalize

try:                                   # numpy 可选，有就快一点
    import numpy as _np
except Exception:                      # noqa: BLE001
    _np = None

_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]*|[\u4e00-\u9fff]")

#: 模型目录名（放在 exe / 项目根旁边）
MODEL_DIRNAME = "models"


def app_root() -> Path:
    """程序根目录：exe 所在目录，或包的上一级。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def model_dir() -> Path:
    return app_root() / MODEL_DIRNAME


def load_model_config() -> dict:
    """读 ``models/model_config.json``（可选）：pooling / query_prefix / max_length。"""
    cfg_path = model_dir() / "model_config.json"
    cfg: dict = {}
    if cfg_path.exists():
        try:
            import json
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:                          # noqa: BLE001
            cfg = {}
    return cfg


def default_pooling(model_path: Path) -> tuple[str, str]:
    """按模型名猜 pooling 和查询前缀。"""
    name = model_path.name.lower()
    if "bge" in name:
        # 我们的查询是"整句 vs 整块"，属于对称相似；实测加前缀反而降分
        # （短关键词检索才需要前缀，可用 model_config.json 打开）
        return "cls", ""
    if "m3e" in name or "gte" in name:
        return "cls", ""
    if "e5" in name:
        return "mean", "query: "
    if "qwen" in name:
        return "last", ""
    return "cls", ""


def find_local_models() -> dict[str, list[Path]]:
    """在 models/ 下找可用的本地模型。

    - ``*.onnx``  → onnx 后端
    - 含 ``config.json`` 的子目录 → sentence-transformers 本地目录
    """
    root = model_dir()
    out: dict[str, list[Path]] = {"onnx": [], "st": []}
    if not root.exists():
        return out
    out["onnx"] = sorted(root.glob("*.onnx"))
    out["st"] = sorted(d for d in root.iterdir() if (d / "config.json").exists())
    return out

CHUNK_CHARS = 256
CHUNK_OVERLAP = 64


# ---------------------------------------------------------------- 后端
class Embedder(Protocol):
    name: str
    semantic: bool                # 是否真的具备语义能力

    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]: ...


class HashingEmbedder:
    """内置降级：字符二元组哈希到固定维度。

    **没有语义能力**——它只能捕捉"字面用词重合"，用来在没有模型时
    跑通流程；界面上会标注"降级模式"。
    """

    name = "hashing（降级，无语义能力）"
    semantic = False

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = _WORD.findall(text)
        grams = tokens + [tokens[i] + tokens[i + 1] for i in range(len(tokens) - 1)]
        for gram in grams:
            h = hash(gram) & 0xFFFFFFFF
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class SentenceTransformerEmbedder:
    """用 sentence-transformers 加载本地模型（BGE / m3e / paraphrase-*）。"""

    semantic = True

    def __init__(self, model: str, query_prefix: str = "") -> None:
        self.query_prefix = query_prefix
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:                 # pragma: no cover - 取决于环境
            raise RuntimeError(
                "需要 sentence-transformers：pip install leakcheck[vector]") from exc
        self._model = SentenceTransformer(model)
        self.name = f"sentence-transformers: {model}"

    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]:   # pragma: no cover
        if is_query and self.query_prefix:
            texts = [self.query_prefix + t for t in texts]
        arr = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, row)) for row in arr]


class OnnxEmbedder:
    """用 onnxruntime + tokenizers 加载 .onnx 模型（推荐，无 torch 依赖）。

    BGE / M3E / GTE 都是 BERT 系：**取 [CLS] 向量 + L2 归一化**。
    查询侧还可以加一句任务前缀（BGE-zh 官方建议）。
    """

    semantic = True

    def __init__(self, model_path: str, tokenizer_path: str | None = None,
                 pooling: str = "cls", max_length: int = 512,
                 query_prefix: str = "") -> None:
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:                 # pragma: no cover
            raise RuntimeError(
                "需要 onnxruntime 和 tokenizers：pip install leakcheck[vector]") from exc

        path = Path(model_path)
        if not path.exists():
            raise RuntimeError(f"模型文件不存在：{path}")
        tok = Path(tokenizer_path) if tokenizer_path else path.with_suffix(".json")
        if not tok.exists():
            raise RuntimeError(f"缺少 tokenizer：{tok}（应与模型同名 .json）")

        self.pooling = pooling
        self.query_prefix = query_prefix
        self.model_name = path.name
        self._session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self._input_names = {i.name for i in self._session.get_inputs()}

        self._tokenizer = Tokenizer.from_file(str(tok))
        self._tokenizer.enable_truncation(max_length=max_length)
        vocab = self._tokenizer.get_vocab()
        self._pad_id = vocab.get("[PAD]", 0)
        self._cls_id = vocab.get("[CLS]", 101)
        self._sep_id = vocab.get("[SEP]", 102)
        self._tokenizer.enable_padding(pad_id=self._pad_id, pad_token="[PAD]")
        self.name = f"onnx: {path.name}（{pooling} pooling）"

    # ---------------------------------------------------------- 编码
    def _batch(self, texts: list[str]):
        import numpy as np

        encs = self._tokenizer.encode_batch(texts)
        ids = [list(e.ids) for e in encs]
        # 有些 tokenizer.json 没配后处理，手动补 [CLS]/[SEP]
        for i, row in enumerate(ids):
            if not row or row[0] != self._cls_id:
                row.insert(0, self._cls_id)
            if row[-1] != self._sep_id:
                row.append(self._sep_id)
            ids[i] = row

        max_len = max(len(r) for r in ids)
        input_ids = np.full((len(ids), max_len), self._pad_id, dtype=np.int64)
        attention = np.zeros((len(ids), max_len), dtype=np.int64)
        for i, row in enumerate(ids):
            input_ids[i, :len(row)] = row
            attention[i, :len(row)] = 1
        return input_ids, attention

    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]:   # pragma: no cover
        import numpy as np

        if is_query and self.query_prefix:
            texts = [self.query_prefix + t for t in texts]

        input_ids, attention = self._batch(texts)
        feeds = {"input_ids": input_ids, "attention_mask": attention}
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)

        hidden = self._session.run(None, feeds)[0]        # (batch, seq, dim)

        if self.pooling == "cls":
            vec = hidden[:, 0]
        elif self.pooling == "last":
            last = attention.sum(axis=1) - 1
            vec = hidden[np.arange(len(last)), last]
        else:                                             # mean
            mask = attention[..., None].astype(hidden.dtype)
            vec = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-6, None)

        norms = np.linalg.norm(vec, axis=1, keepdims=True)
        vec = vec / np.clip(norms, 1e-9, None)
        return [[float(x) for x in row] for row in vec]


def available_backends() -> dict[str, bool]:
    """探测当前环境能用的后端。"""
    state = {"sentence_transformers": False, "onnxruntime": False, "hashing": True}
    try:
        import sentence_transformers  # noqa: F401
        state["sentence_transformers"] = True
    except Exception:                                  # noqa: BLE001
        pass
    try:
        import onnxruntime  # noqa: F401
        state["onnxruntime"] = True
    except Exception:                                  # noqa: BLE001
        pass
    return state


def make_embedder(backend: str = "auto", model: str = "BAAI/bge-small-zh-v1.5",
                  onnx_model: str = "") -> Embedder:
    """按优先级挑一个可用的后端。"""
    state = available_backends()
    local = find_local_models()

    if backend in ("auto", "onnx") and state["onnxruntime"] and local["onnx"]:
        path = local["onnx"][0]
        cfg = load_model_config()
        pooling, prefix = default_pooling(path)
        return OnnxEmbedder(
            str(path),
            pooling=cfg.get("pooling", pooling),
            query_prefix=cfg.get("query_prefix", prefix),
            max_length=int(cfg.get("max_length", 512)),
        )
    if backend in ("auto", "sentence_transformers") and state["sentence_transformers"]:
        if local["st"]:
            return SentenceTransformerEmbedder(str(local["st"][0]))
        return SentenceTransformerEmbedder(model)
    if backend in ("auto", "onnx") and state["onnxruntime"] and onnx_model:
        return OnnxEmbedder(onnx_model)
    if backend == "hashing" or backend == "auto":
        return HashingEmbedder()
    raise RuntimeError(f"不可用的向量后端：{backend}")


# ---------------------------------------------------------------- 分块与检索
@dataclass
class VectorHit:
    doc_id: str
    score: float
    chunk: str
    chunk_index: int
    tag: str = ""
    path: str = ""

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id, "score": round(self.score, 4),
            "chunk_index": self.chunk_index, "tag": self.tag, "path": self.path,
            "chunk": self.chunk[:200],
        }


def chunk_text(text: str, size: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按固定窗口切块（带重叠），保证跨块的句子也能被某一块覆盖。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    step = max(size - overlap, 1)
    return [text[i:i + size] for i in range(0, len(text), step)
            if text[i:i + size].strip()]


@dataclass
class VectorScreen:
    """敏感资料的向量索引 + 相似度检索。"""

    cfg: Config = field(default_factory=Config)
    embedder: Embedder = field(default_factory=HashingEmbedder)
    min_score: float = 0.55

    _vecs: list[list[float]] = field(default_factory=list)
    _meta: list[VectorHit] = field(default_factory=list)

    @property
    def backend_name(self) -> str:
        return self.embedder.name

    @property
    def semantic(self) -> bool:
        return bool(getattr(self.embedder, "semantic", False))

    def __len__(self) -> int:
        return len(self._vecs)

    # ------------------------------------------------------------ 建索引
    def add(self, doc_id: str, text: str, tag: str = "", path: str = "") -> int:
        norm = normalize(text, self.cfg)
        chunks = chunk_text(norm)
        if not chunks:
            return 0
        vecs = self.embedder.encode(chunks)
        for i, (chunk, vec) in enumerate(zip(chunks, vecs)):
            self._vecs.append(vec)
            self._meta.append(VectorHit(doc_id=doc_id, score=0.0, chunk=chunk,
                                        chunk_index=i, tag=tag, path=path or doc_id))
        return len(chunks)

    # ------------------------------------------------------------ 检索
    def search(self, query: str, top_k: int = 5) -> list[VectorHit]:
        if not self._vecs or not query.strip():
            return []
        qv = self.embedder.encode([normalize(query, self.cfg)], is_query=True)[0]
        scores = [_cosine(qv, vec) for vec in self._vecs]
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)

        best_per_doc: dict[str, VectorHit] = {}
        for idx in order:
            score = scores[idx]
            if score < self.min_score:
                break
            meta = self._meta[idx]
            if meta.doc_id in best_per_doc:
                continue
            best_per_doc[meta.doc_id] = VectorHit(
                doc_id=meta.doc_id, score=score, chunk=meta.chunk,
                chunk_index=meta.chunk_index, tag=meta.tag, path=meta.path)
            if len(best_per_doc) >= top_k:
                break
        return list(best_per_doc.values())


def _cosine(a: list[float], b: list[float]) -> float:
    if _np is not None:
        return float(_np.dot(a, b))
    return sum(x * y for x, y in zip(a, b))
