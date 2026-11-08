"""路径比对：判断"这个文件算不算敏感"。

为什么先做这一层
----------------
文档里那句"**是否敏感可通过路径判断**"是整个方案里性价比最高的一步：
它把"全盘内容扫描"降级成"只看被访问过、且路径像敏感文件的那些"，
索引规模正比于"近期访问的敏感文件"，而不是磁盘容量。

三条设计原则
------------
1. **排除优先**：系统目录、依赖目录、缓存目录先剔掉，避免噪音进索引。
2. **命中即解释**：每个判定都带上"哪条规则、命中什么"，便于答辩和排查误判。
3. **规则数据化**：规则可以来自 JSON，加一类敏感目录不用改代码。
"""

from __future__ import annotations

import fnmatch
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_MULTI_SLASH = re.compile(r"/+")


# --------------------------------------------------------------------- 工具
def normalize_path(path: str) -> str:
    """路径归一化，只用于**比对**，不影响原始路径。

    - 反斜杠统一成正斜杠；去掉首尾引号与多余斜杠
    - NFKC（全角字符折叠）+ 统一小写（Windows 大小写不敏感）
    - 保留 UNC 前缀 ``//server/share``
    """
    if not path:
        return ""
    p = path.strip().strip('"').strip("'").replace("\\", "/")
    unc = p.startswith("//")
    p = _MULTI_SLASH.sub("/", p)
    if unc and not p.startswith("//"):
        p = "/" + p
    if len(p) > 1:
        p = p.rstrip("/")
    return unicodedata.normalize("NFKC", p).lower()


def _suffix(basename: str) -> str:
    """取扩展名（不含点）。``.env`` 这种点开头文件没有扩展名，返回空串。"""
    idx = basename.rfind(".")
    if idx <= 0 or idx == len(basename) - 1:
        return ""
    return basename[idx + 1:].lower()


# --------------------------------------------------------------------- 规则
@dataclass(frozen=True)
class PathRule:
    """一条路径规则。``exclude=True`` 表示排除（优先级最高）。"""

    name: str
    exclude: bool = False
    tag: str = "generic"        # contract / code / data / secret / hr / generic
    severity: str = "low"       # high / medium / low
    prefixes: tuple[str, ...] = ()       # 目录前缀，如 "d:/公司法务"
    names: tuple[str, ...] = ()          # 精确文件名，如 ".env"、"id_rsa"
    globs: tuple[str, ...] = ()          # 文件名通配，如 "*合同*.docx"
    extensions: tuple[str, ...] = ()     # 扩展名，不带点
    segments: tuple[str, ...] = ()       # 路径段关键词，如 "合同"

    def __post_init__(self) -> None:
        # 统一小写，匹配时才不用管大小写
        for f in ("prefixes", "names", "globs", "extensions", "segments"):
            lowered = tuple(x.lower() for x in getattr(self, f))
            object.__setattr__(self, f, lowered)

    def match(self, norm: str) -> str | None:
        """命中则返回人类可读的原因，否则 None。"""
        basename = norm.rsplit("/", 1)[-1]
        for prefix in self.prefixes:
            if norm.startswith(prefix):
                return f"目录前缀 {prefix}"
        if basename in self.names:
            return f"文件名 {basename}"
        for pattern in self.globs:
            if fnmatch.fnmatch(basename, pattern):
                return f"文件名匹配 {pattern}"
        ext = _suffix(basename)
        if ext and ext in self.extensions:
            return f"扩展名 .{ext}"
        for keyword in self.segments:
            for seg in norm.split("/"):
                # 排除规则要求"整段相等"，否则 template.docx 会被 temp 误伤；
                # 包含规则允许子串，因为"2024服务合同"这种命名很常见。
                hit = (seg == keyword) if self.exclude else (keyword in seg)
                if hit:
                    return f"路径段『{seg}』含『{keyword}』"
        return None

    def to_dict(self) -> dict:
        return {
            "name": self.name, "exclude": self.exclude, "tag": self.tag,
            "severity": self.severity,
            "prefixes": list(self.prefixes), "names": list(self.names),
            "globs": list(self.globs), "extensions": list(self.extensions),
            "segments": list(self.segments),
        }


@dataclass
class PathDecision:
    """一次路径判定的结果，带解释。"""

    path: str
    normalized: str
    sensitive: bool
    tag: str
    severity: str
    rule: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "path": self.path, "sensitive": self.sensitive, "tag": self.tag,
            "severity": self.severity, "rule": self.rule, "reason": self.reason,
        }


