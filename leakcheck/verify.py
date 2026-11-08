"""③.4 校验：拒绝哈希碰撞，并把区间精修到精确边界。

两道保险
--------
1. 指纹是 64 位哈希，理论会碰撞；而且"某些 k-gram 相同"不等于"整段相同"。
   所以报警前必须回原文做**逐字符比对**，锚点对不上就整段丢弃。
2. winnowing 给出的 run 是保守估计，两端通常还能再延伸几十个字符，
   这里顺手把它扩到最大一致区间，让告警里的片段更像"人看到的原文"。
"""

from __future__ import annotations

from .align import Cluster
from .types import Span

MAX_EXTEND = 4096  # 单侧最大延伸，防止在超长重复文本上退化成 O(n^2)


def _match_at(doc: str, payload: str, doc_pos: int, payload_pos: int, k: int = 8) -> bool:
    if doc_pos < 0 or payload_pos < 0:
        return False
    if doc_pos + k > len(doc) or payload_pos + k > len(payload):
        return False
    return doc[doc_pos:doc_pos + k] == payload[payload_pos:payload_pos + k]


def expand(doc: str, payload: str, doc_pos: int, payload_pos: int, k: int) -> Span | None:
    """以 (doc_pos, payload_pos) 为锚点，向两侧扩到最大一致区间。"""
    if not _match_at(doc, payload, doc_pos, payload_pos, k):
        return None
    left = 0
    limit = min(doc_pos, payload_pos, MAX_EXTEND)
    while left < limit and doc[doc_pos - left - 1] == payload[payload_pos - left - 1]:
        left += 1
    right = k
    max_right = min(len(doc) - doc_pos, len(payload) - payload_pos, k + MAX_EXTEND)
    while right < max_right and doc[doc_pos + right] == payload[payload_pos + right]:
        right += 1
    return Span(doc_start=doc_pos - left, doc_end=doc_pos + right,
                payload_start=payload_pos - left, payload_end=payload_pos + right)


def verify_cluster(cluster: Cluster, doc: str, payload: str, k: int) -> Span | None:
    """校验一个 run：取首/中/尾三个锚点，全部逐字符一致才算真命中。"""
    if not cluster.positions:
        return None
    picks = {0, len(cluster.positions) // 2, len(cluster.positions) - 1}
    spans: list[Span] = []
    for i in sorted(picks):
        p_pos, d_pos = cluster.positions[i]
        span = expand(doc, payload, d_pos, p_pos, k)
        if span is None:
            return None  # 锚点都不一致 → 判定为哈希碰撞
        spans.append(span)
    return max(spans, key=lambda s: s.length)
