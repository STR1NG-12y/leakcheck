"""门面层：add / match / 短内容支路 / 持久化。"""

from __future__ import annotations

import unittest

from leakcheck.config import Config
from leakcheck.matcher import Matcher

CONTRACT = (
    "第一条 合作内容：乙方为甲方提供数据中台建设与运维服务，服务期限为三十六个月。\n"
    "第二条 合同金额：总金额为人民币壹仟贰佰捌拾万元整，分四期支付。\n"
    "第三条 保密条款：双方对合作中知悉的商业秘密负有保密义务，期限五年。\n"
    "第四条 违约责任：违约方应支付合同总额百分之十的违约金。\n"
)
SECRET = "Zx7Qm2Lp9Kw4"


def make() -> Matcher:
    m = Matcher(Config())
    m.add("合同A", CONTRACT, "contract")
    m.add("密码", SECRET, "secret")
    return m


class TestMatch(unittest.TestCase):
    def test_paragraph(self) -> None:
        hits = make().match("第二条 合同金额：总金额为人民币壹仟贰佰捌拾万元整，分四期支付。", "p1")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].doc_id, "合同A")
        self.assertEqual(hits[0].detector, "text")
        self.assertGreaterEqual(hits[0].longest_run, 30)

    def test_whole_document(self) -> None:
        hits = make().match(CONTRACT, "p2")
        self.assertAlmostEqual(hits[0].coverage_payload, 1.0, places=2)

    def test_unrelated(self) -> None:
        self.assertEqual(make().match("帮我写一首关于秋天的诗。", "p3"), [])

    def test_mangled_still_detected(self) -> None:
        text = CONTRACT.splitlines()[1].replace(" ", "\u3000").replace(",", "，")
        text = "\u200b".join(text)
        self.assertEqual(len(make().match(text, "p4")), 1)

    def test_too_short_fragment(self) -> None:
        # 16 字 < min_run=32，按策略不报
        self.assertEqual(make().match(CONTRACT[5:21], "p5"), [])

    def test_short_branch(self) -> None:
        hits = make().match(f"数据库密码是 {SECRET}，帮我连一下", "p6")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].detector, "short")
        self.assertEqual(hits[0].score, 1.0)

    def test_short_branch_absent(self) -> None:
        self.assertEqual(make().match("随便聊聊今天的天气怎么样", "p7"), [])

    def test_empty_and_blank(self) -> None:
        self.assertEqual(make().match("", "p8"), [])
        self.assertEqual(make().match("   \n\t ", "p9"), [])


class TestIndexApi(unittest.TestCase):
    def test_duplicate_id_rejected(self) -> None:
        m = make()
        with self.assertRaises(ValueError):
            m.add("合同A", "另一份内容")

    def test_stats(self) -> None:
        stats = make().stats()
        self.assertEqual(stats["docs"], 2)
        self.assertGreater(stats["fingerprints"], 0)
        self.assertEqual(stats["short_patterns"], 1)

    def test_save_load(self) -> None:
        from pathlib import Path

        m = make()
        path = Path(__file__).parent / "_idx.bin"
        try:
            m.save(path)
            loaded = Matcher.load(path)
            self.assertEqual(len(loaded), 2)
            hits = loaded.match(CONTRACT.splitlines()[0], "x")
            self.assertEqual(hits[0].doc_id, "合同A")
        finally:
            path.unlink(missing_ok=True)

    def test_empty_index_returns_nothing(self) -> None:
        self.assertEqual(Matcher(Config()).match("任何内容"), [])

    def test_add_rejects_empty_text(self) -> None:
        with self.assertRaises(ValueError):
            Matcher(Config()).add("x", "")


class TestConfig(unittest.TestCase):
    def test_min_run_constraint(self) -> None:
        with self.assertRaises(ValueError):
            Config(k=16, w=8, min_run=20)  # 20 < 23

    def test_derived(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.guarantee_len, 23)
        self.assertEqual(cfg.max_gap, 64)

    def test_with_returns_copy(self) -> None:
        cfg = Config()
        tighter = cfg.with_(min_run=48)
        self.assertEqual(cfg.min_run, 32)
        self.assertEqual(tighter.min_run, 48)


if __name__ == "__main__":
    unittest.main()
