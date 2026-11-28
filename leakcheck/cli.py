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
    policy_path = getattr(args, "policy", None)
    if policy_path:
        _require_file(policy_path, "规则文件")
        policy = PathPolicy.load(policy_path)
    else:
        policy = PathPolicy()
    kw = getattr(args, "keywords", None)
    if kw:
        if not Path(kw).exists():
            raise SystemExit(f"找不到关键词文件：{kw}\n"
                             f"（相对路径按当前目录解析：{Path.cwd()}）")
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

    另外无条件把 errors 设为 replace：输出里可能出现零宽字符（demo 的
    "改格式"样本）或生僻字，GBK 编不出来时会直接抛 UnicodeEncodeError，
    在重定向/打包成 exe 时尤其容易踩到。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

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


def _require_file(path: str, what: str) -> None:
    """用户给的路径必须存在，否则给一句人话，而不是抛 Python 堆栈。"""
    if path and not Path(path).exists():
        raise SystemExit(f"找不到{what}：{path}")


# ------------------------------------------------------------------ 子命令
def cmd_build(args) -> int:
    from .cascade import Cascade

    cfg = Config(k=args.k, w=args.w)
    casc = Cascade(cfg)
    policy = _load_policy(args)

    if args.dir:
        _require_file(args.dir, "目录")
        docs, stats = collect([args.dir], policy, force=True)
        print("路径扫描:", json.dumps(stats.to_dict(), ensure_ascii=False))
        if not docs:
            raise SystemExit("路径策略筛完没有剩下可索引的文件"
                             "（用 `python -m leakcheck paths --path ...` 查看判定原因）")
    elif args.from_list:
        _require_file(args.from_list, "清单文件")
        paths = read_path_list(args.from_list)
        docs, stats = collect(paths, policy, force=True)
        print(f"清单 {len(paths)} 条 →", json.dumps(stats.to_dict(), ensure_ascii=False))
        if not docs:
            raise SystemExit("清单里没有可读取的文本文件（检查路径是否存在、是否为二进制）")
    else:
        _require_file(args.docs, "敏感内容文件")
        docs = load_docs(args.docs)

    for doc_id, tag, text in docs:
        casc.add(doc_id, text, tag, doc_id)
    print(f"入库 {len(docs)} 份（含骨架索引，③④ 都能直接用）")
    print("索引统计:", json.dumps(casc.matcher.stats(), ensure_ascii=False))
    out = args.out
    if not Path(out).suffix:
        out += ".idx"
    casc.save(out)
    print(f"已保存 → {out}")
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
    _require_file(args.index, "索引文件（先运行 build 建索引）")
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

    if args.index:
        _require_file(args.index, "索引文件（先运行 build 建索引）")
    matcher = Matcher.load(args.index) if args.index else None
    if args.from_list:
        _require_file(args.from_list, "清单文件")
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


# ------------------------------------------------------------- 内置演示数据
_DEMO_CONTRACT = (
    "第一条 合作内容：乙方为甲方提供数据中台建设与运维服务，服务期限为三十六个月。",
    "第二条 合同金额：本合同总金额为人民币壹仟贰佰捌拾万元整，分四期支付，"
    "每期支付合同总额的百分之二十五。",
    "第三条 保密条款：双方对合作中知悉的商业秘密、技术资料、客户名单负有保密义务，"
    "保密期限为合同终止后五年。",
    "第四条 违约责任：任何一方违约，应向守约方支付合同总额百分之二十的违约金。",
)

_DEMO_CODE = "\n".join([
    "def score(payload: dict) -> int:",
    '    """核心风控评分入口。"""',
    "    total = 0",
    "    for key, value in payload.items():",
    "        total += len(str(key)) * len(str(value))",
    "    return total % 100",
    "",
    "",
    "def decision(score_value: int, threshold: float = 0.75) -> str:",
    '    """根据分数给出处置建议。"""',
    "    if score_value >= threshold * 100:",
    '        return "reject"',
    '    return "accept"',
])

_DEMO_API_KEY = "sk-proj-7f3a9c2e5b8d1f4a6c0e9b7d3a5c1e8f"
_DEMO_DB_PWD = "Zx7Qm2Lp9Kw4"

