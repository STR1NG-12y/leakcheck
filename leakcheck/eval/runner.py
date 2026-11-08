"""④ 评测（四）：跑整套实验并打印表格。

流程：造数据 → 建索引 → 逐条检测 → 算指标 → 跑基线 → 对比。
"""

from __future__ import annotations

import time

from ..config import Config
from ..matcher import Matcher
from .baseline import BruteForce, SimHash
from .dataset import build
from .metrics import evaluate, format_table


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _run_baseline(cls, units, payloads, cfg) -> tuple[dict, float]:
    detector = cls(units, cfg)
    records = []
    t0 = time.perf_counter()
    for p in payloads:
        preds = _dedupe(detector.match(p["text"], cfg))
        records.append({"id": p["id"], "kind": p["kind"], "expect": p["expect"],
                        "label": p["label"], "preds": preds})
    return evaluate(records), (time.perf_counter() - t0) * 1000


def run_eval(docs: int = 20, seed: int = 0) -> int:
    cfg = Config()
    ds = build(docs=docs, seed=seed)

    matcher = Matcher(cfg)
    t0 = time.perf_counter()
    for uid, tag, text in ds.units:
        matcher.add(uid, text, tag)
    build_ms = (time.perf_counter() - t0) * 1000

    records = []
    t0 = time.perf_counter()
    for p in ds.payloads:
        hits = matcher.match(p["text"], p["id"])
        records.append({"id": p["id"], "kind": p["kind"], "expect": p["expect"],
                        "label": p["label"], "preds": _dedupe([h.doc_id for h in hits])})
    scan_ms = (time.perf_counter() - t0) * 1000

    stats = matcher.stats()
    print(f"语料：{stats['docs']} 份 / {stats['chars']} 字符 / "
          f"{stats['fingerprints']} 指纹 / {stats['short_patterns']} 条短模式 / "
          f"约 {stats['est_mem_mb']} MB")
    print(f"建索引 {build_ms:.1f} ms，检测 {scan_ms:.1f} ms / {len(ds.payloads)} 条 "
          f"= {scan_ms / max(len(ds.payloads), 1):.2f} ms/条\n")

    result = evaluate(records)
    print(format_table(result, "leakcheck 指纹+倒排+对角线"))
    print()

    units = [(uid, text) for uid, _tag, text in ds.units]
    print("== 基线对比（同一批样本）==")
    print(f"{'方法':<12}{'P':>8}{'R':>8}{'F1':>8}{'ms/条':>10}")
    print("-" * 46)
    print(f"{'leakcheck':<12}{result['overall']['precision']:>8.3f}"
          f"{result['overall']['recall']:>8.3f}{result['overall']['f1']:>8.3f}"
          f"{scan_ms / max(len(ds.payloads), 1):>10.2f}")
    for cls, note in ((BruteForce, "逐条 find"), (SimHash, "整篇 simhash")):
        try:
            res, ms = _run_baseline(cls, units, ds.payloads, cfg)
        except Exception as exc:  # 基线不该拖垮主流程
            print(f"{cls.name:<12} 运行失败：{exc}")
            continue
        o = res["overall"]
        print(f"{cls.name:<12}{o['precision']:>8.3f}{o['recall']:>8.3f}"
              f"{o['f1']:>8.3f}{ms / max(len(ds.payloads), 1):>10.2f}  {note}")

    print("\n说明：暴力法在小语料下往往更快（C 实现的 find），"
          "它的代价随语料规模线性增长；索引法的查询开销基本与语料无关。")
    return 0
