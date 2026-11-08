"""① 预处理：把文本变成"规范形式"。

为什么必须有这一层
------------------
同一段文字有无数种写法：多个空格、全角标点、零宽字符、大小写。
如果不先归一化，员工只要在粘贴时多敲一个空格，指纹就对不上——检测直接失效。

铁律：**敏感侧和待上传侧必须调用同一个函数、同一份配置**。
这也是为什么它被单独抽出来，而不是散落在各处。
"""

from __future__ import annotations

import re
import unicodedata

from .config import Config

# 零宽与格式类字符：BOM、零宽空格、零宽连接符、软连字符、方向控制符
_INVISIBLE = dict.fromkeys(
    [0xFEFF, 0x200B, 0x200C, 0x200D, 0x2060, 0x00AD, 0x180E]
    + list(range(0x202A, 0x202F))
    + list(range(0x2066, 0x206A))
)

# 所有空白（含全角空格 \u3000）折叠为一个半角空格
_WS_RE = re.compile(r"[\s\u3000]+")


def normalize(text: str, cfg: Config) -> str:
    """归一化。幂等：``normalize(normalize(x)) == normalize(x)``。

    处理顺序有讲究：先去不可见字符，再 NFKC（全角→半角），最后折空白。
    反过来做会把全角空格变成普通空格后再折叠，结果一样，但先去零宽能避免
    "零宽字符把两个词粘在一起"导致 NFKC 结果异常。
    """
    if not text:
        return ""
    out = text.translate(_INVISIBLE)
    if cfg.nfkc:
        out = unicodedata.normalize("NFKC", out)
    if cfg.collapse_ws:
        out = _WS_RE.sub(" ", out)
    if cfg.lower:
        out = out.lower()
    return out.strip()
