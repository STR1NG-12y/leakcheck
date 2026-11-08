"""评测层：数据集 / 指标 / 基线。"""

from __future__ import annotations

import unittest

from leakcheck.config import Config
from leakcheck.eval.baseline import BruteForce, SimHash
from leakcheck.eval.dataset import build
from leakcheck.eval.metrics import evaluate
from leakcheck.matcher import Matcher


class TestDataset(unittest.TestCase):
    def test_shape_and_labels(self) -> None:
        ds = build(docs=20, seed=0)
        self.assertGreaterEqual(len(ds.units), 5)
        self.assertGreaterEqual(len(ds.payloads), 10)
        ids = {u[0] for u in ds.units}
        for p in ds.payloads:
            self.assertIn(p["expect"], ("hit", "clean", "miss"))
            if p["expect"] in ("hit", "miss"):
                self.assertIn(p["label"], ids)
            else:
                self.assertIsNone(p["label"])

    def test_reproducible(self) -> None:
        a = build(docs=12, seed=1)
        b = build(docs=12, seed=1)
        self.assertEqual([u[2] for u in a.units], [u[2] for u in b.units])

    def test_different_seed_differs(self) -> None:
        a = build(docs=12, seed=1)
        b = build(docs=12, seed=2)
        self.assertNotEqual([u[2] for u in a.units], [u[2] for u in b.units])


class TestMetrics(unittest.TestCase):
    def test_perfect(self) -> None:
        res = evaluate([
            {"id": "1", "kind": "paste", "expect": "hit", "label": "a", "preds": ["a"]},
            {"id": "2", "kind": "chat", "expect": "clean", "label": None, "preds": []},
        ])
        self.assertEqual(res["overall"]["f1"], 1.0)
        self.assertEqual(res["top1"]["acc"], 1.0)

    def test_miss_and_alarm(self) -> None:
        res = evaluate([
            {"id": "1", "kind": "paste", "expect": "hit", "label": "a", "preds": []},
            {"id": "2", "kind": "chat", "expect": "clean", "label": None, "preds": ["b"]},
        ])
        self.assertEqual(res["overall"]["fn"], 1)
        self.assertEqual(res["overall"]["fp"], 1)
        self.assertEqual(len(res["misses"]), 1)
        self.assertEqual(len(res["false_alarms"]), 1)

    def test_boundary_excluded(self) -> None:
        res = evaluate([
            {"id": "s", "kind": "short", "expect": "miss", "label": "a", "preds": []},
        ])
        self.assertEqual(res["overall"]["n"], 0)
        self.assertEqual(res["boundary"]["missed"], 1)


class TestEvalEndToEnd(unittest.TestCase):
    def test_perfect_on_builtin_dataset(self) -> None:
        ds = build(docs=16, seed=0)
        m = Matcher(Config())
        for uid, tag, text in ds.units:
            m.add(uid, text, tag)
        records = []
        for p in ds.payloads:
            hits = m.match(p["text"], p["id"])
            records.append({"id": p["id"], "kind": p["kind"], "expect": p["expect"],
                            "label": p["label"], "preds": [h.doc_id for h in hits]})
        res = evaluate(records)
        self.assertEqual(res["overall"]["fn"], 0, res["misses"])
        self.assertEqual(res["overall"]["fp"], 0, res["false_alarms"])


class TestBaselines(unittest.TestCase):
    def test_bruteforce_finds_substring(self) -> None:
        cfg = Config()
        doc = "第一条 合作内容：乙方为甲方提供数据中台建设与运维服务，服务期限三十六个月。"
        bf = BruteForce([("a", doc)], cfg)
        self.assertEqual(bf.match(doc[10:40], cfg), ["a"])

    def test_bruteforce_misses_rewrite(self) -> None:
        cfg = Config()
        doc = "第一条 合作内容：乙方为甲方提供数据中台建设与运维服务。"
        bf = BruteForce([("a", doc)], cfg)
        self.assertEqual(bf.match("我们打算外包一套数据平台建设服务。", cfg), [])

    def test_simhash_detects_full_text(self) -> None:
        cfg = Config()
        doc = "乙方为甲方提供数据中台建设与运维服务，服务期限为三十六个月。" * 3
        sh = SimHash([("a", doc)], cfg)
        self.assertEqual(sh.match(doc, cfg), ["a"])


if __name__ == "__main__":
    unittest.main()
