"""路径比对：归一化、规则命中、优先级、排除。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from leakcheck.pathpolicy import (DEFAULT_RULES, PathPolicy, PathRule,
                                  normalize_path, rules_from_keywords)


class TestNormalize(unittest.TestCase):
    def test_backslash_and_case(self) -> None:
        self.assertEqual(normalize_path(r"C:\公司法务\2024合同.DOCX"),
                         "c:/公司法务/2024合同.docx")

    def test_quotes_and_trailing_slash(self) -> None:
        self.assertEqual(normalize_path('"D:/a/b/"'), "d:/a/b")

    def test_unc_preserved(self) -> None:
        self.assertEqual(normalize_path(r"\\server\share\x.txt"), "//server/share/x.txt")

    def test_fullwidth_folded(self) -> None:
        self.assertEqual(normalize_path("D:\\ＡＢＣ\\x.txt"), "d:/abc/x.txt")

    def test_empty(self) -> None:
        self.assertEqual(normalize_path(""), "")


class TestClassify(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PathPolicy()

    def kind(self, path: str) -> tuple[bool, str, str]:
        d = self.policy.check(path)
        return d.sensitive, d.tag, d.rule

    def test_contract_by_dir_keyword(self) -> None:
        self.assertEqual(self.kind(r"D:\公司法务\2024服务合同.docx"),
                         (True, "contract", "合同"))

    def test_contract_by_extension(self) -> None:
        self.assertTrue(self.kind(r"D:\downloads\agreement.pdf")[0])

    def test_secret_dotfile(self) -> None:
        self.assertEqual(self.kind(r"C:\app\config\.env"), (True, "secret", "密钥"))

    def test_secret_ssh_key(self) -> None:
        self.assertTrue(self.kind(r"C:\Users\LZY\.ssh\id_rsa")[0])

    def test_data_csv(self) -> None:
        self.assertEqual(self.kind(r"D:\财务\客户名单.csv")[1], "data")

    def test_code(self) -> None:
        self.assertEqual(self.kind(r"C:\work\src\main.py")[1], "code")

    def test_hr(self) -> None:
        self.assertEqual(self.kind(r"D:\人事\2024简历合集.docx")[1], "hr")

    def test_plain_not_sensitive(self) -> None:
        d = self.policy.check(r"D:\notes\todo.md")
        self.assertFalse(d.sensitive)
        self.assertEqual(d.rule, "默认")

    def test_reason_is_explainable(self) -> None:
        d = self.policy.check(r"D:\公司法务\2024服务合同.docx")
        self.assertTrue(d.reason)
        self.assertIn("合同", d.reason)


class TestPriorityAndExclude(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PathPolicy()

    def test_system_dir_beats_extension(self) -> None:
        d = self.policy.check(r"C:\Windows\System32\drivers\合同.docx")
        self.assertFalse(d.sensitive)
        self.assertIn("排除", d.rule)

    def test_node_modules_excluded(self) -> None:
        d = self.policy.check(r"D:\proj\node_modules\pkg\合同说明.md")
        self.assertFalse(d.sensitive)

    def test_dot_git_excluded(self) -> None:
        self.assertFalse(self.policy.check(r"D:\proj\.git\config").sensitive)

    def test_temp_segment_excluded(self) -> None:
        d = self.policy.check(r"D:\temp\合同A.docx")
        self.assertFalse(d.sensitive)
        self.assertEqual(d.tag, "excluded")

    def test_template_not_mistaken_for_temp(self) -> None:
        # 回归：曾经用子串匹配 "temp"，把 template 目录误排除
        self.assertTrue(self.policy.check(r"D:\template\合同A.docx").sensitive)

    def test_readme_in_project_not_code(self) -> None:
        self.assertFalse(self.policy.check(r"D:\proj\README.md").sensitive)


class TestPolicyApi(unittest.TestCase):
    def test_filter(self) -> None:
        policy = PathPolicy()
        paths = [r"D:\a\合同.docx", r"D:\b\notes.md", r"D:\c\.env"]
        self.assertEqual([p.split("\\")[-1] for p in policy.filter(paths)],
                         ["合同.docx", ".env"])

    def test_add_custom_rule_first(self) -> None:
        policy = PathPolicy()
        policy.add(PathRule(name="图纸", tag="design", severity="medium",
                            segments=("图纸",)))
        d = policy.check(r"D:\图纸\2024.dwg")
        self.assertTrue(d.sensitive)
        self.assertEqual(d.rule, "图纸")

    def test_json_roundtrip(self) -> None:
        policy = PathPolicy()
        data = policy.to_dict()
        self.assertEqual(len(data["rules"]), len(DEFAULT_RULES))
        restored = PathPolicy.from_dict(json.loads(json.dumps(data)))
        self.assertEqual(len(restored.rules), len(policy.rules))
        self.assertEqual(restored.check(r"D:\a\合同.docx").tag, "contract")

    def test_dump_and_load(self) -> None:
        path = Path(__file__).parent / "_policy.json"
        try:
            PathPolicy().dump(path)
            loaded = PathPolicy.load(path)
            self.assertTrue(loaded.check(r"D:\a\.env").sensitive)
        finally:
            path.unlink(missing_ok=True)

    def test_empty_policy_is_not_sensitive(self) -> None:
        self.assertFalse(PathPolicy(rules=[]).check(r"D:\a\合同.docx").sensitive)

    def test_base_default_appends_to_builtin(self) -> None:
        """带 base=default 时是"追加"：自定义规则优先，内置规则仍然有效。"""
        policy = PathPolicy.from_dict({
            "base": "default",
            "rules": [{"name": "图纸", "tag": "design", "severity": "high",
                       "extensions": ["dwg"]}],
        })
        # 自定义规则生效
        self.assertEqual(policy.check(r"D:\图纸\结构图.dwg").tag, "design")
        # 内置规则仍然生效
        self.assertEqual(policy.check(r"D:\a\合同.docx").tag, "contract")
        # 内置排除仍然生效
        self.assertFalse(policy.check(r"C:\Windows\x.docx").sensitive)

    def test_custom_rule_can_override_builtin(self) -> None:
        """自定义规则插在最前，可以覆盖内置判定（比如把某目录改成排除）。"""
        policy = PathPolicy.from_dict({
            "base": "default",
            "rules": [{"name": "排除-合同草稿", "exclude": True,
                       "segments": ["草稿"]}],
        })
        self.assertFalse(policy.check(r"D:\公司法务\草稿\合同.docx").sensitive)
        self.assertTrue(policy.check(r"D:\公司法务\正式\合同.docx").sensitive)

    def test_without_base_replaces_builtin(self) -> None:
        """不带 base 是"替换"：内置规则不参与。"""
        policy = PathPolicy.from_dict({"rules": [{"name": "只有这一条",
                                                  "extensions": ["dwg"]}]})
        self.assertFalse(policy.check(r"D:\a\合同.docx").sensitive)
        self.assertTrue(policy.check(r"D:\a\x.dwg").sensitive)


class TestKeywordFile(unittest.TestCase):
    def _write(self, text: str) -> Path:
        path = Path(__file__).parent / "_kw.txt"
        path.write_text(text, encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_groups_and_default_tag(self) -> None:
        path = self._write(
            "# 注释行\n"
            "[contract] 合同 协议 法务\n"
            "[design] 图纸 施工图\n"
            "采购 验收单\n")
        rules = rules_from_keywords(path)
        self.assertEqual([r.tag for r in rules], ["contract", "design", "keyword"])
        self.assertEqual(rules[0].segments, ("合同", "协议", "法务"))

    def test_same_tag_merged(self) -> None:
        path = self._write("[contract] 合同\n[contract] 协议 法务\n")
        rules = rules_from_keywords(path)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].segments, ("合同", "协议", "法务"))

    def test_duplicates_removed(self) -> None:
        rules = rules_from_keywords(self._write("[a] 合同 合同 协议\n"))
        self.assertEqual(rules[0].segments, ("合同", "协议"))

    def test_empty_file(self) -> None:
        self.assertEqual(rules_from_keywords(self._write("# 只有注释\n")), [])

    def test_keyword_rules_take_priority(self) -> None:
        path = self._write("[secret2] 绝密\n")
        policy = PathPolicy.from_keywords(path)
        d = policy.check(r"D:\随便\绝密材料.txt")
        self.assertTrue(d.sensitive)
        self.assertEqual(d.tag, "secret2")

    def test_builtin_rules_still_work(self) -> None:
        path = self._write("[x] 绝密\n")
        policy = PathPolicy.from_keywords(path)
        self.assertEqual(policy.check(r"D:\a\合同.docx").tag, "contract")
        self.assertFalse(policy.check(r"C:\Windows\x.docx").sensitive)

    def test_excludes_stay_first(self) -> None:
        """关键词规则不能把系统目录/开发依赖的排除顶掉。"""
        path = self._write("[x] 合同 Windows node_modules\n")
        policy = PathPolicy.from_keywords(path)
        self.assertFalse(policy.check(r"C:\Windows\合同.docx").sensitive)
        self.assertFalse(policy.check(r"D:\p\node_modules\合同.js").sensitive)
        self.assertTrue(policy.check(r"D:\业务\合同.docx").sensitive)


if __name__ == "__main__":
    unittest.main()