_DEMO_SALARY = "\n".join([
    "姓名,部门,职级,月薪,入职日期",
    "张伟,研发,高级工程师,32000,2021-03-15",
    "吴静,财务,财务总监,42000,2015-04-10",
    "郑凯,市场,市场专员,14000,2023-08-22",
])

_DEMO_PLAN = ("平台采用「采集层—存储层—计算层—服务层」四层架构。采集层通过 CDC 工具实时同步"
              "业务库变更，存储层使用 Iceberg 作为湖仓一体底座，计算层基于 Spark 与 Flink 双引擎，"
              "服务层对外提供统一的数据 API。")

_DEMO_PREFIX = ("我们最近在做数据治理的整体规划：公司去年完成了业务系统上云，"
                "核心交易库迁移到了新集群，但指标口径一直没统一，财务、运营、风控各算各的。"
                "想请你看看下面这段表述是否准确：\n")
_DEMO_SUFFIX = "\n上面这段摘自内部讨论稿，你只需判断表述是否清楚。"

#: (文档 id, 正文, 类别)
DEMO_DOCS: list[tuple[str, str, str]] = [
    ("2024服务合同.docx", "\n".join(_DEMO_CONTRACT), "contract"),
    ("module_risk.py", _DEMO_CODE, "code"),
    ("prod.env#OPENAI_API_KEY", _DEMO_API_KEY, "secret"),
    ("prod.env#DB_PASSWORD", _DEMO_DB_PWD, "secret"),
    ("员工薪酬名单.csv", _DEMO_SALARY, "hr"),
    ("技术方案-数据中台.md", _DEMO_PLAN, "design"),
]

#: (标题, 预期 hit/miss/boundary, 完整输入)
DEMO_SAMPLES: list[tuple[str, str, str]] = [
    ("逐字复制一条合同条款", "hit", _DEMO_CONTRACT[1]),
    ("润色请求：复制 + 加话", "hit", "帮我润色一下，语气再正式一点：" + _DEMO_CONTRACT[1]),
    ("改格式：全角空格 + 零宽字符", "hit",
     _DEMO_CONTRACT[1].replace("：", "\u3000：").replace("壹仟贰佰", "壹仟贰佰\u200b")),
    ("让 Coding Agent 看代码", "hit",
     "这段代码有性能问题吗？\n```python\n" + _DEMO_CODE + "\n```"),
    ("问 API key 还能不能用", "hit", f"这个 key 还有额度吗？{_DEMO_API_KEY}"),
    ("问数据库密码（12 字短值）", "hit",
     f"帮我连一下测试库，密码是 {_DEMO_DB_PWD}，看看能不能通。"),
    ("表格里的一行", "hit",
     "帮我算一下这个人的年终奖：吴静,财务,财务总监,42000,2015-04-10"),
    ("长文里嵌了一段", "hit", _DEMO_PREFIX + _DEMO_CONTRACT[2] + _DEMO_SUFFIX),
    ("提到技术方案里的一段", "hit", "这段架构描述能展开讲讲吗？" + _DEMO_PLAN),
    ("无关提问（应放行）", "miss", "帮我写一首关于秋天的七言绝句，要求押平声韵。"),
    ("同义改写（已知边界）", "boundary",
     "乙方要给甲方做数据中台的建设，另外还要负责日常的运维工作，合作时长是三年。"),
    ("翻译成英文（已知边界）", "boundary",
     "Translate this clause into English: Party B shall provide Party A with data platform "
     "construction and maintenance services for a term of thirty-six months."),
]


def build_demo_matcher() -> Matcher:
    """按内置演示数据建一个 Matcher（demo 命令与报告脚本共用）。"""
    matcher = Matcher(Config())
    for doc_id, text, tag in DEMO_DOCS:
        matcher.add(doc_id, text, tag)
    return matcher


