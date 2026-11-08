"""③.2-3.3 对角线聚合：本项目最关键的一步。

核心观察
--------
如果净荷里的某段是从文档位置 p 开始拷的，那么每次命中都满足

    文档位置 − 净荷位置 = p − 0 = 常数

也就是说，**真拷贝的命中点全落在同一条斜率为 1 的直线上**；
而哈希碰撞、常见套话会散落在不同斜线上。

于是"是不是抄的"变成一个几何问题：同一条线上有没有够长的一段连续点。

实现按 (doc_idx, delta) 分桶，桶内排序后把间隔不大的点合并成 run。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class Cluster:
    """一条对角线上的连续命中段。run 的长度 ≈ 实际被抄的字符数。"""

    doc_idx: int
    delta: int
    payload_start: int
    payload_end: int
    doc_start: int
    doc_end: int
    hits: int
    positions: list[tuple[int, int]] = field(default_factory=list)  # (净荷位置, 文档位置)

    @property
    def length(self) -> int:
        return self.payload_end - self.payload_start


class RunAccumulator:
    """支持分批喂入（流式场景也适用）。"""

    def __init__(self, k: int, max_gap: int, max_hits: int = 4_000_000) -> None:
        self.k = k
        self.max_gap = max_gap
        self.max_hits = max_hits
        self.buckets: dict[tuple[int, int], list[tuple[int, int]]] = {}
        self.count = 0
        self.truncated = False

    def add(self, doc_idx: int, doc_pos: int, payload_pos: int) -> None:
        if self.truncated:
            return
        key = (doc_idx, doc_pos - payload_pos)
        bucket = self.buckets.get(key)
        if bucket is None:
            self.buckets[key] = [(payload_pos, doc_pos)]
        else:
            bucket.append((payload_pos, doc_pos))
        self.count += 1
        if self.count >= self.max_hits:  # 防御重复文本导致的爆炸
            self.truncated = True

    def extend(self, hits: Iterable[tuple[int, int, int]]) -> None:
        for doc_idx, doc_pos, payload_pos in hits:
            self.add(doc_idx, doc_pos, payload_pos)

    def finish(self) -> list[Cluster]:
        clusters: list[Cluster] = []
        for (doc_idx, delta), pairs in self.buckets.items():
            pairs.sort()
            start = 0
            for i in range(1, len(pairs) + 1):
                if i == len(pairs) or pairs[i][0] - pairs[i - 1][0] > self.max_gap:
                    run = pairs[start:i]
                    p_start, p_end = run[0][0], run[-1][0] + self.k
                    if p_end > p_start:
                        clusters.append(Cluster(
                            doc_idx=doc_idx,
                            delta=delta,
                            payload_start=p_start,
                            payload_end=p_end,
                            doc_start=run[0][1],
                            doc_end=run[-1][1] + self.k,
                            hits=len(run),
                            positions=run,
                        ))
                    start = i
        clusters.sort(key=lambda c: c.length, reverse=True)
        return clusters


def collect_runs(hits: Iterable[tuple[int, int, int]], k: int, max_gap: int,
                 max_hits: int = 4_000_000) -> list[Cluster]:
    """一次性版本（非流式场景的便捷入口）。"""
    acc = RunAccumulator(k, max_gap, max_hits)
    acc.extend(hits)
    return acc.finish()
