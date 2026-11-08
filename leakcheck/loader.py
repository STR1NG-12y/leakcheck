"""按路径策略把文件读成"待索引的文本"。

职责边界很清楚：
- 路径判定（敏不敏感、哪一类）在 :mod:`leakcheck.pathpolicy` 里做，纯逻辑、可单测；
- 这里只做 I/O：遍历目录 → 问策略 → 读文本 → 产出 (id, tag, text)。

id 用"相对输入根目录的路径"，保证唯一且可读。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .pathpolicy import PathPolicy

# 已知读不出文本的格式：直接跳过，不去尝试解码
BINARY_SUFFIXES = (
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".rtf", ".odt",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".ico",
    ".zip", ".rar", ".7z", ".gz", ".tar", ".exe", ".dll", ".so", ".bin",
    ".db", ".sqlite", ".mdb", ".mp3", ".mp4", ".mov", ".avi", ".psd", ".fig",
)


@dataclass
class LoadStats:
    scanned: int = 0        # 遍历到的文件数
    sensitive: int = 0      # 路径判定为敏感的
    loaded: int = 0         # 成功读出文本的文件数
    units: int = 0          # 实际入库的条目数（密钥文件会按值拆成多条）
    skipped_size: int = 0   # 太大跳过
    skipped_binary: int = 0  # 读不出文本
    skipped_suffix: int = 0  # 已知的二进制格式
    chars: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "errors"}
        d["errors"] = self.errors[:5]
        return d


def secret_units(rel_id: str, text: str, min_len: int = 8) -> list[tuple[str, str, str]]:
    """把密钥类文件按"值"拆成多条。

    索引粒度 = 想保护的粒度：整篇 `.env` 入库只能保护整行，
    而真实泄露往往是单独一个密码值，所以这里拆成 ``KEY=VALUE`` 的右侧部分。
    """
    units: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", ":"):
            if sep in line:
                key, value = line.split(sep, 1)
                value = value.strip().strip('"').strip("'")
                if len(value) >= min_len:
                    units.append((f"{rel_id}#{key.strip()}", "secret", value))
                break
    return units or [(rel_id, "secret", text)]


def read_text(path: Path, max_bytes: int) -> str | None:
    """尽力读出文本；二进制返回 None。"""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > max_bytes:
        return None
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def read_path_list(path: str | Path) -> list[str]:
    """读路径清单：每行一个路径，``#`` 开头为注释，支持目录。"""
    out: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip().strip('"')
        if line and not line.startswith("#"):
            out.append(line)
    return out


def collect(paths: list[str], policy: PathPolicy,
            max_bytes: int = 4 * 1024 * 1024,
            force: bool = False) -> tuple[list[tuple[str, str, str]], LoadStats]:
    """返回 ([(id, tag, text)], 统计)。

    ``force=True`` 时清单里的文件**不再问路径策略**，一律收录（tag 用策略给的，
    策略判不敏感则标 ``listed``）——用于"安全团队已经认定可疑"的清单。
    """
    stats = LoadStats()
    docs: list[tuple[str, str, str]] = []

    for raw in paths:
        root = Path(raw)
        if root.is_file():
            # 单文件输入用"给的路径"当 id：既能避免同名冲突，也让后续路径规则能拿到目录上下文
            candidates = [(root, str(root))]
        elif root.is_dir():
            candidates = [(p, str(p.relative_to(root)).replace("\\", "/"))
                          for p in sorted(root.rglob("*")) if p.is_file()]
        else:
            stats.errors.append(f"路径不存在: {raw}")
            continue

        for path, rel_id in candidates:
            stats.scanned += 1
            decision = policy.check(str(path))
            if not decision.sensitive and not force:
                continue
            stats.sensitive += 1
            if path.suffix.lower() in BINARY_SUFFIXES:
                stats.skipped_suffix += 1
                continue
            text = read_text(path, max_bytes)
            if text is None:
                stats.skipped_binary += 1
                continue
            if not text.strip():
                stats.skipped_binary += 1
                continue
            stats.loaded += 1
            stats.chars += len(text)
            tag = decision.tag if decision.sensitive else "listed"
            if tag == "secret":
                docs.extend(secret_units(rel_id, text))
            else:
                docs.append((rel_id, tag, text))
    stats.units = len(docs)
    return docs, stats
