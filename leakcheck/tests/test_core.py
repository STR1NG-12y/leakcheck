"""核心算法：归一化 / 指纹 / AC / 聚合 / 校验 / 判定。"""

from __future__ import annotations

import random
import unittest

from leakcheck.ahocorasick import AhoCorasick
from leakcheck.align import RunAccumulator, collect_runs
from leakcheck.config import Config
from leakcheck.fingerprint import codepoints, fingerprint, rolling_hashes, winnow
from leakcheck.normalize import normalize
from leakcheck.verify import expand, verify_cluster

MASK = (1 << 64) - 1
BASE = 0x100000001B3


def brute(codes, k):
    out = []
    for i in range(len(codes) - k + 1):
        h = 0
        for j in range(k):
            h = (h * BASE + codes[i + j]) & MASK
        out.append(h)
    return out


class TestNormalize(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config()

    def test_collapses_whitespace(self) -> None:
        # 注意 NFKC 会把全角冒号折成半角，这也是"格式差异归一化"的一部分
        self.assertEqual(normalize("合同   金额\n\n：  壹万", self.cfg), "合同 金额 : 壹万")

    def test_drops_zero_width(self) -> None:
        self.assertEqual(normalize("合\u200b同\u200c金\ufeff额", self.cfg), "合同金额")

    def test_nfkc_fullwidth(self) -> None:
        self.assertEqual(normalize("ＡＢＣ１２３", self.cfg), "ABC123")

    def test_idempotent(self) -> None:
        text = " 甲乙   丙\u3000丁 \u200b"
        once = normalize(text, self.cfg)
        self.assertEqual(normalize(once, self.cfg), once)

    def test_lower_option(self) -> None:
        cfg = Config(lower=True)
        self.assertEqual(normalize("AbC 合同", cfg), "abc 合同")


class TestFingerprint(unittest.TestCase):
    def test_rolling_matches_bruteforce(self) -> None:
        rnd = random.Random(7)
        for _ in range(60):
            n = rnd.randint(0, 40)
            k = rnd.randint(1, 9)
            codes = [rnd.randint(1, 200) for _ in range(n)]
            self.assertEqual(rolling_hashes(codes, k), brute(codes, k))

    def test_too_short(self) -> None:
        self.assertEqual(rolling_hashes([1, 2], 5), [])
        self.assertEqual(fingerprint("短", Config()), [])

    def test_winnow_positions(self) -> None:
        rnd = random.Random(1)
        hashes = [rnd.randrange(1 << 32) for _ in range(300)]
        picked = winnow(hashes, 8)
        positions = [p for p, _ in picked]
        self.assertEqual(positions, sorted(set(positions)))
        self.assertTrue(all(h == hashes[p] for p, h in picked))

    def test_density_close_to_theory(self) -> None:
        cfg = Config(k=16, w=8)
        text = "敏感内容比对算法验证" * 400
        fps = fingerprint(text, cfg)
        self.assertAlmostEqual(len(fps) / len(text), cfg.density, delta=0.05)

    def test_guarantee_no_miss(self) -> None:
        """任意 >= k+w-1 的公共子串必须共享指纹（本算法的核心承诺）。"""
        rnd = random.Random(3)
        cfg = Config(k=8, w=5, min_run=12)
        text = "".join(rnd.choice("abcdefgh") for _ in range(400))
        doc_hashes = {h for _, h in fingerprint(text, cfg)}
        need = cfg.guarantee_len  # 12
        checked = 0
        for start in range(0, len(text) - need):
            for length in (need, need + 9, need + 50):
                if start + length > len(text):
                    continue
                sub = {h for _, h in fingerprint(text[start:start + length], cfg)}
                self.assertTrue(sub & doc_hashes, f"漏检 start={start} len={length}")
                checked += 1
        self.assertGreater(checked, 500)


class TestAhoCorasick(unittest.TestCase):
    def test_multi_pattern(self) -> None:
        ac = AhoCorasick()
        for w in ("he", "she", "his", "hers"):
            ac.add(w, w.upper())
        ac.build()
        found = {(s, w) for s, _e, w, _v in ac.find("ushers")}
        self.assertIn((1, "she"), found)
        self.assertIn((2, "he"), found)
        self.assertIn((2, "hers"), found)

    def test_overlap(self) -> None:
        ac = AhoCorasick()
        ac.add("aba", 1)
        ac.add("bab", 2)
        ac.build()
        self.assertEqual(sorted(w for _s, _e, w, _v in ac.find("ababa")),
                         ["aba", "aba", "bab"])

    def test_empty(self) -> None:
        ac = AhoCorasick()
        ac.build()
        self.assertEqual(list(ac.find("任意文本")), [])


class TestAlignment(unittest.TestCase):
    def test_one_diagonal_one_run(self) -> None:
        hits = [(0, 100 + p, p) for p in range(0, 60, 6)]
        clusters = collect_runs(hits, k=16, max_gap=64)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].doc_start, 100)
        self.assertGreaterEqual(clusters[0].length, 50)

    def test_different_diagonals_split(self) -> None:
        self.assertEqual(len(collect_runs([(0, 100, 0), (0, 900, 500)], 16, 64)), 2)

    def test_gap_splits(self) -> None:
        hits = [(0, 10, 0), (0, 400, 380)]
        self.assertEqual(len(collect_runs(hits, 16, 64)), 2)

    def test_streaming_accumulator(self) -> None:
        acc = RunAccumulator(k=16, max_gap=64)
        acc.extend([(0, 100, 0), (0, 106, 6)])
        acc.add(0, 112, 12)
        clusters = acc.finish()
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].hits, 3)


class TestVerify(unittest.TestCase):
    def test_expand_boundaries(self) -> None:
        doc = "AAA" + "敏感内容" * 10 + "BBB"
        payload = "前缀" + "敏感内容" * 10 + "后缀"
        span = expand(doc, payload, 3, 2, k=8)
        assert span is not None
        self.assertEqual(doc[span.doc_start:span.doc_end],
                         payload[span.payload_start:span.payload_end])
        self.assertEqual(span.length, 40)

    def test_collision_rejected(self) -> None:
        doc = "这是一段完全无关的文档内容，用来验证碰撞检测机制。"
        payload = "这是另一段毫不相干的文字，哈希碰巧一样也不行。"
        clusters = collect_runs([(0, 2, 2), (0, 7, 7), (0, 12, 12)], k=4, max_gap=64)
        self.assertTrue(clusters)
        self.assertIsNone(verify_cluster(clusters[0], doc, payload, k=4))


if __name__ == "__main__":
    unittest.main()