def cmd_demo(args) -> int:
    """内置演示：用一批真实感的样本跑一遍，覆盖命中/放行/已知边界。"""
    matcher = build_demo_matcher()
    samples = DEMO_SAMPLES

    print()
    print(f"   模拟 {len(samples)} 条用户真实发出的内容，对照本地 6 份敏感资料。")
    print()
    print(f"   敏感库：合同 1 份、代码 1 份、密钥 2 条、薪酬表 1 份、技术方案 1 份，"
          f"共 {matcher.stats()['fingerprints']} 个指纹")
    print()

    n_hit = n_pass = n_bound = n_bad = 0
    for i, (title, expect, text) in enumerate(samples, 1):
        hits = matcher.match(text, f"s{i}")
        preview = text.replace("\n", " ")[:46]
        print(f"   [{i}/{len(samples)}] {title}")
        print(f"        输入   {preview}…")

        if expect == "hit" and hits:
            n_hit += 1
            best = hits[0]
            kind = "内容比对" if best.detector == "text" else "精确匹配"
            print(f"        结果   命中  {best.doc_id}（{best.tag}，{kind}，"
                  f"连续 {best.longest_run} 字，占 {best.coverage_payload:.0%}）")
            span = best.spans[0]
            snippet = text[span.payload_start:span.payload_end].replace("\n", " ")[:38]
            print(f"        片段   {snippet}…")
        elif expect == "hit" and not hits:
            n_bad += 1
            print("        结果   意外：应当命中却放行了")
        elif expect == "miss" and not hits:
            n_pass += 1
            print("        结果   放行")
        elif expect == "miss" and hits:
            n_bad += 1
            print(f"        结果   意外：应当放行却命中了 {hits[0].doc_id}")
        elif expect == "boundary" and not hits:
            n_bound += 1
            print("        结果   漏检（放行）—— 精确匹配路线的已知边界，见 README「限制」")
        else:
            n_bad += 1
            print("        结果   意外：边界样本被检出了")
        print()

    print(f"   小结：{len(samples)} 条中 {n_hit} 条命中、{n_pass} 条放行、"
          f"{n_bound} 条已知边界漏检" + (f"，{n_bad} 条异常" if n_bad else ""))
    print("   提示：真实使用时先用 [2] 给你们的资料建索引，再用 [5] 或 [6] 检测。")
    print()
    return 0


def cmd_eval(args) -> int:
    from .eval.runner import run_eval

    return run_eval(seed=args.seed, docs=args.docs)


def cmd_bench(args) -> int:
    from .eval.bench import run_bench

    sizes = tuple(int(x) for x in args.sizes.split(","))
    return run_bench(sizes=sizes, chars_each=args.chars)


