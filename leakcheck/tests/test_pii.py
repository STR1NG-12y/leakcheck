"""隐私识别与公共引用库的单元测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
from leakcheck import pii
from leakcheck.publicref import PublicRef


class TestPiiValidation(unittest.TestCase):
    def test_valid_id18(self) -> None:
        self.assertTrue(pii.valid_id18("110101199003071233"))
        self.assertFalse(pii.valid_id18("110101199003071234"))   # 校验位错
        self.assertFalse(pii.valid_id18("11010119900307123"))    # 位数不够

    def test_valid_luhn(self) -> None:
        self.assertTrue(pii.valid_luhn("6222021234567890128"))
        self.assertFalse(pii.valid_luhn("1234567890123456"))


class TestPiiScan(unittest.TestCase):
    def test_id_strong_pattern_is_p0(self) -> None:
        hits = pii.scan("客户张伟，身份证号码：110101199003071233。")
        kinds = {(h.kind, h.level) for h in hits}
        self.assertIn(("身份证", "P0"), kinds)

    def test_bad_checksum_is_p1(self) -> None:
        hits = pii.scan("身份证号 110101199003071234")
        self.assertEqual(hits[0].kind, "身份证")
        self.assertEqual(hits[0].level, "P1")

    def test_order_number_not_flagged(self) -> None:
        # 18 位但不是身份证，Luhn 也不通过，不该报
        self.assertEqual(pii.scan("订单号 123456789012345678 已发货。"), [])

    def test_phone_and_email_are_p2(self) -> None:
        hits = pii.scan("手机 13800138000，邮箱 a@b.com")
        levels = {h.kind: h.level for h in hits}
        self.assertEqual(levels.get("手机号"), "P2")
        self.assertEqual(levels.get("邮箱"), "P2")

    def test_placeholder_not_flagged(self) -> None:
        self.assertEqual(pii.scan("请填写身份证号：____________（示例）"), [])

    def test_masked(self) -> None:
        hits = pii.scan("身份证号码：110101199003071233")
        self.assertEqual(hits[0].masked[:4], "1101")
        self.assertTrue(hits[0].masked.endswith("1233"))
        self.assertNotIn(hits[0].value, hits[0].masked)


class TestPublicRef(unittest.TestCase):
    def test_famous_quote_matched(self) -> None:
        ref = PublicRef()
        self.assertIsNotNone(ref.match("千里之行，始于足下。"))

    def test_legal_boilerplate_matched(self) -> None:
        ref = PublicRef()
        self.assertIsNotNone(ref.match("双方本着平等自愿、诚实信用的原则，签订本合同。"))

    def test_specific_clause_not_matched(self) -> None:
        ref = PublicRef()
        self.assertIsNone(ref.match("本合同总金额为人民币壹仟贰佰捌拾万元整，分四期支付。"))

    def test_user_lines_appended(self) -> None:
        ref = PublicRef(extra_lines=["# 注释", "本公司固定表述一段话"])
        self.assertIsNotNone(ref.match("本公司固定表述一段话"))


if __name__ == "__main__":
    unittest.main()

class TestPiiCustomRules(unittest.TestCase):
    def test_parse_rule_with_pipe_in_regex(self) -> None:
        rules, errors = pii.parse_rules_text(
            "病历号 | (?:病历号|住院号)\\s*[:：]?\\s*([A-Z]{0,2}\\d{6,10}) | | P0")
        self.assertEqual(errors, [])
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0][0], "病历号")
        self.assertIn("|", rules[0][1])          # 正则里的 | 没被拆掉

    def test_bad_rule_reported(self) -> None:
        rules, errors = pii.parse_rules_text("坏行\n等级错 | (x) | | P9")
        self.assertEqual(rules, [])
        self.assertEqual(len(errors), 2)

    def test_custom_rule_hits(self) -> None:
        rules = [("病历号", r"(?:病历号|住院号)\s*[:：]?\s*([A-Z]{0,2}\d{6,10})", "", "P0")]
        hits = pii.scan("患者病历号：ZY2026001234", rules)
        self.assertTrue(any(h.kind == "病历号" and h.level == "P0" for h in hits))

    def test_custom_rule_ignored_when_invalid(self) -> None:
        hits = pii.scan("普通的一句话", [("坏", "([", "", "P0")])
        self.assertEqual([h for h in hits if h.kind == "坏"], [])

class TestPiiSettings(unittest.TestCase):
    def test_roundtrip(self) -> None:
        from leakcheck.pii import CustomRule, Settings
        st = Settings()
        st.enabled.discard("phone")
        st.customs.append(CustomRule("病历号", ["病历号", "住院号"], "alpha", "", "", "P0"))
        import tempfile, os
        fd, name = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            st.save(Path(name))
            back = Settings.load(Path(name))
        finally:
            os.unlink(name)
        self.assertNotIn("phone", back.enabled)
        self.assertIn("id_card", back.enabled)
        self.assertEqual(back.customs[0].kind, "病历号")
        self.assertEqual(back.customs[0].level, "P0")

    def test_disabled_type_not_reported(self) -> None:
        from leakcheck.pii import Settings
        st = Settings()
        st.enabled.discard("phone")
        hits = pii.scan("手机 13800138000，身份证号码：110101199003071233", settings=st)
        kinds = {h.kind for h in hits}
        self.assertNotIn("手机号", kinds)
        self.assertIn("身份证", kinds)

    def test_custom_rule_from_triggers(self) -> None:
        from leakcheck.pii import CustomRule, Settings
        st = Settings()
        st.customs.append(CustomRule("病历号", ["病历号", "住院号"], "alpha", "", "", "P0"))
        hits = pii.scan("患者病历号：ZY2026001234", settings=st)
        self.assertTrue(any(h.kind == "病历号" and h.level == "P0" for h in hits))
