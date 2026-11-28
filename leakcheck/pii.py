"""隐私信息（PII）识别：内置类型可勾选 + 自定义规则。

等级：
- P0 高危（身份证 / 银行卡 / 护照，校验通过）→ 建议阻断
- P1 疑似（校验未过，或上下文像真数据）→ 人工确认
- P2 弱信号（手机号 / 邮箱 / 车牌 / 工号）→ 提示

展示一律脱敏；完整值只存在内存里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 校验

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def valid_id18(value: str) -> bool:
    """GB 11643 的 mod 11-2 校验位。"""
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


def _validate(check: str, value: str) -> bool:
    if check == "id":
        return valid_id18(value)
    if check == "luhn":
        return valid_luhn(value)
    return True


# ---------------------------------------------------------------- 内置类型

@dataclass(frozen=True)
class BuiltinType:
    key: str
    name: str
    level: str
    check: str
    pattern: str          # 强模式，第 1 组是值
    weak: str | None = None   # 弱模式（只看格式）


BUILTIN_TYPES: tuple[BuiltinType, ...] = (
    BuiltinType("id_card", "身份证", "P0", "id",
                r"(?:公民身份号码|身份证号码|身份证号|身份证|证件号码|证件号)"
                r"\s*[:：]?\s*([0-9]{17}[0-9Xx]|[0-9]{15})",
                r"\b([0-9]{17}[0-9Xx])\b"),
    BuiltinType("bank_card", "银行卡", "P0", "luhn",
                r"(?:银行卡号|银行卡|卡号|账号|账户)\s*[:：]?\s*([0-9][0-9\s\-]{12,25}[0-9])",
                r"\b([0-9]{16,19})\b"),
    BuiltinType("passport", "护照", "P0", "",
                r"(?:护照号码|护照号|护照)\s*[:：]?\s*([EeGg][0-9]{8})"),
    BuiltinType("phone", "手机号", "P2", "",
                r"(?:手机号码|手机号|手机|电话|联系方式)\s*[:：]?\s*(1[3-9][0-9]{9})",
                r"\b(1[3-9][0-9]{9})\b"),
    BuiltinType("email", "邮箱", "P2", "",
                r"(?:电子邮箱|邮箱|邮件)\s*[:：]?\s*([\w.+-]+@[\w-]+\.[\w.]+)",
                r"\b([\w.+-]+@[\w-]+\.[\w.]{2,})\b"),
    BuiltinType("plate", "车牌", "P2", "",
                "", r"([京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼]"
                    r"[A-HJ-NP-Z][0-9A-HJ-NP-Z]{5})"),
    BuiltinType("staff", "工号", "P2", "",
                "", r"(?:工号|员工编号|学号)\s*[:：]?\s*([A-Za-z]{0,3}[0-9]{4,10})"),
)

#: 值格式 → 正则片段（GUI 里选这个，不用写正则）
FORMATS: dict[str, tuple[str, str]] = {
    "num": ("纯数字", r"([0-9]{4,20})"),
    "alpha": ("字母+数字", r"([A-Za-z]{0,3}[0-9]{4,20})"),
    "id": ("身份证", r"([0-9]{17}[0-9Xx])"),
    "card": ("银行卡", r"([0-9][0-9\s\-]{12,25}[0-9])"),
}

# ---------------------------------------------------------------- 数据结构

@dataclass
class PiiHit:
    kind: str
    level: str
    value: str
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


@dataclass
class CustomRule:
    """自定义规则：触发词 + 值格式，正则由程序拼。"""
    kind: str
    triggers: list[str]
    fmt: str = "num"          # num | alpha | id | card | regex
    regex: str = ""           # fmt == "regex" 时用
    check: str = ""
    level: str = "P1"

    def pattern(self) -> str:
        if self.fmt == "regex" and self.regex:
            return self.regex
        body = FORMATS.get(self.fmt, FORMATS["num"])[1]
        if not self.triggers:
            return body
        alt = "|".join(re.escape(t) for t in self.triggers if t)
        return rf"(?:{alt})\s*[:：]?\s*{body}"

    def to_rule(self) -> tuple[str, str, str, str]:
        return (self.kind, self.pattern(), self.check, self.level)


@dataclass
class Settings:
    """隐私设置：启用了哪些内置类型 + 自定义规则。"""
    enabled: set[str] = field(default_factory=lambda: {t.key for t in BUILTIN_TYPES})
    customs: list[CustomRule] = field(default_factory=list)

    # -------------------------------------------------- 读写
    @classmethod
    def load(cls, path: Path | None) -> "Settings":
        if path is None or not Path(path).exists():
            return cls()
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return cls()
        st = cls()
        st.enabled = set()
        found_flag = False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.lower().startswith("# enabled:"):
                st.enabled = {x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()}
                found_flag = True
                continue
            if line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            kind, trig, fmt, check, level = parts[:5]
            triggers = [t for t in trig.replace("|", ",").split(",") if t]
            st.customs.append(CustomRule(kind, triggers, fmt or "num", "", check, level))
        if not found_flag:
            st.enabled = {t.key for t in BUILTIN_TYPES}   # 老文件没开关行，默认全开
        return st

    def save(self, path: Path) -> None:
        lines = ["# leakcheck 隐私规则 v2",
                 "# 第一行控制内置类型开关；下面是自定义规则：类型,触发词,值格式,校验,等级",
                 "# 值格式：num 纯数字 / alpha 字母+数字 / id 身份证 / card 银行卡 / regex 自定义",
                 "# enabled: " + ",".join(sorted(self.enabled))]
        for c in self.customs:
            trig = "|".join(c.triggers) if c.triggers else ""
            lines.append(f"{c.kind},{trig},{c.fmt},-, {c.level}".replace(", ", ","))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -------------------------------------------------- 查询
    def builtin_rules(self) -> list[tuple[str, str, str, str]]:
        return [(t.name, t.pattern, t.check, t.level)
                for t in BUILTIN_TYPES if t.key in self.enabled and t.pattern]

    def weak_rules(self) -> list[tuple[str, str, str | None]]:
        return [(t.name, t.weak, t.check if t.check in ("id", "luhn") else None)
                for t in BUILTIN_TYPES if t.key in self.enabled and t.weak]

    def custom_rules(self) -> list[tuple[str, str, str, str]]:
        return [c.to_rule() for c in self.customs]


# ---------------------------------------------------------------- 扫描

_CONTEXT_PLUS = ("姓名", "客户", "员工", "患者", "本人", "住址", "联系方式",
                 "工资", "薪酬", "合同", "病历", "就诊", "报销", "入职", "身份证")
_CONTEXT_MINUS = ("示例", "例如", "格式", "xxx", "***", "测试", "占位", "请填写")


def _context(text: str, start: int, end: int, words) -> int:
    window = text[max(0, start - 60):min(len(text), end + 60)]
    return sum(1 for w in words if w in window)


def scan(text: str,
         rules: list[tuple[str, str, str, str]] | None = None,
         settings: Settings | None = None) -> list[PiiHit]:
    """扫一段文本。

    - ``settings``：按设置跑（内置勾选 + 自定义）
    - ``rules``：直接给一组规则（老接口，测试用）
    - 都不给：全部内置类型
    """
    if settings is not None:
        strong = settings.builtin_rules()
        weak = settings.weak_rules()
        custom = settings.custom_rules()
    else:
        strong = [(t.name, t.pattern, t.check, t.level) for t in BUILTIN_TYPES if t.pattern]
        weak = [(t.name, t.weak, t.check if t.check in ("id", "luhn") else None)
                for t in BUILTIN_TYPES if t.weak]
        custom = list(rules or [])

    hits: list[PiiHit] = []
    taken: list[tuple[int, int]] = []

    def overlap(a: int, b: int) -> bool:
        return any(not (b <= s or a >= e) for s, e in taken)

    def push(kind: str, level: str, value: str, start: int, end: int, reason: str) -> None:
        hits.append(PiiHit(kind, level, value, start, end, reason))
        taken.append((start, end))

    # 1) 自定义规则（优先级最高，先占位）
    for kind, pattern, check, level in custom:
        try:
            compiled = re.compile(pattern)
        except re.error:
            continue
        for m in compiled.finditer(text):
            start, end = m.span(1) if m.groups() else m.span()
            if overlap(start, end):
                continue
            raw = m.group(1) if m.groups() else m.group(0)
            value = re.sub(r"[\s\-]", "", raw)
            ok = _validate(check, value)
            hit_level = level if (ok or level == "P2") else "P1"
            reason = "自定义规则" + ("（校验通过）" if ok and check else "")
            push(kind, hit_level, value, start, end, reason)

    # 2) 内置强模式
    for kind, pattern, check, level in strong:
        try:
            compiled = re.compile(pattern)
        except re.error:
            continue
        for m in compiled.finditer(text):
            start, end = m.span(1) if m.groups() else m.span()
            if overlap(start, end):
                continue
            value = re.sub(r"[\s\-]", "", m.group(1) if m.groups() else m.group(0))
            ok = _validate(check, value)
            hit_level = level if (ok or level == "P2") else "P1"
            reason = ("标签+校验通过" if ok and check else
                      ("标签+格式命中" if not check else "标签命中但校验未通过"))
            push(kind, hit_level, value, start, end, reason)

    # 3) 内置弱模式
    for kind, pattern, check in weak:
        if not pattern:
            continue
        try:
            compiled = re.compile(pattern)
        except re.error:
            continue
        for m in compiled.finditer(text):
            start, end = m.span(1) if m.groups() else m.span()
            if overlap(start, end):
                continue
            value = re.sub(r"[\s\-]", "", m.group(1) if m.groups() else m.group(0))
            ok = _validate(check or "", value) if check else True
            if check and not ok:
                continue
            level = "P2"
            for t in BUILTIN_TYPES:
                if t.name == kind:
                    level = t.level
            push(kind, level, value, start, end, "格式命中")

    # 4) 上下文调整
    for hit in hits:
        plus = _context(text, hit.start, hit.end, _CONTEXT_PLUS)
        minus = _context(text, hit.start, hit.end, _CONTEXT_MINUS)
        if minus > plus and hit.level == "P0":
            hit.level = "P1"
            hit.reason += "；上下文像示例，降为疑似"
        elif plus >= 2 and hit.level == "P1":
            hit.level = "P0"
            hit.reason += "；上下文强化，升为高危"

    hits.sort(key=lambda h: (h.level, h.start))
    return hits


def summary(hits: list[PiiHit]) -> str:
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

# ---------------------------------------------------------------- 旧格式兼容

DEFAULT_RULES = """# leakcheck 隐私规则 v2
# 第一行控制内置类型开关；下面是自定义规则：类型,触发词,值格式,校验,等级
# 值格式：num 纯数字 / alpha 字母+数字 / id 身份证 / card 银行卡 / regex 自定义
# 想加规则，直接在界面里点「+ 新增」即可，不用改这个文件
# enabled: id_card,bank_card,passport,phone,email,plate,staff
# 例子（去掉行首 # 即生效）：
# 病历号,病历号|住院号,alpha,-,P0
# 保单号,保单号|投保单号,num,-,P1
"""



def parse_rules_text(text: str) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """解析旧版「类型|正则|校验|等级」文本（兼容用）。"""
    rules: list[tuple[str, str, str, str]] = []
    errors: list[str] = []
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("|") < 3:
            errors.append(f"第 {no} 行：需要 4 段（类型|正则|校验|等级）")
            continue
        kind_part, rest = line.split("|", 1)
        left, level = rest.rsplit("|", 1)
        pattern, check = left.rsplit("|", 1)
        kind, pattern = kind_part.strip(), pattern.strip()
        check, level = check.strip(), level.strip()
        if check not in ("", "id", "luhn"):
            errors.append(f"第 {no} 行：校验只能是 id / luhn 或留空")
            continue
        if level not in ("P0", "P1", "P2"):
            errors.append(f"第 {no} 行：等级只能是 P0 / P1 / P2")
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(f"第 {no} 行：正则写错了（{exc}）")
            continue
        rules.append((kind, pattern, check, level))
    return rules, errors


def load_rules(path: Path | None) -> list[tuple[str, str, str, str]]:
    """读旧格式规则文件（兼容老用户的文件）。"""
    if path is None or not Path(path).exists():
        return []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rules, _errors = parse_rules_text(text)
    return rules
