"""输入方式：路径清单（强制收录）+ 可疑清单参与判定。"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from leakcheck.decision import Judge
from leakcheck.loader import collect, read_path_list
from leakcheck.matcher import Matcher
from leakcheck.pathpolicy import PathPolicy

ROOT = Path(__file__).parent / "_tmp_list"


def build_tree() -> Path:
    shutil.rmtree(ROOT, ignore_errors=True)
    (ROOT / "公司法务").mkdir(parents=True)
    (ROOT / "misc").mkdir()
    (ROOT / "公司法务" / "2024服务合同.txt").write_text(
        "第二条 合同金额：本合同总金额为人民币壹仟贰佰捌拾万元整。", encoding="utf-8")
    (ROOT / "misc" / "upload_1107.txt").write_text(
        "第十条 本次合作的专属项目编号为 7F3A21，双方函件均以此编号为准。", encoding="utf-8")
    (ROOT / "list.txt").write_text(
        "# 安全团队给的清单\n"
        f"{ROOT / '公司法务' / '2024服务合同.txt'}\n"
        f"{ROOT / 'misc' / 'upload_1107.txt'}\n", encoding="utf-8")
    return ROOT


class TestPathList(unittest.TestCase):
    def setUp(self) -> None:
        self.root = build_tree()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_read_list_skips_comments(self) -> None:
        items = read_path_list(self.root / "list.txt")
        self.assertEqual(len(items), 2)
        self.assertTrue(all("#" not in i for i in items))

    def test_force_includes_rule_missed_files(self) -> None:
        paths = read_path_list(self.root / "list.txt")
        docs, stats = collect(paths, PathPolicy(), force=True)
        self.assertEqual(stats.loaded, 2)
        tags = {Path(d[0]).name: d[1] for d in docs}
        self.assertEqual(tags["2024服务合同.txt"], "contract")
        self.assertEqual(tags["upload_1107.txt"], "listed")  # 规则没覆盖 → 标 listed

    def test_without_force_rule_missed_files_dropped(self) -> None:
        paths = read_path_list(self.root / "list.txt")
        docs, _ = collect(paths, PathPolicy())
        self.assertEqual([Path(d[0]).name for d in docs], ["2024服务合同.txt"])


class TestListedInJudge(unittest.TestCase):
    def setUp(self) -> None:
        self.root = build_tree()
        self.listed = read_path_list(self.root / "list.txt")
        self.page = str(self.root / "misc" / "upload_1107.txt")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_listed_path_is_suspected_not_clean(self) -> None:
        v = Judge(None, PathPolicy(), listed=self.listed).by_path(self.page)
        self.assertEqual(v.level, "suspected")
        self.assertEqual(v.tag, "listed")
        self.assertEqual(v.severity, "high")
        self.assertIn("清单", v.rule)

    def test_unlisted_path_still_clean(self) -> None:
        v = Judge(None, PathPolicy(), listed=self.listed).by_path(
            str(self.root / "misc" / "other.txt"))
        self.assertEqual(v.level, "clean")

    def test_listed_plus_content_is_confirmed(self) -> None:
        docs, _ = collect(self.listed, PathPolicy(), force=True)
        m = Matcher()
        for doc_id, tag, text in docs:
            m.add(doc_id, text, tag)
        text = "看看这句：第十条 本次合作的专属项目编号为 7F3A21，双方函件均以此编号为准。"
        v = Judge(m, PathPolicy(), listed=self.listed).by_path(self.page, text)
        self.assertEqual(v.level, "confirmed")

    def test_directory_in_list_covers_children(self) -> None:
        judge = Judge(None, PathPolicy(), listed=[str(self.root / "misc")])
        v = judge.by_path(self.page)
        self.assertEqual(v.level, "suspected")


if __name__ == "__main__":
    unittest.main()
