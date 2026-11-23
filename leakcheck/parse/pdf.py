"""PDF 解析：文本层 + 表格 + 图片 OCR。

三条路径并行：
1. **文本层**（pdfplumber）：正常电子版 PDF 走这条，最准
2. **表格**（pdfplumber.extract_tables）：整行 + 单元格双表示，都能成指纹
3. **图片**（PyMuPDF 提取内嵌图 / 整页渲染 → RapidOCR）：
   - 页面文字几乎为空 → 视为扫描页，整页渲染后 OCR
   - 有文字但夹着图 → 只 OCR 那些图（照片、截图里的字）
"""

from __future__ import annotations

from pathlib import Path

from .blocks import (Block, ParseResult, merge_blocks, need_ocr,
                     split_long)
from .layout import sort_reading_order, strip_header_footer

_HINT = "pip install leakcheck[pdf]"

#: 最多 OCR 多少页（防超长扫描件把时间吃光）
MAX_OCR_PAGES = 40
#: 整页渲染的分辨率
OCR_DPI = 200
#: 太小的图不 OCR（图标、分隔线）
MIN_IMAGE_EDGE = 60
#: 单页内最多 OCR 几张图
MAX_IMAGES_PER_PAGE = 5


# ---------------------------------------------------------------- 文本层
def _group_lines(words: list[dict], tol: float = 3.0) -> list[tuple[float, float, str]]:
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (round(float(w["top"]), 1), float(w["x0"])))
    lines: list[list[dict]] = []
    for w in ordered:
        top = float(w["top"])
        if lines and abs(top - float(lines[-1][0]["top"])) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    out: list[tuple[float, float, str]] = []
    for line in lines:
        line.sort(key=lambda w: float(w["x0"]))
        text = "".join(str(w["text"]) for w in line)
        out.append((float(line[0]["x0"]), float(line[0]["top"]), text))
    return out


def _table_blocks(page_no: int, tables, base: int) -> list[Block]:
    """表格双表示：整行（长内容走指纹）+ 单元格（短值也能命中）。"""
    blocks: list[Block] = []
    for table in tables or []:
        for r_i, row in enumerate(table):
            cells = [(c or "").strip().replace("\n", " ") for c in row]
            line = ",".join(cells).strip(", ")
            if line:
                blocks.append(Block(text=line, kind="table_row", page=page_no,
                                    vdoc=base + len(blocks), meta={"row": r_i}))
            for c_i, cell in enumerate(cells):
                if cell:
                    blocks.append(Block(text=cell, kind="table_cell", page=page_no,
                                        vdoc=base + len(blocks),
                                        meta={"row": r_i, "col": c_i}))
    return blocks


# ---------------------------------------------------------------- 图片 OCR
def _decode_image(img: dict):
    """把 pdfplumber 的图片对象转成 OCR 能吃的 ndarray（RGB）。

    页面里的图有两种存法：
    - 内嵌 JPEG/PNG → 直接解码
    - 原始像素流（FlateDecode）→ 按 width/height/colorspace 重建
    """
    import io

    import numpy as np
    from PIL import Image

    stream = img.get("stream")
    if stream is None:
        return None
    try:
        data = stream.get_data()
    except Exception:                                 # noqa: BLE001
        return None
    if not data or len(data) < 2048:
        return None

    # ① 已经是图片文件
    if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return np.array(Image.open(io.BytesIO(data)).convert("RGB"))
        except Exception:                             # noqa: BLE001
            return None

    # ② 原始像素：注意 page.images 里 width/height 是"显示尺寸"，
    #    真实像素在 srcsize 里（两者可能不同，用错了会得到压扁的图）。
    srcsize = img.get("srcsize") or (None, None)
    width = int(srcsize[0] or img.get("width") or 0)
    height = int(srcsize[1] or img.get("height") or 0)
    bits = int(img.get("bits") or 8)
    colorspace = str(img.get("colorspace") or "")
    if not width or not height or bits != 8:
        return None
    if "RGB" in colorspace:
        mode, channels = "RGB", 3
    elif "Gray" in colorspace:
        mode, channels = "L", 1
    else:
        return None
    need = width * height * channels
    if len(data) < need:
        return None
    try:
        image = Image.frombytes(mode, (width, height), data[:need])
        return np.array(image.convert("RGB"))
    except Exception:                                 # noqa: BLE001
        return None


