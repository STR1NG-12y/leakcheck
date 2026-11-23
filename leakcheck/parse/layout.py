"""版面后处理：页眉页脚剔除、阅读顺序恢复。"""

from __future__ import annotations

from collections import defaultdict

from .blocks import Block


def _norm(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def strip_header_footer(blocks: list[Block], *, min_pages: int = 3,
                        y_tolerance: float = 6.0) -> tuple[list[Block], int]:
    """剔除在多页相同位置反复出现的行（页眉/页脚/水印）。

    判据：文本（去标点后）相同 且 y 坐标接近，且出现在 >= min_pages 页。
    返回 (保留的块, 剔除数量)。
    """
    if not blocks:
        return blocks, 0

    pages = {b.page for b in blocks}
    if len(pages) < min_pages:
        return blocks, 0

    seen: dict[tuple[str, int], set[int]] = defaultdict(set)
    for b in blocks:
        if b.kind != "paragraph" or not b.text.strip():
            continue
        key = (_norm(b.text), int(round(b.bbox[1] / max(y_tolerance, 1e-6))))
        seen[key].add(b.page)

    repeated = {key for key, pgs in seen.items() if len(pgs) >= min_pages}
    if not repeated:
        return blocks, 0

    kept: list[Block] = []
    removed = 0
    for b in blocks:
        if b.kind == "paragraph":
            key = (_norm(b.text), int(round(b.bbox[1] / max(y_tolerance, 1e-6))))
            if key in repeated:
                removed += 1
                continue
        kept.append(b)
    return kept, removed


def sort_reading_order(blocks: list[Block]) -> list[Block]:
    """按 (页, y, x) 排序，恢复阅读顺序。"""
    return sorted(blocks, key=lambda b: (b.page, b.bbox[1], b.bbox[0]))
