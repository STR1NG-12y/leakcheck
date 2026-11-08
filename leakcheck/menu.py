"""交互式菜单（由 启动.bat 调用）。

为什么用 Python 而不是纯 bat：cmd 解析批处理是按字节读的，文件里放中文
（UTF-8 或 GBK）时，读块边界一旦落在汉字中间，就会出现"半行被当成命令"
的错位，中文和 `/` `·` 这类符号尤其容易触发。把菜单搬到 Python 里，
中文由 Python 直接写控制台，彻底绕开这个问题。

用法::

    python -m leakcheck.menu          # 交互菜单
    python -m leakcheck.menu 1        # 直接执行第 1 项（跑完即退）
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .pathpolicy import parse_keyword_file, save_keyword_file

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "leakcheck.idx"

BAR = "=" * 58
SUB = "-" * 58


def clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def pause() -> None:
    try:
        input("\n回车继续...")
    except EOFError:
        pass


def run_cli(args: list[str]) -> int:
    """以子进程跑真正的命令，保持与命令行完全一致的行为。"""
    print()
    code = subprocess.call([sys.executable, "-m", "leakcheck", *args], cwd=ROOT)
    return code


def ask(label: str, example: str = "", optional: bool = False) -> str:
    if example:
        tip = f"     示例   {example}"
        print(tip)
    suffix = "（可留空）" if optional else ""
    try:
        return input(f"{label}{suffix}: ").strip()
    except EOFError:
        return ""


def ask_keywords() -> str:
    """询问是否使用关键词表；直接回车时用项目根目录的 关键词.txt。"""
    default = "关键词.txt" if KEYWORDS.exists() else ""
    tip = f"（可留空，回车使用 {default}）" if default else "（可留空，不用关键词表）"
    print(f"     关键词表{tip}")
    try:
        value = input("     关键词表: ").strip()
    except EOFError:
        value = ""
    if not value:
        value = default
    if value and not (ROOT / value).exists() and not Path(value).exists():
        print(f"     [提示] 找不到 {value}，本次不使用关键词表")
        return ""
    if value:
        print(f"     → 使用关键词表 {value}")
    return value


KEYWORDS = ROOT / "关键词.txt"


def header() -> None:
    idx = "已建立  leakcheck.idx" if INDEX.exists() else "未建立（先跑 2 或 3）"
    kw = "关键词.txt（已找到）" if KEYWORDS.exists() else "未设置（可选）"
    print()
    print("   " + BAR)
    print("       leakcheck    敏感内容外发检测")
    print("       路径为主判据   +   内容二次确认")
    print("   " + BAR)
    print()
    print(f"     目录     {ROOT}")
    print(f"     Python   {sys.version.split()[0]}")
    print(f"     索引     {idx}")
    print(f"     关键词   {kw}")
    print()


def show_menu() -> None:
    print("   ---- 入门 " + "-" * 46)
    print("       [1]  演示            用内置样本跑一遍，不用准备数据")
    print()
    print("   ---- 准备 " + "-" * 46)
    print("       [2]  建索引·目录    按路径规则扫描一个目录")
    print("       [3]  建索引·清单    按可疑路径清单强制收录")
    print()
    print("   ---- 判定 " + "-" * 46)
    print('       [4]  路径判定        只看"该不该保护"，不读内容')
    print("       [5]  判定            路径 + 内容二次确认")
    print("       [6]  内容比对        只给一段明文")
    print()
    print("   ---- 其他 " + "-" * 46)
    print("       [7]  评测            出 P/R/F1 与基线对比")
    print("       [8]  单元测试        108 个用例")
    print("       [9]  管理关键词      增删判定用的关键词")
    print("       [0]  退出")
    print()


# ------------------------------------------------------------------ 动作
def act_demo() -> None:
    run_cli(["demo"])


def act_build_dir() -> None:
    print()
    print("   ---- 建索引：扫描目录 " + "-" * 38)
    print()
    print("      作用   遍历该目录，按路径规则筛出敏感文件，读完内容后建索引")
    print("      示例   D:\\公司资料")
    print("             E:\\2024年归档")
    print()
    d = ask("目录")
    if not d:
        return
    if not Path(d).exists():
        print(f"\n   [错误] 目录不存在：{d}")
        return
    kw = ask_keywords()
    args = ["build", "--dir", d, "-o", "leakcheck.idx"]
    if kw:
        args += ["--keywords", kw]
    run_cli(args)
    print("\n   索引已写入 leakcheck.idx")


def act_build_list() -> None:
    print()
    print("   ---- 建索引：按清单收录 " + "-" * 36)
    print()
    print("      作用   按清单收录文件；清单里规则没命中的也会收（tag=listed）")
    print("      示例   我的清单.txt")
    print("      格式   每行一个路径，# 开头是注释，可以写目录")
    print()
    f = ask("清单文件")
    if not f:
        return
    if not Path(f).exists():
        print(f"\n   [错误] 文件不存在：{f}")
        return
    kw = ask_keywords()
    args = ["build", "--from-list", f, "-o", "leakcheck.idx"]
    if kw:
        args += ["--keywords", kw]
    run_cli(args)
    print("\n   索引已写入 leakcheck.idx")


def act_paths() -> None:
    print()
    print("   ---- 路径判定 " + "-" * 44)
    print()
    print("      作用   只判路径敏不敏感，不读文件内容，不需要索引")
    print("      示例   D:\\公司法务\\2024服务合同.docx")
    print()
    p = ask("路径")
    if not p:
        return
    kw = ask_keywords()
    args = ["paths", "--path", p]
    if kw:
        args += ["--keywords", kw]
    run_cli(args)


def act_judge() -> None:
    print()
    print("   ---- 判定：路径 + 内容 " + "-" * 36)
    print()
    print("      作用   先判路径，再拿明文做二次确认，给出确认/疑似/放行")
    print("      前提   需要索引（先跑 2 或 3）；没有索引时只判路径")
    print("      示例   D:\\公司法务\\2024服务合同.docx")
    print()
    target = ask("文件路径")
    if not target:
        return
    kw = ask_keywords()
    print()
    print("      提示   可留空：只判路径（结论最高为疑似）")
    payload = ask("明文文件", optional=True)
    if not INDEX.exists():
        print("\n   [提示] 没有找到索引，只做路径判定（结论最高为疑似）")
        print("   先跑 [2] 或 [3] 建索引，才能做内容二次确认。")
        args = ["judge", "--path", target]
        if kw:
            args += ["--keywords", kw]
        run_cli(args)
        return
    args = ["judge", "--index", "leakcheck.idx", "--path", target]
    if kw:
        args += ["--keywords", kw]
    if payload:
        args += ["--file", payload]
    run_cli(args)


def act_match() -> None:
    if not INDEX.exists():
        print("\n   [提示] 找不到 leakcheck.idx，请先执行 [2] 或 [3] 建索引。")
        return
    print()
    print("   ---- 内容比对 " + "-" * 44)
    print()
    print("      作用   只给一段明文，比对索引里的敏感内容（不需要路径）")
    print("      示例   prompt.txt")
    print()
    f = ask("明文文件")
    if f:
        run_cli(["match", "--index", "leakcheck.idx", "--file", f])


def act_eval() -> None:
    run_cli(["eval"])


def act_test() -> None:
    print()
    subprocess.call(
        [sys.executable, "-m", "unittest", "discover", "-s", "leakcheck/tests", "-t", "."],
        cwd=ROOT)


def _print_keywords(groups: list[tuple[str, list[str]]]) -> None:
    print()
    print("   ---- 当前关键词 " + "-" * 42)
    print()
    if not groups:
        print("     （还没有关键词表，添加第一个词时会自动创建）")
    for tag, words in groups:
        print(f"     [{tag}]  " + " ".join(words))
    print()


def act_keywords() -> None:
    """查看 / 添加 / 删除关键词，直接改 关键词.txt。"""
    while True:
        groups = parse_keyword_file(KEYWORDS) if KEYWORDS.exists() else []
        _print_keywords(groups)
        print(f"     文件   {KEYWORDS}")
        print()
        print("       [1] 添加关键词")
        print("       [2] 删除关键词")
        print("       [0] 返回")
        print()
        try:
            choice = input("   请选择: ").strip()
        except EOFError:
            return
        if choice in ("0", ""):
            return

        if choice == "1":
            tag = input("     类别（回车用 keyword）: ").strip().strip("[]").strip() or "keyword"
            raw = input("     关键词（空格分隔，可一次加多个）: ").strip()
            words = [w for w in raw.split() if w]
            if not words:
                print("     [提示] 没有输入关键词")
                continue
            tags = [t for t, _ in groups]
            if tag in tags:
                idx = tags.index(tag)
                have = groups[idx][1]
                groups[idx] = (tag, have + [w for w in words if w not in have])
            else:
                groups.append((tag, words))
            save_keyword_file(KEYWORDS, groups)
            print(f"\n     已保存：往 [{tag}] 加了 {len(words)} 个词")
            continue

        if choice == "2":
            raw = input("     要删除的关键词（空格分隔可多个）: ").strip()
            words = set(raw.split())
            if not words:
                continue
            new_groups = []
            removed = 0
            for tag, ws in groups:
                kept = [w for w in ws if w not in words]
                removed += len(ws) - len(kept)
                if kept:
                    new_groups.append((tag, kept))
            save_keyword_file(KEYWORDS, new_groups)
            print(f"\n     已删除 {removed} 个词")
            continue

        print("     [提示] 没有这个选项")


ACTIONS = {
    "1": act_demo,
    "2": act_build_dir,
    "3": act_build_list,
    "4": act_paths,
    "5": act_judge,
    "6": act_match,
    "7": act_eval,
    "8": act_test,
    "9": act_keywords,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 行缓冲：保证菜单文字和子命令输出按顺序出现（管道里尤其明显）
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass

    # 命令行模式：直接跑一项，跑完退出
    if argv:
        action = ACTIONS.get(argv[0])
        if action is None:
            print(f"未知选项：{argv[0]}")
            return 1
        action()
        return 0

    while True:
        clear()
        header()
        show_menu()
        try:
            choice = input("   请输入序号: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice == "0" or choice == "":
            return 0
        action = ACTIONS.get(choice)
        if action is None:
            print(f"\n   [提示] 没有这个序号：{choice}")
            pause()
            continue
        action()
        pause()


if __name__ == "__main__":
    raise SystemExit(main())
