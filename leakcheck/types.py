"""三层共用的数据结构。

位置约定
--------
所有 offset 都是 **归一化文本中的字符下标**。归一化会改变长度，
所以位置不能直接用来切原文；需要展示原文时，要么在原文上再数一次，
要么接受"归一化坐标"（本算法统一用后者，简单且一致）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Span:
    """一个命中片段：文档侧区间 + 净荷侧区间（左闭右开）。"""

    doc_start: int
    doc_end: int
    payload_start: int
    payload_end: int

    @property
    def length(self) -> int:
        return self.doc_end - self.doc_start

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Hit:
    """一次命中（一条净荷 × 一份敏感内容）。"""

    doc_id: str
    payload_id: str
    detector: str          # "text"（指纹支路）/ "short"（精确支路）
    score: float           # 0~1 的排序分（不是概率）
    longest_run: int       # 最长连续命中长度，最可信的证据
    matched: int           # 该文档所有命中片段的并集长度
    coverage_payload: float
    coverage_doc: float
    spans: list[Span] = field(default_factory=list)
    tag: str = ""          # 敏感内容的类别（合同/代码/…），仅用于展示
    path: str = ""         # 这份敏感内容的来源路径（若有），供决策层复用
    reason: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["spans"] = [s.to_dict() for s in self.spans]
        return d


@dataclass
class Stats:
    """索引规模，用于报告内存与成本。"""

    docs: int = 0
    chars: int = 0
    fingerprints: int = 0
    short_patterns: int = 0

    def to_dict(self) -> dict:
        return asdict(self)
