"""②a 指纹支路：滚动哈希 + winnowing 抽稀。

两个性质撑起了整个算法
----------------------
1. **滚动哈希** O(n)：滑一个字符只做一次乘加，不重算整个窗口。
2. **winnowing 有检出保证**：每个长度为 w 的哈希窗口取最小值作指纹，
   则任意长度 >= k + w - 1 的公共子串，两侧必然共享至少一个指纹。

第 2 点是"漏检"从玄学变成参数的关键：k+w-1 就是算法的检出下限（默认 23 字），
写成公式而不是"经验值"。

为什么要抽稀：不抽的话每个字符一个指纹，索引膨胀 4~5 倍，而区分度没有提升。
"""

from __future__ import annotations

from collections import deque
from typing import Sequence

from .config import Config

MASK = (1 << 64) - 1
BASE = 0x100000001B3  # FNV-1a 的 64 位质数，奇数且大于最大码位 0x10FFFF


def codepoints(text: str) -> list[int]:
    """字符 → 码位+1（+1 是为了避免 0 值让哈希退化）。"""
    return [ord(ch) + 1 for ch in text]


def rolling_hashes(codes: Sequence[int], k: int) -> list[int]:
    """所有长度 k 窗口的滚动哈希，长度 = len(codes)-k+1。

    ``h[i] = sum(codes[i+j] * BASE^(k-1-j)) mod 2^64``
    """
    n = len(codes)
    if k <= 0 or n < k:
        return []
    h = 0
    for j in range(k):
        h = (h * BASE + codes[j]) & MASK
    out = [h]
    pow_k = pow(BASE, k, 1 << 64)  # 用于剔除滑出窗口的字符
    for i in range(n - k):
        h = (h * BASE - codes[i] * pow_k + codes[i + k]) & MASK
        out.append(h)
    return out


def winnow(hashes: Sequence[int], w: int) -> list[tuple[int, int]]:
    """每个长度 w 的窗口保留最小值（同值取最右），返回 (位置, 哈希)。

    单调队列实现，O(n)。返回的位置单调递增、不重复。
    """
    if w <= 0:
        raise ValueError("w 必须 >= 1")
    m = len(hashes)
    if m == 0:
        return []
    if w >= m:  # 只有一个窗口
        best = min(range(m), key=lambda i: (hashes[i], -i))
        return [(best, hashes[best])]

    out: list[tuple[int, int]] = []
    dq: deque[int] = deque()
    last = -1
    for i, h in enumerate(hashes):
        while dq and hashes[dq[-1]] >= h:
            dq.pop()
        dq.append(i)
        if dq[0] < i - w + 1:  # 队首滑出窗口
            dq.popleft()
        if i >= w - 1:
            pos = dq[0]
            if pos != last:
                out.append((pos, hashes[pos]))
                last = pos
    return out


def fingerprint(text: str, cfg: Config) -> list[tuple[int, int]]:
    """规范文本 → [(位置, 哈希)]。**入参必须是已归一化的文本**。"""
    if len(text) < cfg.k:
        return []
    return winnow(rolling_hashes(codepoints(text), cfg.k), cfg.w)
