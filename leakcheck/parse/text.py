"""纯文本类输入：txt / md / csv / 代码等（零依赖）。"""

from __future__ import annotations

import re
from pathlib import Path

from ..loader import read_text
from .blocks import MAX_BLOCK_CHARS, Block, ParseResult, split_long

_MAX_BYTES = 16 * 1024 * 1024
_PARA_SPLIT = re.compile(r"\n\s*\n")
_ROW_SUFFIXES = {".csv", ".tsv"}


def parse_text(path: Path, *, vdoc_base: int = 0) -> ParseResult:
    raw = read_text(path, _MAX_BYTES)
    if raw is None:
        return ParseResult(degraded=True, warnings=[f"读不出文本（编码无法识别）：{path.name}"])

    if path.suffix.lower() in _ROW_SUFFIXES:
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        blocks = [Block(text=ln.strip(), kind="table_row", page=1, vdoc=vdoc_base + i)
                  for i, ln in enumerate(lines)]
    else:
        paras = [p.strip() for p in _PARA_SPLIT.split(raw) if p.strip()]
        if not paras:
            paras = [raw.strip()] if raw.strip() else []
        blocks = []
        for p in paras:
            blocks.extend(split_long(Block(text=p, kind="paragraph", vdoc=vdoc_base + len(blocks))))

    result = ParseResult(blocks=blocks)
    if not blocks:
        result.degraded = True
        result.warnings.append(f"文件里没有可解析的文本：{path.name}")
    if any(len(b.text) > MAX_BLOCK_CHARS for b in blocks):
        result.warnings.append("存在超长块，已按句末标点切分")
    return result
