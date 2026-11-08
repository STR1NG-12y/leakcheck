"""② 索引：敏感内容的两种登记方式 + 持久化。

对外部只暴露"按 id 加内容"，内部按长度自动分流：

    长度 >= k+w-1  →  拆分指纹，写倒排表 hash → [(doc_idx, pos)]
    长度 <  k+w-1  →  登记为精确模式串，进 AC 自动机

为什么倒排表是核心：它把"待上传文本和 N 份敏感内容逐个比"变成
"和一份哈希表比"，查询成本只与待上传文本长度有关，**与语料规模无关**。

内存
----
- 每个倒排项打包成一个 int（doc_idx<<32 | pos），比 tuple 省一半以上。
- 原文也保留一份，用于命中后逐字符校验和片段展示，这部分与语料等大。
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Iterable, Iterator

from .ahocorasick import AhoCorasick
from .config import Config
from .fingerprint import fingerprint

_POS_BITS = 32
_POS_MASK = (1 << _POS_BITS) - 1
_EMPTY: tuple[int, ...] = ()

_INDEX_VERSION = 2


def pack(doc_idx: int, pos: int) -> int:
    return (doc_idx << _POS_BITS) | pos


def unpack(value: int) -> tuple[int, int]:
    return value >> _POS_BITS, value & _POS_MASK


class Index:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.names: list[str] = []     # 内部下标 → 外部 id
        self.tags: list[str] = []
        self.paths: list[str] = []     # 来源路径（决策层要用它复核规则）
        self.texts: list[str] = []     # 规范文本，用于校验
        self.postings: dict[int, list[int]] = {}
        self._by_id: dict[str, int] = {}
        self._short: list[tuple[str, int]] = []  # (模式串, doc_idx)
        self._ac: AhoCorasick | None = None
        self.fp_count = 0

    # ------------------------------------------------------------ 写入
    def add(self, doc_id: str, text: str, tag: str = "", path: str = "") -> int:
        """登记一份敏感内容，返回写入的指纹数量（短内容返回 0）。"""
        if doc_id in self._by_id:
            raise ValueError(f"文档 id 重复: {doc_id}")
        if len(text) > self.cfg.max_text_chars:
            text = text[: self.cfg.max_text_chars]

        idx = len(self.names)
        self.names.append(doc_id)
        self.tags.append(tag)
        self.paths.append(path or doc_id)
        self.texts.append(text)
        self._by_id[doc_id] = idx

        # 长内容 → 指纹
        if len(text) >= self.cfg.guarantee_len:
            added = 0
            for pos, h in fingerprint(text, self.cfg):
                bucket = self.postings.get(h)
                if bucket is None:
                    self.postings[h] = [pack(idx, pos)]
                else:
                    bucket.append(pack(idx, pos))
                added += 1
            self.fp_count += added
            return added

        # 短内容 → 精确模式
        if len(text) >= self.cfg.short_min_len:
            self._short.append((text, idx))
            self._ac = None  # 标记需要重建
        return 0

    def add_many(self, items: Iterable[tuple[str, str, str]]) -> int:
        return sum(self.add(doc_id, text, tag) for doc_id, text, tag in items)

    # ------------------------------------------------------------ 查询
    def lookup(self, h: int) -> list[int]:
        """返回倒排项（共享引用，勿修改）。"""
        return self.postings.get(h, _EMPTY)  # type: ignore[return-value]

    def hits(self, fps: Iterable[tuple[int, int]]) -> Iterator[tuple[int, int, int]]:
        """净荷指纹流 → (doc_idx, 文档位置, 净荷位置)。"""
        postings = self.postings
        for payload_pos, h in fps:
            for packed in postings.get(h, _EMPTY):
                yield packed >> _POS_BITS, packed & _POS_MASK, payload_pos

    def short_find(self, text: str) -> list[tuple[int, int, int]]:
        """精确支路：返回 (doc_idx, start, end)。"""
        if not self._short:
            return []
        if self._ac is None:
            ac = AhoCorasick()
            for word, idx in self._short:
                ac.add(word, idx)
            ac.build()
            self._ac = ac
        return [(value, start, end) for start, end, _word, value in self._ac.find(text)]

    # ------------------------------------------------------------ 元信息
    @property
    def doc_count(self) -> int:
        return len(self.names)

    def text_of(self, idx: int) -> str:
        return self.texts[idx]

    def tag_of(self, idx: int) -> str:
        return self.tags[idx]

    def path_of(self, idx: int) -> str:
        return self.paths[idx]

    def name_of(self, idx: int) -> str:
        return self.names[idx]

    def stats(self) -> dict:
        chars = sum(len(t) for t in self.texts)
        return {
            "docs": len(self.names),
            "chars": chars,
            "fingerprints": self.fp_count,
            "short_patterns": len(self._short),
            "distinct_hashes": len(self.postings),
            # 粗估：打包 int ≈ 36B，dict 槽 ≈ 100B，原文即字符数
            "est_mem_mb": round((self.fp_count * 36 + len(self.postings) * 100 + chars)
                                / 1024 / 1024, 2),
        }

    # ------------------------------------------------------------ 持久化
    def save(self, path: str | Path) -> None:
        payload = {
            "version": _INDEX_VERSION,
            "cfg": self.cfg.to_dict(),
            "names": self.names,
            "tags": self.tags,
            "paths": self.paths,
            "texts": self.texts,
            "postings": self.postings,
            "short": self._short,
        }
        Path(path).write_bytes(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))

    @classmethod
    def load(cls, path: str | Path) -> "Index":
        payload = pickle.loads(Path(path).read_bytes())
        if payload.get("version") != _INDEX_VERSION:
            raise ValueError("索引版本不匹配（旧索引缺少 path 字段），请重建")
        cfg = _config_from(payload["cfg"])
        idx = cls(cfg)
        idx.names = payload["names"]
        idx.tags = payload["tags"]
        idx.paths = payload["paths"]
        idx.texts = payload["texts"]
        idx.postings = payload["postings"]
        idx._short = payload["short"]
        idx._by_id = {name: i for i, name in enumerate(idx.names)}
        idx.fp_count = sum(len(b) for b in idx.postings.values())
        return idx


def _config_from(data: dict) -> Config:
    fields = ("k", "w", "min_run", "min_hits", "coverage", "max_gap_mult",
              "short_min_len", "short_min_run", "nfkc", "collapse_ws", "lower",
              "max_text_chars")
    return Config(**{f: data[f] for f in fields if f in data})
