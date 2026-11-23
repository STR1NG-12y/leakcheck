"""T0 解析层：把任意格式变成带类型的 Block 流。

设计要点
--------
1. **保留结构而不是提取文字**：顺序（阅读顺序）、结构（表格单元格）、区域（虚拟文档）
2. **一个 Block = 一个虚拟文档**：索引里的 doc_idx 直接用 ``Block.vdoc``，
   对角线聚合天然不会跨区域桥接，不需要任何分隔符
3. **按需 OCR**：先评估文本层质量，异常才 fallback
4. **依赖惰性导入**：核心零依赖，缺哪个库就提示装哪个
"""

from __future__ import annotations

from pathlib import Path

from .blocks import Block, ParseResult, merge_blocks, need_ocr

__all__ = ["Block", "ParseResult", "parse", "merge_blocks", "need_ocr"]


#: 图片：直接走 OCR
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


def parse(path: str | Path, *, vdoc_base: int = 0) -> ParseResult:
    """把文件解析成 Block 序列。

    支持：txt/md/csv/json（零依赖）、docx/xlsx/pptx（可选）、pdf（可选）。
    不支持的格式会退回"尽力当文本读"，并把 ``degraded`` 置位。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在：{p}")
    if p.is_dir():
        raise IsADirectoryError(f"这是一个目录：{p}")

    suffix = p.suffix.lower()

    if suffix in {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".py",
                  ".java", ".js", ".ts", ".go", ".c", ".cpp", ".h", ".sql",
                  ".yaml", ".yml", ".ini", ".conf", ".env"}:
        from .text import parse_text
        return parse_text(p, vdoc_base=vdoc_base)

    if suffix == ".docx":
        from .office import parse_docx
        return parse_docx(p, vdoc_base=vdoc_base)

    if suffix == ".xlsx":
        from .office import parse_xlsx
        return parse_xlsx(p, vdoc_base=vdoc_base)

    if suffix == ".pptx":
        from .office import parse_pptx
        return parse_pptx(p, vdoc_base=vdoc_base)

    if suffix in IMAGE_SUFFIXES:
        from . import ocr

        result = ParseResult()
        if not ocr.available():
            result.degraded = True
            result.warnings.append("没有 OCR 引擎，图片读不出文字")
            return result
        blocks = ocr.ocr_blocks(path, page=1, vdoc_base=vdoc_base)
        result.blocks = blocks
        result.degraded = True
        if not blocks:
            result.warnings.append(f"图片里没识别出文字：{path.name}")
        return result

    if suffix == ".pdf":
        from .pdf import parse_pdf
        return parse_pdf(p, vdoc_base=vdoc_base)

    # 未知后缀：尽力当文本读
    from .text import parse_text
    result = parse_text(p, vdoc_base=vdoc_base)
    result.degraded = True
    result.warnings.append(f"未知格式 {suffix or '(无后缀)'}，按纯文本处理")
    return result
