"""③.5 打分与判定：把"证据"变成"结论"。

刻意不用相似度阈值，而是两条**可解释**的规则取或：

    长片段规则：有一段连续命中 >= min_run 字，且指纹数 >= min_hits
                → 对应"整段粘贴"，最可靠
    高覆盖规则：净荷里被命中比例 >= coverage，且最长段不算短
                → 对应"只发出去一句话，但几乎全是敏感内容"

两条都不满足就放行。这样每个告警都能说清"为什么报"，也能被人工复核。

``score`` 只是排序分，不是概率，不能写成"置信度 95%"。
"""

from __future__ import annotations

from typing import Iterable

from .align import Cluster
from .config import Config
from .index import Index
from .types import Hit, Span

MIN_COVERAGE_PAYLOAD = 24  # 净荷短于此长度时，覆盖率没有统计意义


def merge_spans(spans: list[Span]) -> list[Span]:
    """合并重叠片段（同一份内容被多次摘抄时会出现）。"""
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: (s.doc_start, s.payload_start))
    merged = [ordered[0]]
    for s in ordered[1:]:
        last = merged[-1]
        if s.doc_start <= last.doc_end and s.payload_start <= last.payload_end:
            merged[-1] = Span(
                doc_start=min(last.doc_start, s.doc_start),
                doc_end=max(last.doc_end, s.doc_end),
                payload_start=min(last.payload_start, s.payload_start),
                payload_end=max(last.payload_end, s.payload_end),
            )
        else:
            merged.append(s)
    return merged


def union_len(ranges: Iterable[tuple[int, int]]) -> int:
    total, cur_start, cur_end = 0, None, None
    for start, end in sorted(ranges):
        if cur_end is None:
            cur_start, cur_end = start, end
        elif start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    if cur_start is not None and cur_end is not None:
        total += cur_end - cur_start
    return total


def build_hits(payload_id: str, payload_text: str, clusters: list[Cluster],
               index: Index, cfg: Config) -> list[Hit]:
    """run 列表 → 按文档聚合的 Hit 列表（已过滤掉不达标的）。"""
    from .verify import verify_cluster

    by_doc: dict[int, list[Cluster]] = {}
    for cluster in clusters:
        by_doc.setdefault(cluster.doc_idx, []).append(cluster)

    payload_len = max(len(payload_text), 1)
    hits: list[Hit] = []

    for doc_idx, group in by_doc.items():
        doc_text = index.text_of(doc_idx)
        spans: list[Span] = []
        total_fp = 0
        for cluster in group:
            span = verify_cluster(cluster, doc_text, payload_text, cfg.k)
            if span is None:
                continue
            spans.append(span)
            total_fp += cluster.hits
        if not spans:
            continue

        merged = merge_spans(spans)
        matched = union_len((s.doc_start, s.doc_end) for s in merged)
        cov_payload = union_len((s.payload_start, s.payload_end) for s in merged) / payload_len
        cov_doc = matched / max(len(doc_text), 1)
        longest = max(s.length for s in merged)

        long_rule = longest >= cfg.min_run and total_fp >= cfg.min_hits
        cov_rule = (cov_payload >= cfg.coverage
                    and longest >= cfg.min_run / 2
                    and len(payload_text) >= MIN_COVERAGE_PAYLOAD)
        if not (long_rule or cov_rule):
            continue

        score = min(1.0, 0.5 * cov_payload + 0.5 * min(1.0, longest / (4 * cfg.min_run)))
        reasons = [f"最长连续命中 {longest} 字", f"{total_fp} 个指纹"]
        if cov_rule and not long_rule:
            reasons.append(f"覆盖率 {cov_payload:.0%}")
        hits.append(Hit(
            doc_id=index.name_of(doc_idx),
            payload_id=payload_id,
            detector="text",
            score=round(score, 3),
            longest_run=longest,
            matched=matched,
            coverage_payload=round(cov_payload, 3),
            coverage_doc=round(cov_doc, 3),
            spans=merged,
            tag=index.tag_of(doc_idx),
            path=index.path_of(doc_idx),
            reason="，".join(reasons),
        ))

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits
