"""④ 评测（二）：指标计算。

口径（写论文时要交代清楚）：

- ``hit`` 样本：预测里包含正确 id → TP，否则 FN
- ``clean`` 样本：预测为空 → TN，否则 FP
- ``miss`` 样本（短于算法保证长度）：单独统计是否被检出，**不计入 P/R**
  —— 否则等于用不可能的任务压低自己的召回率
- top-1：预测的第一条是否就是真值，衡量"能不能指认对"
"""

from __future__ import annotations

from collections import defaultdict

COUNTERS = ("tp", "fn", "fp", "tn")


def _blank() -> dict[str, int]:
    return {k: 0 for k in COUNTERS}


def evaluate(records: list[dict]) -> dict:
    """records 每项：{id, kind, expect, label, preds:[doc_id,...]}。"""
    per_kind: dict[str, dict[str, int]] = defaultdict(_blank)
    overall = _blank()
    boundary = {"detected": 0, "missed": 0, "total": 0}
    top1_hit = top1_total = 0
    misses: list[dict] = []
    false_alarms: list[dict] = []

    for rec in records:
        preds = list(rec.get("preds") or [])
        kind = rec.get("kind", "?")
        expect = rec.get("expect", "hit")
        label = rec.get("label")

        if expect == "miss":
            boundary["total"] += 1
            boundary["detected" if preds else "missed"] += 1
            continue

        if expect == "hit":
            top1_total += 1
            if preds and preds[0] == label:
                top1_hit += 1
            ok = label in preds
            for counter in (per_kind[kind], overall):
                counter["tp" if ok else "fn"] += 1
            if not ok:
                misses.append({"id": rec.get("id"), "kind": kind, "preds": preds})
        else:  # clean
            ok = not preds
            for counter in (per_kind[kind], overall):
                counter["tn" if ok else "fp"] += 1
            if not ok:
                false_alarms.append({"id": rec.get("id"), "kind": kind, "preds": preds})

    def finalize(c: dict[str, int]) -> dict:
        tp, fp, fn, tn = c["tp"], c["fp"], c["fn"], c["tn"]
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return {**c, "precision": round(precision, 4), "recall": round(recall, 4),
                "f1": round(f1, 4), "n": tp + fp + fn + tn}

    return {
        "overall": finalize(overall),
        "kinds": {k: finalize(v) for k, v in sorted(per_kind.items())},
        "boundary": boundary,
        "top1": {"hit": top1_hit, "total": top1_total,
                 "acc": round(top1_hit / top1_total, 4) if top1_total else 0.0},
        "misses": misses,
        "false_alarms": false_alarms,
    }


def format_table(result: dict, title: str = "评测结果") -> str:
    lines = [f"== {title} =="]
    header = f"{'类别':<16}{'TP':>4}{'FN':>4}{'FP':>4}{'TN':>4}{'P':>8}{'R':>8}{'F1':>8}"
    lines += [header, "-" * len(header)]

    def row(name: str, c: dict) -> str:
        return (f"{name:<16}{c['tp']:>4}{c['fn']:>4}{c['fp']:>4}{c['tn']:>4}"
                f"{c['precision']:>8.3f}{c['recall']:>8.3f}{c['f1']:>8.3f}")

    for kind, c in result["kinds"].items():
        lines.append(row(kind, c))
    lines += ["-" * len(header), row("合计", result["overall"])]

    b = result["boundary"]
    if b["total"]:
        lines.append(f"边界样本（短于 k+w-1）：{b['detected']}/{b['total']} 被检出")
    t = result["top1"]
    lines.append(f"top-1 定位准确率：{t['hit']}/{t['total']} = {t['acc']:.1%}")
    for m in result["misses"]:
        lines.append(f"  漏报: {m['id']} ({m['kind']}) 预测={m['preds']}")
    for f in result["false_alarms"]:
        lines.append(f"  误报: {f['id']} ({f['kind']}) 预测={f['preds']}")
    return "\n".join(lines)