def cmd_scan(args) -> int:
    """三阶级联扫描：逐字 → 骨架 → 语义，输出 A/B/C 证据。"""
    from .cascade import LEVEL_DESC, Cascade
    from .loader import collect
    from .parse import parse

    policy = _load_policy(args)

    if args.index:
        _require_file(args.index, "索引")
        cascade = Cascade.load(args.index)
        print(f"已加载索引：{args.index}（{len(cascade)} 份）")
    else:
        cascade = Cascade(Config(k=args.k, w=args.w))

    if args.index:
        pass
    elif args.dir:
        _require_file(args.dir, "目录")
        docs, stats = collect([args.dir], policy, force=True)
        print("路径扫描:", json.dumps(stats.to_dict(), ensure_ascii=False))
        if not docs:
            raise SystemExit("路径策略筛完没有剩下可索引的文件")
        for doc_id, tag, text in docs:
            cascade.add(doc_id, text, tag, doc_id)
    else:
        _require_file(args.docs, "敏感内容文件")
        for doc_id, tag, text in load_docs(args.docs):
            cascade.add(doc_id, text, tag, doc_id)

    # 待检测内容：单文件 / 目录 / .jsonl / 直接给文本
    if args.file:
        payloads = list(_iter_payloads(Path(args.file)))
        if not payloads:
            raise SystemExit(f"没有可检测的内容：{args.file}")
    elif args.text is not None:
        payloads = [(args.payload_id or "text", args.text)]
    else:
        raise SystemExit("需要 --file 或 --text 之一")

    batch = len(payloads) > 1
    summary: list[dict] = []
    total_hits = 0

    from . import pii as pii_mod
    from .publicref import PublicRef

    ref = PublicRef()
    pii_total = 0
    for name, payload in payloads:
        result = cascade.scan(payload, name)
        for w in result.warnings:
            print(f"   [提示] {w}")

        # 名言/法条这类公共文本不算泄露
        kept, public_n = [], 0
        for item in result.findings:
            a, b = item.payload_span
            if ref.match(payload[a:b]):
                public_n += 1
                continue
            kept.append(item)
        result.findings = kept

        pii_hits = pii_mod.scan(payload)
        pii_total += len(pii_hits)

        if result.findings:
            total_hits += 1

        if args.json:
            summary.append({
                "payload": name,
                "findings": [f.to_dict() for f in result.findings],
                "public_refs": public_n,
                "pii": [h.to_dict() for h in pii_hits],
                "warnings": result.warnings,
            })
            continue

        if batch:
            top = result.top_level or "放行"
            best = result.findings[0] if result.findings else None
            detail = (f"{best.doc_id} · {best.length} 字" if best
                      else ("公共引用，不计泄露" if public_n else "没有发现重合内容"))
            if pii_hits:
                detail += f"   [隐私 {pii_mod.summary(pii_hits)}]"
            print(f"{name:<28} {top:>4}   {detail}")
        elif result.findings:
            for f in result.findings:
                desc = LEVEL_DESC.get(f.level, "")
                print(f"\n[{f.level}] {desc}")
                print(f"   命中  {f.doc_id}（{f.detector}，{f.tag or '-'}）")
                print(f"   长度  {f.length} 字   区间  "
                      f"明文[{f.payload_span[0]}:{f.payload_span[1]}]"
                      f"  <->  文档[{f.doc_span[0]}:{f.doc_span[1]}]")
                for e in f.evidence:
                    print(f"   · {e}")
        else:
            print("\n[放行] 没有发现与敏感库重合的内容")

        if pii_hits:
            print("\n[隐私] 疑似个人敏感信息（脱敏显示）")
            for hit in pii_hits:
                print(f"   {hit.level}  {hit.kind:<6} {hit.masked:<22} {hit.reason}")

    if args.json:
        print(json.dumps(summary if batch else summary[0],
                         ensure_ascii=False, indent=2))
        return 0

    if batch:
        extra = f"，隐私 {pii_total} 处" if pii_total else ""
        print(f"\n共 {len(payloads)} 条，命中 {total_hits} 条{extra}")
    return 0


