"""两级级联门面：一阶逐字 → 二阶骨架。

> v3：删去原来的三阶（BM25 + SW）。理由是实测它在 30 条用例上"正例零贡献、
> 负例八成噪声"，且 BM25 分数在负例与边界之间完全重叠，没有可分阈值。
> 需要"语义模糊筛选"时请用 ``vector_screen``（向量模型，单独成板块）。

对外只有 ``add()`` 和 ``scan()``。
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .matcher import Matcher
from .normalize import normalize
from .index import Index
from .skeleton import Skeleton, SkeletonIndex
from .types import Hit

#: 两阶索引的格式版本（与单层 Index 的版本分开，避免混用）
_CASCADE_VERSION = 1

LEVEL_DESC = {
    "A": "逐字命中",
    "B": "骨架命中",
    "C": "语义线索（向量筛选，非证据）",
    "D": "跨会话碎片",
    "E": "路径规则缺口",
}


@dataclass
class Finding:
    level: str                 # A | B
    doc_id: str
    detector: str              # text | skeleton | short
    payload_span: tuple[int, int]
    doc_span: tuple[int, int]
    length: int
    score: float
    evidence: list[str] = field(default_factory=list)
    tag: str = ""
    path: str = ""

    def to_dict(self) -> dict:
        return {
            "level": self.level, "doc_id": self.doc_id, "detector": self.detector,
            "payload_span": list(self.payload_span), "doc_span": list(self.doc_span),
            "length": self.length, "score": round(self.score, 3),
            "tag": self.tag, "path": self.path, "evidence": list(self.evidence),
        }


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    partial: bool = False

    @property
    def top_level(self) -> str:
        order = "ABCDE"
        levels = [f.level for f in self.findings]
        return min(levels, key=order.index) if levels else ""


class Cascade:
    """一阶（字符指纹）+ 二阶（骨架）。两层都有"可证明"的命中依据。"""

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        self.matcher = Matcher(self.cfg)
        self.skeleton = SkeletonIndex(self.cfg)

    # ------------------------------------------------------------ 建索引
    def add(self, doc_id: str, text: str, tag: str = "", path: str = "") -> None:
        self.matcher.add(doc_id, text, tag, path)
        self.skeleton.add(doc_id, text, tag, path)

    def add_many(self, items) -> None:
        for doc_id, tag, text in items:
            self.add(doc_id, text, tag)

    def __len__(self) -> int:
        return self.matcher.index.doc_count

    # ------------------------------------------------------------ 检测
    def scan(self, text: str, payload_id: str = "") -> ScanResult:
        result = ScanResult()
        if not text or self.matcher.index.doc_count == 0:
            return result

        norm = normalize(text, self.cfg)
        if len(norm) > self.cfg.max_text_chars:
            norm = norm[: self.cfg.max_text_chars]
        if not norm:
            return result

        # ---- 一阶：逐字 ----
        hits = self.matcher.match(norm, payload_id)
        result.warnings.extend(self.matcher.last_warnings)
        if hits:
            result.findings.extend(self._from_hits(hits, "A", "逐字命中（23 字有数学保证）"))
            return result

        # ---- 二阶：骨架 ----
        sk_hits = self.skeleton.scan(norm)
        if sk_hits:
            result.findings.extend(
                self._from_hits(sk_hits, "B", "骨架命中（同义改写/语序调整）"))
        return result

    # ------------------------------------------------------------ 持久化
    def save(self, path: str | Path) -> None:
        """把两层索引一起存盘，之后 ④ 两阶扫描可以直接 load，不用重建。"""
        payload = {
            "version": _CASCADE_VERSION,
            "kind": "cascade",
            "matcher": self.matcher.index.to_payload(),
            "skeleton": {
                "index": self.skeleton.index.to_payload(),
                "skeletons": [(s.text, s.spans) for s in self.skeleton.skeletons],
                "orig": self.skeleton.orig,
            },
        }
        Path(path).write_bytes(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))

    @classmethod
    def load(cls, path: str | Path) -> "Cascade":
        payload = pickle.loads(Path(path).read_bytes())
        if payload.get("kind") != "cascade":
            raise ValueError("这不是两阶索引（可能是旧版单层索引），请用 ① 重建")
        if payload.get("version") != _CASCADE_VERSION:
            raise ValueError("索引版本不匹配，请重建")

        matcher_index = Index.from_payload(payload["matcher"])
        casc = cls(matcher_index.cfg)
        casc.matcher.index = matcher_index

        sk = payload["skeleton"]
        sk_index = Index.from_payload(sk["index"])
        casc.skeleton.index = sk_index
        casc.skeleton.skeletons = [Skeleton(text=text, spans=list(spans))
                                   for text, spans in sk["skeletons"]]
        casc.skeleton.orig = list(sk["orig"])
        return casc

    # ------------------------------------------------------------ 辅助
    def _from_hits(self, hits: list[Hit], level: str, evidence: str) -> list[Finding]:
        out: list[Finding] = []
        for h in hits:
            span = h.spans[0] if h.spans else None
            out.append(Finding(
                level=level,
                doc_id=h.doc_id,
                detector=h.detector,
                payload_span=(span.payload_start, span.payload_end) if span else (0, 0),
                doc_span=(span.doc_start, span.doc_end) if span else (0, 0),
                length=h.longest_run,
                score=h.score,
                evidence=[evidence, h.reason] if h.reason else [evidence],
                tag=h.tag,
                path=h.path,
            ))
        return out
