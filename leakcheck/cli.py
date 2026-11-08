"""命令行：build / match / demo / eval。

输入约定（整个程序只有这一种"敏感内容"格式）：

    docs.jsonl 每行一个 JSON：
        {"id": "合同A", "text": "……全文……", "tag": "contract"}

待检测明文可以是：命令行字符串、单个 txt 文件、或 batched JSONL。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .config import Config
from .decision import Judge
from .loader import collect, read_path_list, read_text
from .matcher import Matcher
from .pathpolicy import PathPolicy, rules_from_keywords

PAYLOAD_MAX_BYTES = 16 * 1024 * 1024  # 单条明文文件读取上限


def _load_policy(args) -> PathPolicy:
    """统一的策略构造：--policy 打底，--keywords 追加到最前（优先级最高）。"""
    policy = PathPolicy.load(args.policy) if getattr(args, "policy", None) else PathPolicy()
    kw = getattr(args, "keywords", None)
    if kw:
        rules = rules_from_keywords(kw)
        if not rules:
            raise SystemExit(f"关键词文件里没有可用关键词：{kw}")
        policy.rules = [r for r in policy.rules]  # 复制，避免改到共享对象
        policy.prepend_includes(rules)
    return policy


# ------------------------------------------------------------------ 工具
def force_utf8() -> None:
    """默认不动编码：中文 Windows 的控制台是 936，Python 用什么编码输出
    就按什么编码解码，前后一致才不会乱码。

    只有在设置环境变量 ``LEAKCHECK_UTF8=1`` 时才把重定向输出切成 UTF-8
    （用于把结果喂给别的现代工具）。
    """
    if os.environ.get("LEAKCHECK_UTF8") != "1":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def load_docs(path: str) -> list[tuple[str, str, str]]:
    """读 docs.jsonl → [(id, tag, text)]。"""
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                out.append((str(obj["id"]), obj.get("tag", ""), obj["text"]))
            except (json.JSONDecodeError, KeyError) as exc:
                raise SystemExit(f"{path}:{lineno} 格式错误：{exc}") from exc
    return out


def docs_from_dir(path: str, suffixes=(".txt", ".md")) -> list[tuple[str, str, str]]:
    """便捷入口：把一个目录里的文本文件都当成敏感内容（不做路径判定）。

    正式流程请用 ``build --dir``（走路径策略）；这个函数只用于"我已经确认全是敏感文件"的场合。
    """
    root = Path(path)
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in suffixes:
            out.append((p.stem, p.read_text(encoding="utf-8", errors="replace"),
                        p.parent.name or "file"))
    return out


def fmt_hit(hit, payload_text: str = "") -> str:
    span = hit.spans[0]
    snippet = payload_text[span.payload_start:span.payload_end][:50].replace("\n", " ")
    tag = f"（{hit.tag}）" if hit.tag else ""
    return (f"  → {hit.doc_id}{tag}  [{hit.detector}] score={hit.score} "
            f"连续={hit.longest_run}字 覆盖={hit.coverage_payload:.0%}\n"
            f"    理由：{hit.reason}\n"
            f"    片段：{snippet}…")


# ------------------------------------------------------------------ 子命令
def cmd_build(args) -> int:
    cfg = Config(k=args.k, w=args.w)
    matcher = Matcher(cfg)
    policy = _load_policy(args)

    if args.dir:
        docs, stats = collect([args.dir], policy)
        print("路径扫描:", json.dumps(stats.to_dict(), ensure_ascii=False))
        if not docs:
            raise SystemExit("路径策略筛完没有剩下可索引的文件"
                             "（用 `python -m leakcheck paths --path ...` 查看判定原因）")
    elif args.from_list:
        paths = read_path_list(args.from_list)
        docs, stats = collect(paths, policy, force=True)
        print(f"清单 {len(paths)} 条 →", json.dumps(stats.to_dict(), ensure_ascii=False))
        if not docs:
            raise SystemExit("清单里没有可读取的文本文件（检查路径是否存在、是否为二进制）")
    else:
        docs = load_docs(args.docs)

    matched = matcher.add_many(docs)
    print(f"入库 {len(docs)} 份，{matched} 个指纹")
    print("索引统计:", json.dumps(matcher.stats(), ensure_ascii=False))
    matcher.save(args.out)
    print(f"已保存 → {args.out}")
    return 0


def cmd_paths(args) -> int:
    """只做路径判定，不读内容——用来调规则、排查"为什么没进索引"。"""
    policy = _load_policy(args)
    paths: list[str] = list(args.path or [])
    if args.from_file:
        paths += read_path_list(args.from_file)
    if not paths:
        raise SystemExit("请用 --path 或 --from-file 给至少一个路径")

    decisions = policy.classify_many(paths)
    if args.json:
        print(json.dumps([d.to_dict() for d in decisions], ensure_ascii=False, indent=2))
        return 0

    sensitive = sum(1 for d in decisions if d.sensitive)
    for d in decisions:
        mark = "敏感" if d.sensitive else "普通"
        tag = f"{d.tag}/{d.severity}" if d.sensitive else "-"
        print(f"[{mark}] {tag:<14} {d.rule:<12} {d.path}")
        print(f"        └─ {d.reason}")
    print(f"\n共 {len(decisions)} 条路径，{sensitive} 条判定为敏感")
    return 0


def cmd_match(args) -> int:
    matcher = Matcher.load(args.index)
    if args.min_run and args.min_run != matcher.cfg.min_run:
        matcher.cfg = matcher.cfg.with_(min_run=args.min_run)
    rows = []

    if args.jsonl:
        with open(args.jsonl, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                text = obj.get("text", "")
                pid = str(obj.get("id", ""))
                t0 = time.perf_counter()
                hits = matcher.match(text, pid)
                rows.append({"id": pid, "ms": round((time.perf_counter() - t0) * 1000, 2),
                             "hits": hits, "text": text})
    else:
        text = args.text
        if text is None and args.file:
            text = read_text(Path(args.file), PAYLOAD_MAX_BYTES)
            if text is None:
                raise SystemExit(f"读不出文本（需要 UTF-8 / GBK / UTF-16 编码）：{args.file}")
        t0 = time.perf_counter()
        hits = matcher.match(text, args.payload_id)
        rows.append({"id": args.payload_id, "ms": round((time.perf_counter() - t0) * 1000, 2),
                     "hits": hits, "text": text})

    if args.json:
        print(json.dumps([{"id": r["id"], "ms": r["ms"],
                           "hits": [h.to_dict() for h in r["hits"]]} for r in rows],
                         ensure_ascii=False, indent=2))
        return 0

    total = 0
    for r in rows:
        head = f"[{r['id']}]" if r["id"] else "[payload]"
        if not r["hits"]:
            print(f"{head} 无命中（{r['ms']} ms）")
            continue
        total += len(r["hits"])
        print(f"{head} 命中 {len(r['hits'])} 条（{r['ms']} ms）")
        for hit in r["hits"][:5]:
            print(fmt_hit(hit, r["text"]))
    print(f"\n合计 {len(rows)} 条净荷，{total} 次命中")
    return 0


def cmd_judge(args) -> int:
    """主模式：路径判定 → 内容二次确认。"""
    policy = _load_policy(args)

    if (args.text or args.file) and not args.index:
        raise SystemExit("要做内容二次确认，必须用 --index 指定索引")

    matcher = Matcher.load(args.index) if args.index else None
    listed = read_path_list(args.from_list) if args.from_list else []
    judge = Judge(matcher, policy, listed=listed)

    if args.dir:
        root = Path(args.dir)
        if not root.is_dir():
            raise SystemExit(f"不是目录: {args.dir}")
        paths = [str(p) for p in sorted(root.rglob("*")) if p.is_file()]
        verdicts = judge.scan_paths(paths)
        if args.json:
            print(json.dumps([v.to_dict() for v in verdicts], ensure_ascii=False, indent=2))
            return 0
        for v in verdicts:
            print(v.headline())
            for r in v.reasons:
                print(f"    · {r}")
        print(f"\n扫描 {len(paths)} 个文件，{len(verdicts)} 条需要处理")
        return 0

    if not args.path:
        raise SystemExit("请用 --path 或 --dir 指定目标")

    text = args.text
    if text is None and args.file:
        text = read_text(Path(args.file), PAYLOAD_MAX_BYTES)
        if text is None:
            raise SystemExit(f"读不出文本（需要 UTF-8 / GBK / UTF-16 编码）：{args.file}")
    verdict = judge.by_path(args.path, text, args.payload_id)

    if args.json:
        print(json.dumps(verdict.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(verdict.headline())
    for r in verdict.reasons:
        print(f"    · {r}")
    if verdict.hits:
        for hit in verdict.hits[:3]:
            span = hit.spans[0]
            print(f"    └─ {hit.doc_id} [{hit.detector}] 连续 {hit.longest_run} 字，"
                  f"净荷区间 {span.payload_start}-{span.payload_end}")
    return 0


def cmd_demo(args) -> int:
    """内置演示：模拟真实用户在聊天框里发出的几段内容。"""
    cfg = Config()
    matcher = Matcher(cfg)

    contract = [
        "第一条 合作内容：乙方为甲方提供数据中台建设与运维服务，服务期限为三十六个月。",
        "第二条 合同金额：本合同总金额为人民币壹仟贰佰捌拾万元整，分四期支付。",
        "第三条 保密条款：双方对合作中知悉的商业秘密、技术资料负有保密义务，期限五年。",
    ]
    code = "\n".join([
        "def score(payload: dict) -> int:",
        '    """核心风控评分入口。"""',
        "    total = 0",
        "    for key, value in payload.items():",
        "        total += len(str(key)) * len(str(value))",
        "    return total % 100",
    ])
    api_key = "sk-proj-7f3a9c2e5b8d1f4a6c0e9b7d3a5c1e8f"
    db_pwd = "Zx7Qm2Lp9Kw4"

    matcher.add("2024服务合同.docx", "\n".join(contract), "contract")
    matcher.add("module_risk.py", code, "code")
    matcher.add("prod.env/OPENAI_API_KEY", api_key, "secret")
    matcher.add("prod.env/DB_PASSWORD", db_pwd, "secret")

    samples = [
        ("润色一段合同条款", "帮我润色一下，语气再正式一点：\n" + contract[1]),
        ("翻译保密条款", "把这句话翻译成英文：" + contract[2]),
        ("让 Coding Agent 看代码", "这段代码有性能问题吗？\n```python\n" + code + "\n```"),
        ("问 API key 能不能用", f"这个 key 还能用吗？{api_key}"),
        ("问数据库密码", f"帮我连一下数据库，密码是 {db_pwd}"),
        ("普通提问（应当放行）", "帮我写一首关于秋天的七言绝句，要求押平声韵。"),
    ]

    print()
    print("   下面模拟 6 条用户在聊天框里发出的内容，看看哪几条会被判为泄露。")
    print()
    print(f"   本地敏感库：{len(matcher)} 份"
          f"（合同 1 份、代码 1 份、密钥 2 条），"
          f"{matcher.stats()['fingerprints']} 个指纹")
    print()

    hit_count = 0
    for i, (title, text) in enumerate(samples, 1):
        hits = matcher.match(text, f"s{i}")
        preview = text.replace("\n", " ")[:46]
        print(f"   [{i}/{len(samples)}] {title}")
        print(f"        输入   {preview}…")
        if not hits:
            print("        结果   放行\n")
            continue
        hit_count += 1
        best = hits[0]
        kind = "内容比对" if best.detector == "text" else "精确匹配"
        print(f"        结果   命中 {best.doc_id}（{best.tag}，{kind}）")
        print(f"        依据   连续 {best.longest_run} 字相同，"
              f"占这条输入的 {best.coverage_payload:.0%}")
        span = best.spans[0]
        snippet = text[span.payload_start:span.payload_end].replace("\n", " ")[:40]
        print(f"        片段   {snippet}…")
        print()

    print(f"   小结：{len(samples)} 条里 {hit_count} 条命中、"
          f"{len(samples) - hit_count} 条放行。")
    print("   提示：真实使用时先用 [2] 给你们的资料建索引，再用 [5] 判定。")
    print()
    return 0


def cmd_eval(args) -> int:
    from .eval.runner import run_eval

    return run_eval(seed=args.seed, docs=args.docs)


def cmd_bench(args) -> int:
    from .eval.bench import run_bench

    sizes = tuple(int(x) for x in args.sizes.split(","))
    return run_bench(sizes=sizes, chars_each=args.chars)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leakcheck", description="敏感内容包含检测（纯文本算法）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build", help="为敏感内容建索引")
    pb.add_argument("--docs", help="docs.jsonl（每行 {id,text,tag}）")
    pb.add_argument("--dir", help="或给一个目录，按路径策略自动筛选")
    pb.add_argument("--from-list", dest="from_list",
                    help="路径清单（每行一个），清单内文件强制收录，不再问策略")
    pb.add_argument("--policy", help="路径规则 JSON（不给则用内置规则）")
    pb.add_argument("--keywords", help="关键词表（纯文本，空格分隔，命中即敏感，优先级最高）")
    pb.add_argument("-o", "--out", default="leakcheck.idx")
    pb.add_argument("--k", type=int, default=16)
    pb.add_argument("--w", type=int, default=8)
    pb.set_defaults(func=cmd_build)

    pp = sub.add_parser("paths", help="只做路径判定（调规则 / 排查为什么没进索引）")
    pp.add_argument("--path", action="append", help="待判定路径，可重复")
    pp.add_argument("--from-file", dest="from_file", help="从一个文件读路径，每行一个")
    pp.add_argument("--policy", help="路径规则 JSON")
    pp.add_argument("--keywords", help="关键词表（纯文本，空格分隔）")
    pp.add_argument("--json", action="store_true")
    pp.set_defaults(func=cmd_paths)

    pj = sub.add_parser("judge", help="主模式：路径判定 + 内容二次确认")
    pj.add_argument("--path", help="要判定的文件路径")
    pj.add_argument("--dir", help="批量：判定整个目录里的每个文件（只判路径）")
    pj.add_argument("--index", help="索引；给了才能做内容二次确认")
    pj.add_argument("--text", help="待上传明文（做内容确认用）")
    pj.add_argument("--file", help="待上传明文所在文件")
    pj.add_argument("--payload-id", default="", help="给这条明文起个名字")
    pj.add_argument("--policy", help="路径规则 JSON")
    pj.add_argument("--keywords", help="关键词表（纯文本，空格分隔）")
    pj.add_argument("--from-list", dest="from_list",
                    help="可疑路径清单（每行一个）；清单内的文件即使规则不命中也算敏感")
    pj.add_argument("--json", action="store_true")
    pj.set_defaults(func=cmd_judge)

    pm = sub.add_parser("match", help="检测一条待上传明文")
    pm.add_argument("--index", required=True)
    pm.add_argument("--text", help="直接给字符串")
    pm.add_argument("--file", help="给一个 txt 文件")
    pm.add_argument("--jsonl", help="批量：每行 {id,text}")
    pm.add_argument("--payload-id", default="", help="给这条明文起个名字")
    pm.add_argument("--min-run", type=int, help="临时覆盖告警阈值")
    pm.add_argument("--json", action="store_true", help="输出 JSON")
    pm.set_defaults(func=cmd_match)

    pd = sub.add_parser("demo", help="内置小样本演示")
    pd.set_defaults(func=cmd_demo)

    pe = sub.add_parser("eval", help="跑内置评测集，出 P/R/F1 与基线对比")
    pe.add_argument("--docs", type=int, default=20, help="敏感内容份数（越大越慢）")
    pe.add_argument("--seed", type=int, default=0)
    pe.set_defaults(func=cmd_eval)

    pbn = sub.add_parser("bench", help="规模实验：文件数 vs 查询延迟")
    pbn.add_argument("--sizes", default="300,1000,3000", help="逗号分隔的文件数，如 100,500")
    pbn.add_argument("--chars", type=int, default=1200, help="每份约多少字符")
    pbn.set_defaults(func=cmd_bench)
    return p


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
