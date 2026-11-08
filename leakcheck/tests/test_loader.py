"""目录装载：按路径策略筛文件、读文本、生成唯一 id。"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from leakcheck.loader import collect
from leakcheck.pathpolicy import PathPolicy

ROOT = Path(__file__).parent / "_tmp_loader"


def build_tree() -> Path:
    shutil.rmtree(ROOT, ignore_errors=True)
    (ROOT / "合同").mkdir(parents=True)
    (ROOT / "合同" / "服务合同.txt").write_text("甲方：北京星辰科技有限公司。", encoding="utf-8")
    (ROOT / "src").mkdir()
    (ROOT / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (ROOT / "node_modules" / "pkg").mkdir(parents=True)
    (ROOT / "node_modules" / "pkg" / "合同.js").write_text("console.log(1)", encoding="utf-8")
    (ROOT / "notes.md").write_text("随手记的一点东西。", encoding="utf-8")
    (ROOT / "big.txt").write_text("x" * 100, encoding="utf-8")
    return ROOT


class TestCollect(unittest.TestCase):
    def setUp(self) -> None:
        self.root = build_tree()
        self.policy = PathPolicy()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_only_sensitive_files(self) -> None:
        docs, stats = collect([str(self.root)], self.policy)
        ids = sorted(d[0] for d in docs)
        self.assertEqual(ids, ["src/main.py", "合同/服务合同.txt"])
        self.assertEqual(stats.scanned, 5)
        self.assertEqual(stats.sensitive, 2)
        self.assertEqual(stats.loaded, 2)

    def test_tags_come_from_policy(self) -> None:
        docs, _ = collect([str(self.root)], self.policy)
        tags = {d[0]: d[1] for d in docs}
        self.assertEqual(tags["合同/服务合同.txt"], "contract")
        self.assertEqual(tags["src/main.py"], "code")

    def test_size_limit(self) -> None:
        (self.root / "合同" / "大文件.txt").write_text("敏" * 300, encoding="utf-8")
        docs, stats = collect([str(self.root)], self.policy, max_bytes=50)
        self.assertNotIn("合同/大文件.txt", [d[0] for d in docs])
        self.assertGreaterEqual(stats.skipped_binary, 1)

    def test_single_file_input(self) -> None:
        path = str(self.root / "合同" / "服务合同.txt")
        docs, _ = collect([path], self.policy)
        self.assertEqual(len(docs), 1)
        self.assertTrue(docs[0][0].endswith("服务合同.txt"))

    def test_missing_path(self) -> None:
        docs, stats = collect([str(self.root / "不存在")], self.policy)
        self.assertEqual(docs, [])
        self.assertTrue(stats.errors)

    def test_unsupported_suffix_skipped(self) -> None:
        (self.root / "合同" / "扫描件.docx").write_bytes(b"PK\x03\x04fake")
        docs, stats = collect([str(self.root)], self.policy)
        self.assertEqual(stats.skipped_suffix, 1)
        self.assertNotIn("合同/扫描件.docx", [d[0] for d in docs])

    def test_secret_file_split_by_value(self) -> None:
        """密钥文件按 `KEY=VALUE` 拆成多条：保护粒度是"值"，不是"整行"。"""
        (self.root / "config").mkdir()
        (self.root / "config" / ".env").write_text(
            "DB_HOST=10.20.30.40\nDB_PASSWORD=Zx7Qm2Lp9Kw4\n# 注释行\n", encoding="utf-8")
        docs, _ = collect([str(self.root)], self.policy)
        ids = [d[0] for d in docs]
        self.assertIn("config/.env#DB_PASSWORD", ids)
        unit = next(d for d in docs if d[0] == "config/.env#DB_PASSWORD")
        self.assertEqual(unit[1], "secret")
        self.assertEqual(unit[2], "Zx7Qm2Lp9Kw4")

    def test_secret_file_without_pairs_kept_whole(self) -> None:
        (self.root / "key").mkdir()
        (self.root / "key" / "id_rsa").write_text("MIIEowIBAAKCAQEA...", encoding="utf-8")
        docs, _ = collect([str(self.root)], self.policy)
        ids = [d[0] for d in docs]
        self.assertIn("key/id_rsa", ids)


class TestSecretUnits(unittest.TestCase):
    def test_split(self) -> None:
        from leakcheck.loader import secret_units

        units = secret_units("a.env", "A=12345678\nB: 'abcdefgh'\n#x=1\nbadline\n")
        self.assertEqual([u[2] for u in units], ["12345678", "abcdefgh"])
        self.assertEqual(units[0][0], "a.env#A")

    def test_fallback_to_whole(self) -> None:
        from leakcheck.loader import secret_units

        self.assertEqual(secret_units("k.pem", "-----BEGIN KEY-----"), [("k.pem", "secret", "-----BEGIN KEY-----")])


if __name__ == "__main__":
    unittest.main()
