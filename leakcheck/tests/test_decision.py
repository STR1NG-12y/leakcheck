"""决策层：路径为主 + 内容二次确认。"""

from __future__ import annotations

import unittest

from leakcheck.config import Config
from leakcheck.decision import Judge
from leakcheck.matcher import Matcher
from leakcheck.pathpolicy import PathPolicy

CONTRACT = (
    "第一条 合作内容：乙方为甲方提供数据中台建设与运维服务，服务期限为三十六个月。\n"
    "第二条 合同金额：总金额为人民币壹仟贰佰捌拾万元整，分四期支付。\n"
    "第三条 保密条款：双方对合作中知悉的商业秘密负有保密义务，期限五年。\n"
)

CONTRACT_PATH = r"D:\公司法务\2024服务合同.txt"
NORMAL_PATH = r"D:\notes\todo.md"


def make_judge() -> Judge:
    matcher = Matcher(Config())
    matcher.add(CONTRACT_PATH, CONTRACT, "contract", path=CONTRACT_PATH)
    return Judge(matcher, PathPolicy())


class TestPathMode(unittest.TestCase):
    def test_sensitive_path_without_content_is_suspected(self) -> None:
        v = make_judge().by_path(CONTRACT_PATH)
        self.assertEqual(v.level, "suspected")
        self.assertEqual(v.tag, "contract")
        self.assertEqual(v.severity, "high")
        self.assertIn("路径命中", v.reasons[0])

    def test_clean_path_is_clean(self) -> None:
        v = make_judge().by_path(NORMAL_PATH)
        self.assertEqual(v.level, "clean")
        self.assertFalse(v.actionable)

    def test_path_plus_matching_content_is_confirmed(self) -> None:
        text = "帮我润色：" + CONTRACT.splitlines()[1]
        v = make_judge().by_path(CONTRACT_PATH, text, "p1")
        self.assertEqual(v.level, "confirmed")
        self.assertGreater(v.score, 0)
        self.assertTrue(any("二次确认" in r for r in v.reasons))

    def test_path_plus_unrelated_content_is_suspected(self) -> None:
        v = make_judge().by_path(CONTRACT_PATH, "今天天气不错，晚饭吃什么？", "p2")
        self.assertEqual(v.level, "suspected")
        self.assertTrue(any("未比对成功" in r for r in v.reasons))

    def test_rule_gap_detected_by_content(self) -> None:
        """路径没覆盖，但内容命中了——这是策略漏洞，仍要报。"""
        v = make_judge().by_path(r"D:\random\readme.txt", CONTRACT, "p3")
        self.assertEqual(v.level, "confirmed")
        self.assertTrue(any("策略覆盖" in r for r in v.reasons))

    def test_no_matcher_still_works_for_path_only(self) -> None:
        judge = Judge(None, PathPolicy())
        self.assertEqual(judge.by_path(CONTRACT_PATH).level, "suspected")
        self.assertEqual(judge.by_path(NORMAL_PATH).level, "clean")


class TestContentMode(unittest.TestCase):
    def test_hit_rechecks_path_rules(self) -> None:
        verdicts = make_judge().by_content("帮我润色：" + CONTRACT.splitlines()[2], "c1")
        self.assertEqual(len(verdicts), 1)
        v = verdicts[0]
        self.assertEqual(v.mode, "content")
        self.assertEqual(v.level, "confirmed")
        self.assertEqual(v.tag, "contract")
        self.assertEqual(v.severity, "high")
        self.assertIn(CONTRACT_PATH, v.path)

    def test_clean_content(self) -> None:
        self.assertEqual(make_judge().by_content("写一首关于秋天的诗。"), [])

    def test_content_mode_ignores_path_gap(self) -> None:
        matcher = Matcher(Config())
        matcher.add("随便起的id", CONTRACT, "contract", path=r"D:\x\unknown.md")
        v = Judge(matcher, PathPolicy()).by_content(CONTRACT, "c2")
        self.assertEqual(v[0].level, "confirmed")
        self.assertEqual(v[0].severity, "high")
        self.assertTrue(any("补规则" in r for r in v[0].reasons))


class TestScanPaths(unittest.TestCase):
    def test_batch_filters_clean_and_sorts(self) -> None:
        judge = make_judge()
        verdicts = judge.scan_paths([
            NORMAL_PATH,
            CONTRACT_PATH,
            r"D:\财务\客户名单.csv",
        ])
        self.assertEqual(len(verdicts), 2)
        self.assertEqual(verdicts[0].severity, "high")
        self.assertTrue(all(v.actionable for v in verdicts))

    def test_to_dict_serializable(self) -> None:
        import json

        v = make_judge().by_path(CONTRACT_PATH)
        data = json.loads(json.dumps(v.to_dict(), ensure_ascii=False))
        self.assertEqual(data["level"], "suspected")
        self.assertEqual(data["tag"], "contract")
        self.assertIsInstance(data["reasons"], list)


if __name__ == "__main__":
    unittest.main()