# --------------------------------------------------------------------- 默认规则
# 顺序即优先级：排除规则放最前面
DEFAULT_RULES: tuple[PathRule, ...] = (
    PathRule(
        name="排除-系统目录", exclude=True,
        prefixes=("c:/windows", "c:/program files", "c:/program files (x86)",
                  "c:/programdata"),
    ),
    PathRule(
        name="排除-开发依赖", exclude=True,
        segments=("node_modules", ".git", "__pycache__", ".venv", "venv",
                  "site-packages", ".idea", ".vs"),
    ),
    PathRule(
        name="排除-临时缓存", exclude=True,
        segments=("temp", "tmp", "cache", "logs", ".cache"),
    ),
    PathRule(
        name="密钥", tag="secret", severity="high",
        names=(".env", "id_rsa", "id_dsa", "kubeconfig", "credentials"),
        extensions=("env", "pem", "key", "pfx", "p12", "ppk", "kdbx", "openssh"),
        globs=("*.env", "*.pem", "id_rsa*"),
        segments=("密钥", "凭证", "credential", "secret", ".ssh", ".aws"),
    ),
    PathRule(
        name="人事隐私", tag="hr", severity="high",
        segments=("人事", "简历", "招聘", "身份证", "员工", "薪酬绩效"),
        globs=("*简历*", "*offer*"),
    ),
    PathRule(
        name="财务数据", tag="data", severity="high",
        extensions=("csv", "xlsx", "xls", "et"),
        segments=("财务", "财报", "报表", "流水", "发票", "薪酬", "工资", "客户",
                  "名单", "export"),
    ),
    PathRule(
        name="合同", tag="contract", severity="high",
        extensions=("doc", "docx", "pdf", "rtf", "odt", "wps"),
        globs=("*合同*", "*协议*", "*contract*"),
        segments=("合同", "协议", "法务", "律师", "contract", "nda", "报价"),
    ),
    PathRule(
        name="源代码", tag="code", severity="medium",
        extensions=("py", "js", "ts", "jsx", "tsx", "java", "go", "rs", "c", "cpp",
                    "h", "cs", "rb", "php", "sql", "sh", "kt", "swift", "scala", "lua"),
        segments=("源码", "代码", "src", "repo"),
    ),
    PathRule(
        name="设计文档", tag="design", severity="medium",
        extensions=("fig", "sketch", "psd", "ai"),
        segments=("设计", "图纸", "原型", "prd"),
    ),
)

DEFAULT_RULE = PathRule(name="默认", tag="generic", severity="low")


# --------------------------------------------------------------- 关键词文件
DEFAULT_TAG = "keyword"


def parse_keyword_file(path: str | Path,
                       default_tag: str = DEFAULT_TAG) -> list[tuple[str, list[str]]]:
    """把关键词文件解析成 ``[(类别, [词, ...]), ...]``，保持出现顺序并去重。

    格式：一行一组，``[类别] 词 词``；没有方括号的行归到 ``default_tag``；
    ``#`` 开头是注释。菜单里的"查看/添加/删除"也用这个函数，保证读写一致。
    """
    grouped: dict[str, list[str]] = {}
    order: list[str] = []

    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and "]" in line:
            tag, rest = line[1:].split("]", 1)
            tag = tag.strip() or default_tag
        else:
            tag, rest = default_tag, line
        words = [w for w in rest.split() if w]
        if not words:
            continue
        if tag not in grouped:
            grouped[tag] = []
            order.append(tag)
        grouped[tag].extend(w for w in words if w not in grouped[tag])

    return [(tag, grouped[tag]) for tag in order if grouped[tag]]


