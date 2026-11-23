"""③.2-3.3 对角线聚合：本项目最关键的一步。

核心观察
--------
如果净荷里的某段是从文档位置 p 开始拷的，那么每次命中都满足

    文档位置 − 净荷位置 = p − 0 = 常数

也就是说，**真拷贝的命中点全落在同一条斜率为 1 的直线上**；
而哈希碰撞、常见套话会散落在不同斜线上。

于是"是不是抄的"变成一个几何问题：同一条线上有没有够长的一段连续点。

实现按 (doc_idx, delta) 分桶，桶内排序后把间隔不大的点合并成 run。

限流（本版新增）
----------------
重复文本（模板条款、表格）会让同一段内容产生 O(n²) 个候选点。本版用三条限制把它压回线性：

1. **每个净荷位置最多开 max_deltas_per_pos 个新桶**（老桶不占配额）
2. **每个桶只保留前 N + 后 N 个点**（N = bucket_keep），同时单独记录首尾，
   所以 ``longest_run`` 不会被截断影响
3. **被丢弃的点必须计数**，结果里能看到 ``dropped``，不再静默降级

注意：这里**不做**"同一 k-gram 只留一个位置"（Encoplot 式去重）——
实测那样会把重复文本的对角线打断，导致漏检。
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
    capped: bool = False          # 该桶点数超限，区间用首尾推算

    @property
    def length(self) -> int:
        return self.payload_end - self.payload_start


class _Bucket:
    """一个 (doc, delta) 桶：保留样本点 + 精确的首尾。"""

    __slots__ = ("samples", "min_p", "max_p", "min_d", "max_d", "count", "capped")

    def __init__(self, p: int, d: int) -> None:
        self.samples: list[tuple[int, int]] = [(p, d)]
        self.min_p = self.max_p = p
        self.min_d = self.max_d = d
        self.count = 1
        self.capped = False

    def add(self, p: int, d: int, keep: int) -> None:
        self.count += 1
        if len(self.samples) < 2 * keep:
            # 未超限：直接留样本，首尾在 cap 时再算，省掉每次四次比较
            self.samples.append((p, d))
            return

        if not self.capped:
            self.capped = True
            self.min_p = min(s[0] for s in self.samples)
            self.max_p = max(s[0] for s in self.samples)
            self.min_d = min(s[1] for s in self.samples)
            self.max_d = max(s[1] for s in self.samples)

        if p < self.min_p:
            self.min_p = p
        if p > self.max_p:
            self.max_p = p
        if d < self.min_d:
            self.min_d = d
        if d > self.max_d:
            self.max_d = d
        # 首 keep 个保持不动，尾部用环形覆盖保留"最近的 keep 个"
        self.samples[keep + (self.count % keep)] = (p, d)


class RunAccumulator:
    """支持分批喂入（流式场景也适用）。"""

    def __init__(self, k: int, max_gap: int, max_hits: int = 4_000_000,
                 max_deltas_per_pos: int = 8, bucket_keep: int = 128) -> None:
        self.k = k
        self.max_gap = max_gap
        self.max_hits = max_hits
        self.max_deltas_per_pos = max_deltas_per_pos
        self.bucket_keep = bucket_keep
        self.buckets: dict[tuple[int, int], _Bucket] = {}
        self.per_pos: dict[int, int] = {}     # 净荷位置 → 已开的新桶数
        self.count = 0                        # 成功入桶的点数
        self.total = 0                        # 收到的点数
        self.dropped = 0                      # 因配额被丢弃的点数
        self.truncated = False                # 触发全局硬上限

    # ------------------------------------------------------------ 写入
    def add(self, doc_idx: int, doc_pos: int, payload_pos: int) -> None:
        self.total += 1
        if self.truncated:
            self.dropped += 1
            return
        if self.count >= self.max_hits:       # 兜底保险，正常情况不会触发
            self.truncated = True
            self.dropped += 1
            return

        key = (doc_idx, doc_pos - payload_pos)
        bucket = self.buckets.get(key)

        if bucket is None:
            used = self.per_pos.get(payload_pos, 0)
            if used >= self.max_deltas_per_pos:
                self.dropped += 1
                return
            self.per_pos[payload_pos] = used + 1
            self.buckets[key] = _Bucket(payload_pos, doc_pos)
        else:
            bucket.add(payload_pos, doc_pos, self.bucket_keep)
        self.count += 1

    def extend(self, hits: Iterable[tuple[int, int, int]]) -> None:
        for doc_idx, doc_pos, payload_pos in hits:
            self.add(doc_idx, doc_pos, payload_pos)

    # ------------------------------------------------------------ 收尾
    def finish(self) -> list[Cluster]:
        clusters: list[Cluster] = []
        for (doc_idx, delta), bucket in self.buckets.items():
            if bucket.capped:
                # 点数超限：用首尾推区间，长度精确，中间细节略去
                p_start, p_end = bucket.min_p, bucket.max_p + self.k
                clusters.append(Cluster(
                    doc_idx=doc_idx, delta=delta,
                    payload_start=p_start, payload_end=p_end,
                    doc_start=bucket.min_d, doc_end=bucket.max_d + self.k,
                    hits=bucket.count,
                    positions=sorted(bucket.samples),
                    capped=True,
                ))
                continue

            pairs = sorted(bucket.samples)
            start = 0
            for i in range(1, len(pairs) + 1):
                if i == len(pairs) or pairs[i][0] - pairs[i - 1][0] > self.max_gap:
                    run = pairs[start:i]
                    p_start, p_end = run[0][0], run[-1][0] + self.k
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

    # ------------------------------------------------------------ 诊断
    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.truncated:
            out.append(f"候选点超过硬上限 {self.max_hits}，结果可能不完整")
        if self.dropped and not self.truncated:
            out.append(f"重复内容导致 {self.dropped} 个候选点被限流丢弃（结论仍有效）")
        return out


def collect_runs(hits: Iterable[tuple[int, int, int]], k: int, max_gap: int,
                 max_hits: int = 4_000_000,
                 max_deltas_per_pos: int = 8, bucket_keep: int = 128) -> list[Cluster]:
    """一次性版本（非流式场景的便捷入口）。"""
    acc = RunAccumulator(k, max_gap, max_hits, max_deltas_per_pos, bucket_keep)
    acc.extend(hits)
    return acc.finish()