def _page_images(page) -> list:
    out = []
    for img in getattr(page, "images", []) or []:
        array = _decode_image(img)
        if array is not None:
            out.append(array)
    return out


def _ocr_pages(pdf, result: ParseResult, base: int) -> list[Block]:
    """对每页做图片 OCR：扫描页整页当图识别，文字页只识别夹着的图。"""
    from . import ocr

    if not ocr.available():
        result.warnings.append("检测到图片/扫描页，但缺 OCR 引擎（本版已内置，若报错请反馈）")
        return []

    blocks: list[Block] = []
    for pno, page in enumerate(pdf.pages, 1):
        if pno > MAX_OCR_PAGES:
            result.warnings.append(f"超过 {MAX_OCR_PAGES} 页，后面的图片未 OCR")
            break

        try:
            page_text = (page.extract_text() or "").strip()
        except Exception:                             # noqa: BLE001
            page_text = ""

        images = _page_images(page)

        # ① 扫描页：文字极少但有整页图 → 把这张图当正文识别
        if len(page_text) < 30 and images:
            for data in images[:1]:
                got = ocr.ocr_blocks(data, page=pno, vdoc_base=base + len(blocks))
                if got:
                    blocks.extend(got)
                    result.degraded = True
            continue

        # ② 文字页：只识别页面里夹着的图
        for data in images[:MAX_IMAGES_PER_PAGE]:
            got = ocr.ocr_blocks(data, page=pno, vdoc_base=base + len(blocks))
            if got:
                blocks.extend(got)
                result.degraded = True
    return blocks


# ---------------------------------------------------------------- 主流程
def parse_pdf(path: Path, *, vdoc_base: int = 0) -> ParseResult:
    try:
        import pdfplumber
    except ImportError as exc:                        # pragma: no cover
        raise RuntimeError(f"需要 pdfplumber 才能读 .pdf（{_HINT}）") from exc

    result = ParseResult()
    blocks: list[Block] = []
    all_lines: list[tuple[float, float, str]] = []
    page_count = 0

    with pdfplumber.open(str(path)) as pdf:
        page_count = len(pdf.pages)
        for pno, page in enumerate(pdf.pages, 1):
            words = page.extract_words() or []
            lines = _group_lines(words)
            all_lines.extend(lines)
            for x, y, text in lines:
                if not text.strip():
                    continue
                for piece in split_long(Block(text=text, kind="paragraph", page=pno,
                                              bbox=(x, y, x + 1, y + 12))):
                    piece.vdoc = vdoc_base + len(blocks)
                    blocks.append(piece)

            try:
                tables = page.extract_tables() or []
            except Exception:                         # noqa: BLE001
                tables = []
            blocks.extend(_table_blocks(pno, tables, vdoc_base + len(blocks)))

    text_all = "".join(b.text for b in blocks)
    if need_ocr(text_all, all_lines, page_count):
        result.degraded = True
        if not result.warnings:
            result.warnings.append("文本层偏少，已尝试用 OCR 补")

    # 图片 / 扫描页（复用已打开的 pdf 对象，避免二次解析）
    with pdfplumber.open(str(path)) as pdf2:
        blocks.extend(_ocr_pages(pdf2, result, vdoc_base + len(blocks)))

    blocks, removed = strip_header_footer(blocks)
    if removed:
        result.warnings.append(f"剔除页眉页脚 {removed} 行")

    result.blocks = merge_blocks(sort_reading_order(blocks))
    if not result.blocks:
        result.degraded = True
        result.warnings.append(f"PDF 里没有可解析的文本：{path.name}")
    return result