def save_keyword_file(path: str | Path, groups: list[tuple[str, list[str]]]) -> None:
    """把分组写回关键词文件（统一格式，注释会被重写）。"""
    lines = [
        "# 关键词表：命中即判为敏感（路径段包含即可）",
        "# 一行一组：[类别] 词 词 词；没有方括号的归到 keyword 类别",
        "# 可以直接用记事本改，也可以在启动菜单的 [9] 里增删",
        "",
    ]
    for tag, words in groups:
        if not words:
            continue
        lines.append(f"[{tag}] " + " ".join(words))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def rules_from_keywords(path: str | Path, default_tag: str = DEFAULT_TAG,
                        severity: str = "high") -> list[PathRule]:
    """从纯文本关键词表生成规则，便于业务人员直接改。

    文件格式（空格或换行分隔都可以，``#`` 开头是注释）::

        # 一行一组；方括号里是类别（tag），后面是关键词
        [contract] 合同 协议 法务 律师
        [design]   图纸 施工图 招标 标书
        财务 报表 流水 薪酬

    **每一行是一个分组**：有方括号的用括号里的类别，没有的归到默认类别。
    同类别的多行会自动合并。关键词按"路径段包含"匹配（与内置规则的 segments 一致）。
    """
    return [
        PathRule(name=f"关键词:{tag}", tag=tag, severity=severity, segments=tuple(words))
        for tag, words in parse_keyword_file(path, default_tag)
    ]


# --------------------------------------------------------------------- 策略
@dataclass
class PathPolicy:
    rules: list[PathRule] = field(default_factory=lambda: list(DEFAULT_RULES))
    default: PathRule = DEFAULT_RULE

    # ---------------------------------------------------------------- 判定
    def check(self, path: str) -> PathDecision:
        norm = normalize_path(path)
        for rule in self.rules:  # 顺序即优先级
            hit = rule.match(norm)
            if hit is None:
                continue
            if rule.exclude:
                return PathDecision(path, norm, False, "excluded", "none",
                                    rule.name, f"命中排除规则：{hit}")
            return PathDecision(path, norm, True, rule.tag, rule.severity,
                                rule.name, hit)
        return PathDecision(path, norm, False, self.default.tag, self.default.severity,
                            self.default.name, "未匹配任何规则")

    def is_sensitive(self, path: str) -> bool:
        return self.check(path).sensitive

    def filter(self, paths: Iterable[str]) -> list[str]:
        return [p for p in paths if self.check(p).sensitive]

    def classify_many(self, paths: Iterable[str]) -> list[PathDecision]:
        return [self.check(p) for p in paths]

    # ---------------------------------------------------------------- 配置
    def add(self, rule: PathRule, front: bool = True) -> None:
        if front:
            self.rules.insert(0, rule)
        else:
            self.rules.append(rule)

    def prepend_includes(self, rules: list[PathRule]) -> None:
        """把一批"包含"规则插到最前，但**保持排除规则仍然优先**。

        自定义规则不该把 C:\\Windows、node_modules 这类排除规则顶掉，
        所以结果顺序是：排除规则 → 新规则 → 其余规则。
        """
        excludes = [r for r in self.rules if r.exclude]
        others = [r for r in self.rules if not r.exclude]
        self.rules = excludes + [r for r in rules if not r.exclude] + others

    def to_dict(self) -> dict:
        return {"rules": [r.to_dict() for r in self.rules]}

    def dump(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                              encoding="utf-8")

    @classmethod
    def from_dict(cls, data: dict) -> "PathPolicy":
        """从 JSON 构造。

        ``{"base": "default", "rules": [...]}`` —— 在内置规则**之前**插入这些规则
        （自定义优先，内置的排除/包含规则仍然保留）。
        不带 ``base`` 时是**完全替换**，只用文件里的规则。
        """
        rules = [PathRule(
            name=r.get("name", "未命名"),
            exclude=bool(r.get("exclude", False)),
            tag=r.get("tag", "generic"),
            severity=r.get("severity", "low"),
            prefixes=tuple(r.get("prefixes", ())),
            names=tuple(r.get("names", ())),
            globs=tuple(r.get("globs", ())),
            extensions=tuple(r.get("extensions", ())),
            segments=tuple(r.get("segments", ())),
        ) for r in data.get("rules", [])]

        if str(data.get("base", "")).lower() == "default":
            return cls(rules=rules + list(DEFAULT_RULES))
        return cls(rules=rules)

    @classmethod
    def load(cls, path: str | Path) -> "PathPolicy":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_keywords(cls, path: str | Path, base: bool = True,
                      default_tag: str = "keyword",
                      severity: str = "high") -> "PathPolicy":
        """用关键词表构造策略。

        ``base=True`` 时保留内置规则（关键词规则插在最前，优先级最高）。
        """
        rules = rules_from_keywords(path, default_tag, severity)
        if base:
            policy = cls()
            policy.prepend_includes(rules)
            return policy
        return cls(rules=rules)
