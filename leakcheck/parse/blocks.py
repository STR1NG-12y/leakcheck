"""Block 数据结构 + 质量评估 + 块合并规则。"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 块的最小/最大字符数
MIN_BLOCK_CHARS = 40
MAX_BLOCK_CHARS = 2000

#: 质量评估阈值
OCR_COVER_THRESHOLD = 50        # 每页字符数低于此值 → 大概率扫描件
GARBAGE_RATIO_MAX = 0.10        # 乱码字符占比上限
X_REGRESSION_MAX = 0.20         # 行序回退比例上限


@dataclass
class Block:
    """一段（或一格）内容。**一个 Block 就是一个虚拟文档。**"""

    text: str
    kind: str = "paragraph"      # paragraph | table_row | table_cell | image_text | formula
    page: int = 1
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    vdoc: int = 0
    source: str = "text"         # text | ocr
    confidence: float = 1.0
    meta: dict = field(default_factory=dict)

    @property
    def is_ocr(self) -> bool:
        return self.source == "ocr"

    @property
    def low_confidence(self) -> bool:
        return self.is_ocr and self.confidence < 0.7

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "page": self.page, "vdoc": self.vdoc,
            "source": self.source, "confidence": round(self.confidence, 2),
            "chars": len(self.text), "meta": dict(self.meta),
        }


@dataclass
class ParseResult:
    """一次解析的产物。"""

    blocks: list[Block] = field(default_factory=list)
    degraded: bool = False       # 解析质量不足（走了 OCR 或 fallback）
    warnings: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)

    def to_dict(self) -> dict:
        return {
            "blocks": len(self.blocks),
            "chars": self.chars,
            "degraded": self.degraded,
            "warnings": list(self.warnings),
            "kinds": sorted({b.kind for b in self.blocks}),
        }


# ---------------------------------------------------------------- 质量评估
def _garbage_ratio(lines: list[str]) -> float:
    """乱码比例：替换符、私用区字符、超短行都算。"""
    if not lines:
        return 0.0
    bad = 0
    for line in lines:
        if not line.strip():
            continue
        if len(line.strip()) <= 1:
            bad += 1
            continue
        junk = sum(1 for ch in line
                   if ch == "\ufffd" or 0xE000 <= ord(ch) <= 0xF8FF)
        if junk / max(len(line), 1) > 0.2:
            bad += 1
    return bad / max(len(lines), 1)


def _x_regressions(lines: list[tuple[float, float, str]]) -> float:
    """按 y 排序后，x 出现回退的比例（双栏/乱序的特征）。"""
    if len(lines) < 3:
        return 0.0
    ordered = sorted(lines, key=lambda t: (t[1], t[0]))
    back = sum(1 for i in range(1, len(ordered)) if ordered[i][0] < ordered[i - 1][0] - 1e-6)
    return back / (len(ordered) - 1)


def need_ocr(text: str, lines: list[tuple[float, float, str]] | None = None,
             pages: int = 1) -> bool:
    """是否需要 fallback 到 OCR。

    ``lines`` 是 ``(x, y, text)`` 列表；没有坐标信息时只看字符覆盖率。
    """
    pages = max(pages, 1)
    if len(text) / pages < OCR_COVER_THRESHOLD:
        return True
    if lines:
        if _garbage_ratio([t for _, _, t in lines]) > GARBAGE_RATIO_MAX:
            return True
        if _x_regressions(lines) > X_REGRESSION_MAX:
            return True
    return False


# ---------------------------------------------------------------- 块合并
def merge_blocks(blocks: list[Block], *, max_gap_ratio: float = 1.5) -> list[Block]:
    """把相邻同类块合并成"段"。

    典型场景：pdfplumber 按行返回，一行二三十个字，指纹极稀；
    合并成段后才有足够长的窗口。
    """
    if not blocks:
        return []

    out: list[Block] = []
    cur = blocks[0]
    for nxt in blocks[1:]:
        # 表格的行/单元格各自是独立指纹单元，不能合并成一段
        mergeable = cur.kind not in ("table_row", "table_cell")
        same_kind = mergeable and nxt.kind == cur.kind
        same_page = nxt.page == cur.page
        close = (nxt.bbox[1] - cur.bbox[3]) <= max_gap_ratio * max(cur.bbox[3] - cur.bbox[1], 1.0)
        small = len(cur.text) < MIN_BLOCK_CHARS
        if same_kind and same_page and (close or small) and \
                len(cur.text) + len(nxt.text) <= MAX_BLOCK_CHARS:
            merged = Block(
                text=cur.text + "\n" + nxt.text,
                kind=cur.kind,
                page=cur.page,
                bbox=(min(cur.bbox[0], nxt.bbox[0]), min(cur.bbox[1], nxt.bbox[1]),
                      max(cur.bbox[2], nxt.bbox[2]), max(cur.bbox[3], nxt.bbox[3])),
                vdoc=cur.vdoc,
                source=cur.source,
                confidence=min(cur.confidence, nxt.confidence),
                meta=cur.meta,
            )
            cur = merged
        else:
            out.append(cur)
            cur = nxt
    out.append(cur)

    # 太短的块并进前一块（若同类型），否则保留
    final: list[Block] = []
    for b in out:
        if (final and len(b.text) < MIN_BLOCK_CHARS
                and final[-1].kind == b.kind
                and b.kind not in ("table_row", "table_cell")):
            prev = final[-1]
            prev.text = prev.text + "\n" + b.text
            continue
        final.append(b)
    return final


def split_long(block: Block, limit: int = MAX_BLOCK_CHARS) -> list[Block]:
    """超长块按句末标点再切，避免单个块位置跨度过大。"""
    if len(block.text) <= limit:
        return [block]
    pieces: list[Block] = []
    buf: list[str] = []
    size = 0
    for ch in block.text:
        buf.append(ch)
        size += 1
        if size >= limit and ch in "。！？；\n":
            pieces.append(Block(text="".join(buf), kind=block.kind, page=block.page,
                                bbox=block.bbox, vdoc=block.vdoc, source=block.source,
                                confidence=block.confidence, meta=block.meta))
            buf, size = [], 0
    if buf:
        pieces.append(Block(text="".join(buf), kind=block.kind, page=block.page,
                            bbox=block.bbox, vdoc=block.vdoc, source=block.source,
                            confidence=block.confidence, meta=block.meta))
    return pieces