def _iter_payloads(target: Path):
    """展开待检测目标：目录 / .jsonl / 单文件。逐个产出 (名称, 文本)。"""
    from .parse import parse

    if target.is_dir():
        rich = {".pdf", ".docx", ".xlsx", ".pptx",
                ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
        for file in sorted(target.rglob("*")):
            if not file.is_file():
                continue
            if file.suffix.lower() in rich:          # 目录里的图片/Office 也要能扫
                try:
                    parsed = parse(file)
                    text = "\n\n".join(b.text for b in parsed.blocks)
                except Exception:                    # noqa: BLE001
                    continue
            else:
                text = read_text(file, PAYLOAD_MAX_BYTES)
            if text is not None and text.strip():
                yield file.name, text
        return

    if target.suffix.lower() == ".jsonl":
        for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield str(obj.get("id", "?")), str(obj.get("text", ""))
        return

    from .parse import parse

    parsed = parse(target)
    for w in parsed.warnings:
        print(f"   [解析提示] {w}")
    yield target.name, "\n\n".join(b.text for b in parsed.blocks)


def cmd_screen(args) -> int:
    """语义模糊筛选（向量）。产出是线索，不是证据。"""
    from .loader import collect
    from .parse import parse
    from .vector_screen import VectorScreen, make_embedder

    policy = _load_policy(args)
    try:
        embedder = make_embedder(args.backend, args.model, args.onnx_model)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    screen = VectorScreen(cfg=Config(), embedder=embedder, min_score=args.min_score)

    if getattr(args, "index", None):
        _require_file(args.index, "索引")
        from .matcher import Matcher
        idx = Matcher.load(args.index).index
        for i, name in enumerate(idx.names):
            screen.add(name, idx.texts[i],
                       idx.tags[i] if i < len(idx.tags) else "",
                       idx.paths[i] if i < len(idx.paths) else name)
        print(f"已加载索引：{args.index}（{len(idx.names)} 份）")
    elif args.dir:
        _require_file(args.dir, "目录")
        docs, stats = collect([args.dir], policy, force=True)
        print("路径扫描:", json.dumps(stats.to_dict(), ensure_ascii=False))
        if not docs:
            raise SystemExit("路径策略筛完没有剩下可索引的文件")
        for doc_id, tag, text in docs:
            screen.add(doc_id, text, tag, doc_id)
    else:
        _require_file(args.docs, "敏感内容文件")
        for doc_id, tag, text in load_docs(args.docs):
            screen.add(doc_id, text, tag, doc_id)

    if args.file:
        parsed = parse(args.file)
        payload = "\n\n".join(b.text for b in parsed.blocks)
    elif args.text is not None:
        payload = args.text
    else:
        raise SystemExit("需要 --file 或 --text 之一")

    mode = "语义向量" if screen.semantic else "降级（无模型，仅字面）"
    hits = screen.search(payload, top_k=args.top_k)

    if args.json:
        print(json.dumps({
            "backend": screen.backend_name,
            "semantic": screen.semantic,
            "hits": [h.to_dict() for h in hits],
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"\n   向量后端：{screen.backend_name}（{mode}）")
    print(f"   已建块数：{len(screen)}")
    print()
    if not hits:
        print("[无线索] 没有块达到相似度阈值")
        return 0
    print("[语义线索] 以下结果只供人工参考，**不是**泄露证据")
    for h in hits:
        print(f"\n   {h.score:.3f}  {h.doc_id}（{h.tag or '-'}）  块 #{h.chunk_index}")
        snippet = h.chunk.replace("\n", " ")[:60]
        print(f"          {snippet}…")
    return 0


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
    pb.add_argument("--all", action="store_true",
                    help="该目录下的文件全部收录，不看路径规则（适合把资料集中放在一个目录）")
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

    psc = sub.add_parser("scan", help="两阶扫描：逐字 → 骨架（支持 PDF/Word 等格式）")
    psc.add_argument("--index", help="用 ① build 建好的索引（给了就不用再扫目录）")
    psc.add_argument("--docs", help="docs.jsonl（每行 {id,text,tag}）")
    psc.add_argument("--dir", help="或给一个目录，按路径策略自动筛选")
    psc.add_argument("--policy", help="路径规则 JSON")
    psc.add_argument("--keywords", help="关键词表（纯文本，空格分隔）")
    psc.add_argument("--file", help="待检测文件（txt/docx/pdf/xlsx/pptx…）")
    psc.add_argument("--text", help="或直接给一段文本")
    psc.add_argument("--payload-id", default="", help="给这条内容起个名字")
    psc.add_argument("--k", type=int, default=16)
    psc.add_argument("--w", type=int, default=8)
    psc.add_argument("--json", action="store_true")
    psc.set_defaults(func=cmd_scan)

    psv = sub.add_parser("screen", help="语义模糊筛选（向量模型，产出线索而非证据）")
    psv.add_argument("--index", help="用 ① build 建好的索引（给了就不用再扫目录）")
    psv.add_argument("--docs", help="docs.jsonl（每行 {id,text,tag}）")
    psv.add_argument("--dir", help="或给一个目录，按路径策略自动筛选")
    psv.add_argument("--policy", help="路径规则 JSON")
    psv.add_argument("--keywords", help="关键词表（纯文本，空格分隔）")
    psv.add_argument("--file", help="待筛选文件（txt/docx/pdf…）")
    psv.add_argument("--text", help="或直接给一段文本")
    psv.add_argument("--top-k", dest="top_k", type=int, default=5)
    psv.add_argument("--min-score", dest="min_score", type=float, default=0.55)
    psv.add_argument("--backend", default="auto",
                     choices=["auto", "sentence_transformers", "onnx", "hashing"])
    psv.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    psv.add_argument("--onnx-model", dest="onnx_model", default="")
    psv.add_argument("--json", action="store_true")
    psv.set_defaults(func=cmd_screen)

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
