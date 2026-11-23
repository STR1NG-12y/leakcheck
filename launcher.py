"""打包入口：双击进图形界面，带参数走命令行。

    leakcheck.exe                      → 图形界面（默认）
    leakcheck.exe gui                  → 图形界面
    leakcheck.exe scan --dir D:\\资料   → 命令行
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("gui", "--gui"):
        from leakcheck import gui
        return gui.main([])

    from leakcheck import cli
    return cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
