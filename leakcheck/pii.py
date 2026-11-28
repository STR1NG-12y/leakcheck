"""隐私信息（PII）识别：规则 + 校验 + 上下文，独立于内容比对。

输出三类等级：
- P0 高危：强模式 + 校验通过（身份证 / 银行卡 / 护照）
- P1 疑似：弱模式但校验通过，或强模式校验未过
- P2 弱信号：手机号 / 邮箱 / 车牌 / 工号之类

只做提示，不替人下结论；展示一律脱敏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- 校验

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def valid_id18(value: str) -> bool:
    """GB 11643-1999 的 mod 11-2 校验位。"""
    v = value.strip().upper()
    if len(v) != 18 or not v[:17].isdigit() or v[17] not in "0123456789X":
        return False
    total = sum(int(c) * w for c, w in zip(v[:17], _ID_WEIGHTS))
    return _ID_CHECK[total % 11] == v[17]


def valid_luhn(value: str) -> bool:
    """银行卡 Luhn 校验。"""
    digits = [int(c) for c in value if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ---------------------------------------------------------------- 数据结构

@dataclass
class PiiHit:
    kind: str            # 身份证 / 银行卡 / 手机号 …
    level: str           # P0 / P1 / P2
    value: str           # 原始值（内部用，不落盘）
    start: int
    end: int
    reason: str = ""

    @property
    def masked(self) -> str:
        v = self.value
        if len(v) <= 8:
            return v[:2] + "*" * max(0, len(v) - 2)
        return f"{v[:4]}{'*' * (len(v) - 8)}{v[-4:]}"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "level": self.level, "masked": self.masked,
                "start": self.start, "end": self.end, "reason": self.reason}


# ---------------------------------------------------------------- 规则表

#: 触发词（强模式）：标签 + 可选分隔符 + 值
_STRONG = [
    ("身份证", r"(?:公民身份号码|身份证号码|身份证号|身份证|证件号码|证件号)"
               r"\s*[:：]?\s*([0-9]{17}[0-9Xx]|[0-9]{15})"),
    ("银行卡", r"(?:银行卡号|银行卡|卡号|账号|账户)\s*[:：]?\s*"
               r"([0-9][0-9\s\-]{12,25}[0-9])"),
    ("护照",   r"(?:护照号码|护照号|护照)\s*[:：]?\s*([EeGg][0-9]{8})"),
    ("手机号", r"(?:手机号码|手机号|手机|电话|联系方式)\s*[:：]?\s*(1[3-9][0-9]{9})"),
    ("邮箱",   r"(?:电子邮箱|邮箱|邮件)\s*[:：]?\s*([\w.+-]+@[\w-]+\.[\w.]+)"),
]

#: 各类的基础等级（强模式校验通过时）
_BASE_LEVEL = {"身份证": "P0", "银行卡": "P0", "护照": "P0",
               "手机号": "P2", "邮箱": "P2"}

#: 弱模式：只看格式
_WEAK = [
    ("身份证", r"\b([0-9]{17}[0-9Xx])\b", "id"),
    ("银行卡", r"\b([0-9]{16,19})\b", "luhn"),
    ("手机号", r"\b(1[3-9][0-9]{9})\b", None),
    ("邮箱",   r"\b([\w.+-]+@[\w-]+\.[\w.]{2,})\b", None),
    ("车牌",   r"([京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼]"
               r"[A-HJ-NP-Z][0-9A-HJ-NP-Z]{5})", None),
    ("工号",   r"(?:工号|员工编号|学号)\s*[:：]?\s*([A-Za-z]{0,3}[0-9]{4,10})", None),
]

#: 上下文：出现这些词更像真隐私
_CONTEXT_PLUS = ("姓名", "客户", "员工", "患者", "本人", "住址", "联系方式",
                 "工资", "薪酬", "合同", "病历", "就诊", "报销", "入职", "身份证")
#: 出现这些词更像示例
_CONTEXT_MINUS = ("示例", "例如", "格式", "xxx", "***", "测试", "占位", "请填写")


def _context(text: str, start: int, end: int, words) -> int:
    left = max(0, start - 60)
    right = min(len(text), end + 60)
    window = text[left:right]
    return sum(1 for w in words if w in window)


def scan(text: str) -> list[PiiHit]:
    """扫一段文本，返回隐私命中（已按等级排序、已去重）。"""
    hits: list[PiiHit] = []
    taken: list[tuple[int, int]] = []

    def overlap(a: int, b: int) -> bool:
        return any(not (b <= s or a >= e) for s, e in taken)

    # 1) 强模式
    for kind, pattern in _STRONG:
        for m in re.finditer(pattern, text):
            raw = m.group(1)
            start, end = m.span(1)
            if overlap(start, end):
                continue
            value = re.sub(r"[\s\-]", "", raw)
            ok = True
            if kind == "身份证":
                ok = valid_id18(value) if len(value) == 18 else len(value) == 15
            elif kind == "银行卡":
                ok = valid_luhn(value)
            base = _BASE_LEVEL.get(kind, "P1")
            # 高危类型校验不过就降为疑似；本身是弱类型的保持弱等级
            level = base if (ok or base == "P2") else "P1"
            if base == "P2":
                reason = "标签+格式命中"
            elif ok:
                reason = "标签+校验通过"
            else:
                reason = "标签命中但校验未通过"
            hits.append(PiiHit(kind, level, value, start, end, reason))
            taken.append((start, end))

    # 2) 弱模式
    for kind, pattern, check in _WEAK:
        for m in re.finditer(pattern, text):
            raw = m.group(1)
            start, end = m.span(1)
            if overlap(start, end):
                continue
            value = re.sub(r"[\s\-]", "", raw)
            ok = True
            if check == "id":
                ok = valid_id18(value)
                if not ok:
                    continue                      # 18 位数字但不是身份证，直接跳过
            elif check == "luhn":
                ok = valid_luhn(value)
                if not ok:
                    continue                      # 没通过 Luhn 的多半是订单号
            if kind in ("手机号", "邮箱", "车牌", "工号"):
                level = "P2"
            else:
                level = "P0" if ok else "P1"
            hits.append(PiiHit(kind, level, value, start, end,
                               "弱模式命中" + ("（校验通过）" if ok else "")))
            taken.append((start, end))

    # 3) 上下文调整
    for hit in hits:
        plus = _context(text, hit.start, hit.end, _CONTEXT_PLUS)
        minus = _context(text, hit.start, hit.end, _CONTEXT_MINUS)
        if minus > plus:
            if hit.level == "P0":
                hit.level = "P1"
                hit.reason += "；上下文像示例，降为疑似"
        elif plus >= 2 and hit.level == "P1":
            hit.level = "P0"
            hit.reason += "；上下文强化，升为高危"

    hits.sort(key=lambda h: (h.level, h.start))
    return hits


def summary(hits: list[PiiHit]) -> str:
    """P0×1 P1×2 这种摘要。"""
    if not hits:
        return ""
    from collections import Counter
    c = Counter(h.level for h in hits)
    return " ".join(f"{lv}×{c[lv]}" for lv in ("P0", "P1", "P2") if c[lv])


LEVEL_DESC = {
    "P0": "高危隐私（建议阻断，必须脱敏）",
    "P1": "疑似隐私（建议人工确认）",
    "P2": "弱信号（提示）",
}

DEFAULT_RULES = """# 隐私规则（一行一条：类型 | 正则 | 校验 | 等级）
# 校验可选：id（身份证 mod11-2）、luhn（银行卡）、空
身份证 | (?:身份证|证件号码)\\s*[:：]?\\s*([0-9]{17}[0-9Xx]) | id | P0
银行卡 | (?:银行卡|卡号)\\s*[:：]?\\s*([0-9][0-9\\s\\-]{12,25}[0-9]) | luhn | P0
手机号 | (?:手机|电话)\\s*[:：]?\\s*(1[3-9][0-9]{9}) | | P2
"""


def load_rules(path: Path | None) -> list[tuple[str, str, str, str]]:
    """读用户自定义规则；文件不存在就返回空。"""
    if path is None or not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            out.append((parts[0], parts[1], parts[2], parts[3]))
    return out
