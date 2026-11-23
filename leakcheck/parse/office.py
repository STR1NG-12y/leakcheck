"""Office 文档：docx / xlsx / pptx（依赖惰性导入）。"""

from __future__ import annotations

from pathlib import Path

from .blocks import Block, ParseResult, merge_blocks

_HINT = {
    "docx": "pip install python-docx",
    "xlsx": "pip install openpyxl",
    "pptx": "pip install python-pptx",
}


def _table_blocks(table, page: int, vdoc_base: int, start: int) -> list[Block]:
    """表格双表示：整行（走指纹）+ 单元格（走短值）。"""
    blocks: list[Block] = []
    rows = list(table.rows)
    for r_i, row in enumerate(rows):
        cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
        line = ",".join(cells)
        if line.strip(", "):
            blocks.append(Block(text=line, kind="table_row", page=page,
                                vdoc=vdoc_base + start + len(blocks),
                                meta={"row": r_i}))
        for c_i, cell in enumerate(cells):
            if cell:
                blocks.append(Block(text=cell, kind="table_cell", page=page,
                                    vdoc=vdoc_base + start + len(blocks),
                                    meta={"row": r_i, "col": c_i}))
    return blocks


def parse_docx(path: Path, *, vdoc_base: int = 0) -> ParseResult:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(f"需要 python-docx 才能读 .docx（{_HINT['docx']}）") from exc

    doc = Document(str(path))
    blocks: list[Block] = []
    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if text:
            blocks.append(Block(text=text, kind="paragraph",
                                vdoc=vdoc_base + len(blocks)))
    for table in doc.tables:
        blocks.extend(_table_blocks(table, 1, vdoc_base, len(blocks)))

    result = ParseResult(blocks=merge_blocks(blocks))
    if not result.blocks:
        result.degraded = True
        result.warnings.append(f"docx 里没有可解析的文本：{path.name}")
    return result


def parse_xlsx(path: Path, *, vdoc_base: int = 0) -> ParseResult:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(f"需要 openpyxl 才能读 .xlsx（{_HINT['xlsx']}）") from exc

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    blocks: list[Block] = []
    try:
        for sheet in wb.worksheets:
            for r_i, row in enumerate(sheet.iter_rows(values_only=True), 1):
                cells = ["" if v is None else str(v).strip() for v in row]
                line = ",".join(cells).strip(", ")
                if not line:
                    continue
                blocks.append(Block(text=line, kind="table_row", page=1,
                                    vdoc=vdoc_base + len(blocks),
                                    meta={"sheet": sheet.title, "row": r_i}))
    finally:
        wb.close()

    return ParseResult(blocks=blocks)


def parse_pptx(path: Path, *, vdoc_base: int = 0) -> ParseResult:
    try:
        from pptx import Presentation
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(f"需要 python-pptx 才能读 .pptx（{_HINT['pptx']}）") from exc

    prs = Presentation(str(path))
    blocks: list[Block] = []
    for page, slide in enumerate(prs.slides, 1):
        items: list[tuple[float, float, str]] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = (shape.text_frame.text or "").strip()
                if text:
                    top = float(getattr(shape, "top", 0) or 0)
                    left = float(getattr(shape, "left", 0) or 0)
                    items.append((left, top, text))
        # 按 Y 再按 X 排序，恢复阅读顺序
        for left, top, text in sorted(items, key=lambda t: (t[1], t[0])):
            blocks.append(Block(text=text, kind="paragraph", page=page,
                                bbox=(left, top, left, top),
                                vdoc=vdoc_base + len(blocks)))

    result = ParseResult(blocks=merge_blocks(blocks, max_gap_ratio=10.0))
    if not result.blocks:
        result.degraded = True
        result.warnings.append(f"pptx 里没有可解析的文本：{path.name}")
    return result
