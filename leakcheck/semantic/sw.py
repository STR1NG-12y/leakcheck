"""Banded Smith-Waterman：局部序列对齐，用于三阶的"局部验证"。

语义召回只能说明"像"，SW 对齐出多长的公共片段才能说明"抄了多长"。
全量 DP 是 O(mn)，这里只算主对角线附近带宽 band 内的格子，复杂度 O((m+n)·band)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SWResult:
    score: int
    length: int          # 对齐出来的长度（不含 gap）
    a_start: int
    a_end: int
    b_start: int
    b_end: int


def sw_align(a: str, b: str, band: int = 32, match: int = 2,
             mismatch: int = -1, gap: int = -1) -> SWResult:
    """返回最佳局部对齐。``a`` 是查询，``b`` 是文档窗口。"""
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return SWResult(0, 0, 0, 0, 0, 0)

    neg = -10 ** 9
    prev = [0] * (m + 1)
    best = SWResult(0, 0, 0, 0, 0, 0)

    # 只需要保存 band 内的值；band 以 j ≈ i * m / n 为中心
    for i in range(1, n + 1):
        cur = [0] * (m + 1)
        center = int(i * m / n)
        lo = max(1, center - band)
        hi = min(m, center + band)
        ai = a[i - 1]
        row_best = 0
        row_best_j = 0
        for j in range(lo, hi + 1):
            if ai == b[j - 1]:
                v = prev[j - 1] + match
            else:
                v = max(prev[j - 1] + mismatch, prev[j] + gap, cur[j - 1] + gap, 0)
            cur[j] = v
            if v > row_best:
                row_best = v
                row_best_j = j
        if row_best > best.score:
            length = row_best // match if match > 0 else 0
            best = SWResult(
                score=row_best,
                length=max(length, 0),
                a_start=max(0, i - length),
                a_end=i,
                b_start=max(0, row_best_j - length),
                b_end=row_best_j,
            )
        prev = cur

    return best
