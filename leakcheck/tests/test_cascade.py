"""新增模块的最小自测：解析 / 骨架 / 语义 / 级联。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from leakcheck.cascade import Cascade
from leakcheck.config import Config
from leakcheck.normalize import normalize
from leakcheck.parse import parse
from leakcheck.parse.blocks import Block, merge_blocks, need_ocr
from leakcheck.parse.layout import strip_header_footer
from leakcheck.semantic.bm25 import BM25, tokenize
from leakcheck.semantic.sw import sw_align
from leakcheck.skeleton import SkeletonIndex, cn_to_int, lcs_ratio, skeletonize

CONTRACT = (
    "第一条 合作内容：乙方为甲方提供数据中台建设与运维服务，服务期限为三十六个月。"
    "第二条 合同金额：本合同总金额为人民币壹仟贰佰捌拾万元整，分四期支付。"
    "第三条 保密条款：双方对合作中知悉的商业秘密、技术资料负有保密义务，期限五年。"
)


class TestSkeleton(unittest.TestCase):
    def test_stopwords_removed(self) -> None:
        sk = skeletonize("本合同的总金额为一个很大的数字")
        self.assertNotIn("的", sk.text)
        self.assertIn("合同", sk.text)

    def test_arabic_number_normalized(self) -> None:
        a = skeletonize("总金额为12800000元")
        b = skeletonize("总金额为12800000元")
        self.assertEqual(a.text, b.text)
        self.assertIn("[NUM:12800000]", a.text)

    def test_chinese_number_equivalent(self) -> None:
        a = skeletonize("服务期限三十六个月")
        self.assertIn("[NUM:36", a.text)

    def test_cn_to_int(self) -> None:
        self.assertEqual(cn_to_int("三十六"), 36)
        self.assertEqual(cn_to_int("壹仟贰佰捌拾万"), 12800000)
        self.assertEqual(cn_to_int("一万二千"), 12000)
        self.assertIsNone(cn_to_int("合同"))

    def test_spans_map_back(self) -> None:
        text = normalize("甲方支付尾款", Config())
        sk = skeletonize(text)
        start, end = sk.orig_range(0, len(sk.text))
        self.assertGreaterEqual(start, 0)
        self.assertLessEqual(end, len(text))
        self.assertGreater(end, start)

    def test_lcs_ratio(self) -> None:
        self.assertAlmostEqual(lcs_ratio("abcdef", "abcdef"), 1.0)
        self.assertLess(lcs_ratio("abcdef", "xyz"), 0.2)


class TestSkeletonUnits(unittest.TestCase):
    def test_year_month_equivalent(self) -> None:
        a = skeletonize("服务期限三年")
        b = skeletonize("服务期限36个月")
        self.assertEqual(a.text, b.text, "三年 和 36个月 应归一到同一个 token")

    def test_week_day_equivalent(self) -> None:
        self.assertEqual(skeletonize("工期两周").text, skeletonize("工期14日").text)

    def test_english_stopwords_removed(self) -> None:
        sk = skeletonize("party b shall provide the service")
        self.assertNotIn("the", sk.text.split())
        self.assertIn("provide", sk.text)

    def test_decimal_kept(self) -> None:
        a = skeletonize("金额1.5万元")
        self.assertEqual(a.text, skeletonize("金额1.50万元").text, "1.5 与 1.50 应归一")
        self.assertIn("[NUM:15000]", a.text)

    def test_decimal_differs_from_integer(self) -> None:
        self.assertNotEqual(skeletonize("增长12.5%").text,
                            skeletonize("增长125%").text)

    def test_negative_number(self) -> None:
        self.assertIn("[NUM:-5]", skeletonize("温度为-5度").text)

    def test_hyphen_in_code_is_not_sign(self) -> None:
        self.assertIn("A-[NUM:15]", skeletonize("编号A-15").text)

    def test_thousands_separator(self) -> None:
        self.assertEqual(skeletonize("1,280元").text, skeletonize("1280元").text)

    def test_chinese_decimal(self) -> None:
        self.assertIn("[NUM:15000]", skeletonize("一点五万元").text)

    def test_year_is_not_duration(self) -> None:
        # 2024年 是日期，不能按"年→12个月"换算成 24288 月
        self.assertIn("[NUM:2024年]", skeletonize("签署于2024年").text)

    def test_leading_zeros(self) -> None:
        self.assertIn("[NUM:7]", skeletonize("编号007").text)
        self.assertIn("[NUM:0.5]", skeletonize("比例00.50").text)

    def test_span_covers_unit(self) -> None:
        text = normalize("服务期限三年", Config())
        sk = skeletonize(text)
        start, end = sk.orig_range(0, len(sk.text))
        self.assertLessEqual(end, len(text))
        self.assertGreater(end, start)


class TestVectorScreen(unittest.TestCase):
    def test_hashing_backend_ranks_related(self) -> None:
        from leakcheck.vector_screen import HashingEmbedder, VectorScreen

        vs = VectorScreen(embedder=HashingEmbedder(), min_score=0.0)
        vs.add("合同A", CONTRACT, "contract")
        vs.add("无关", "帮我写一首关于秋天的七言绝句，要求押平声韵。", "other")
        hits = vs.search("合同金额是多少", top_k=2)
        self.assertTrue(hits)
        # 降级后端是哈希分桶，偶尔会并列；只要"相关"排在"无关"前面即可
        scores = {h.doc_id: h.score for h in hits}
        self.assertIn("合同A", scores)
        if "无关" in scores:
            self.assertGreater(scores["合同A"], scores["无关"])

    def test_no_hits_below_threshold(self) -> None:
        from leakcheck.vector_screen import HashingEmbedder, VectorScreen

        vs = VectorScreen(embedder=HashingEmbedder(), min_score=0.99)
        vs.add("合同A", CONTRACT)
        self.assertEqual(vs.search("完全无关的一句话", top_k=3), [])

    def test_chunk_text(self) -> None:
        from leakcheck.vector_screen import chunk_text

        chunks = chunk_text("字" * 600)
        self.assertTrue(chunks)
        self.assertTrue(all(len(c) <= 256 for c in chunks))
        self.assertEqual(chunk_text(""), [])

    def test_backend_available(self) -> None:
        from leakcheck.vector_screen import available_backends

        state = available_backends()
        self.assertIn("hashing", state)
        self.assertTrue(state["hashing"])


class TestSkeletonSpans(unittest.TestCase):
    """数字 token 占多个字符时，spans 必须和骨架串一一对应。"""

    def test_spans_length_matches_text(self) -> None:
        sk = skeletonize(normalize("第3条 乙方为甲方提供数据中台建设与运维服务", Config()))
        self.assertEqual(len(sk.text), len(sk.spans))

    def test_orig_range_with_number_in_middle(self) -> None:
        norm = normalize("第3条 乙方为甲方提供数据中台", Config())
        sk = skeletonize(norm)
        start, end = sk.orig_range(0, len(sk.text))
        self.assertEqual(norm[start:end], norm)

    def test_orig_range_covers_number_token(self) -> None:
        norm = normalize("服务期限为三十六个月", Config())
        sk = skeletonize(norm)
        idx = sk.text.index("[NUM:36月]")
        start, end = sk.orig_range(idx, idx + len("[NUM:36月]"))
        self.assertEqual(norm[start:end], "三十六个月")


class TestSkeletonIndex(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config()
        self.sk = SkeletonIndex(self.cfg)
        self.sk.add("合同A", CONTRACT, "contract")

    def test_verbatim_hits(self) -> None:
        hits = self.sk.scan(normalize("第二条 合同金额：本合同总金额为人民币壹仟贰佰捌拾万元整", self.cfg))
        self.assertTrue(hits)
        self.assertEqual(hits[0].detector, "skeleton")

    def test_paraphrase_hits(self) -> None:
        # 同义词替换但句式保留：骨架仍应命中
        rewrite = "乙方为甲方提供数据中台建设和运维服务，服务期限是三年"
        hits = self.sk.scan(normalize(rewrite, self.cfg))
        self.assertTrue(hits, "同义改写应该被骨架层命中")

    def test_heavy_rewrite_falls_through(self) -> None:
        # 改写太彻底（骨架只剩 6 字）时，骨架层应当放行，交给三阶
        heavy = "乙方要给甲方做数据中台的建设，另外还要负责日常的运维工作，合作时长是三年"
        self.assertFalse(self.sk.scan(normalize(heavy, self.cfg)))

    def test_unrelated_misses(self) -> None:
        hits = self.sk.scan(normalize("帮我写一首关于秋天的七言绝句，要求押平声韵", self.cfg))
        self.assertFalse(hits)


class TestBM25(unittest.TestCase):
    def test_tokenize_cjk_bigrams(self) -> None:
        self.assertIn("合同", tokenize("合同金额"))

    def test_search_ranks_relevant(self) -> None:
        bm = BM25()
        bm.add("合同A", CONTRACT)
        bm.add("无关", "帮我写一首关于秋天的七言绝句，要求押平声韵。")
        results = bm.search("合同金额是多少", k=2)
        self.assertEqual(results[0][0], "合同A")


class TestSW(unittest.TestCase):
    def test_exact_alignment(self) -> None:
        r = sw_align("abcdef", "xxabcdefyy", band=8)
        self.assertGreaterEqual(r.length, 6)

    def test_no_alignment(self) -> None:
        r = sw_align("abcdef", "uvwxyz", band=4)
        self.assertEqual(r.length, 0)


class TestParse(unittest.TestCase):
    def setUp(self) -> None:
        # 注意：不能用系统临时目录（受限环境会拒绝写入），放在 tests/_tmp 下
        self.tmp = Path(__file__).parent / "_tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_txt_blocks(self) -> None:
        path = self.tmp / "a.txt"
        path.write_text("第一段内容\n\n第二段内容", encoding="utf-8")
        result = parse(path)
        self.assertTrue(result.blocks)
        self.assertEqual(result.blocks[0].kind, "paragraph")

    def test_csv_rows(self) -> None:
        path = self.tmp / "a.csv"
        path.write_text("姓名,部门\n张三,研发\n李四,财务", encoding="utf-8")
        result = parse(path)
        self.assertEqual(result.blocks[0].kind, "table_row")
        self.assertEqual(len(result.blocks), 3)

    def test_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            parse(Path("不存在的文件.txt"))

    def test_need_ocr_on_tiny_text(self) -> None:
        self.assertTrue(need_ocr("短", [], 3))
        self.assertFalse(need_ocr("长" * 500, [], 1))

    def test_merge_small_blocks(self) -> None:
        blocks = [Block(text="短句一", bbox=(0, 0, 10, 10), vdoc=0),
                  Block(text="短句二", bbox=(0, 12, 10, 22), vdoc=1)]
        merged = merge_blocks(blocks)
        self.assertEqual(len(merged), 1)

    def test_strip_header_footer(self) -> None:
        blocks = [Block(text="XX公司 机密", page=p, bbox=(0, 5, 10, 15)) for p in (1, 2, 3)]
        blocks.append(Block(text="正文内容在这里", page=1, bbox=(0, 100, 10, 110)))
        kept, removed = strip_header_footer(blocks)
        self.assertEqual(removed, 3)
        self.assertEqual(len(kept), 1)


class TestCascade(unittest.TestCase):
    def setUp(self) -> None:
        self.casc = Cascade()
        self.casc.add("合同A", CONTRACT, "contract")

    def test_verbatim_is_level_a(self) -> None:
        result = self.casc.scan("第二条 合同金额：本合同总金额为人民币壹仟贰佰捌拾万元整，分四期支付。")
        self.assertEqual(result.top_level, "A")

    def test_paraphrase_is_level_b(self) -> None:
        result = self.casc.scan("乙方为甲方提供数据中台建设和运维服务，服务期限是三年")
        self.assertEqual(result.top_level, "B")

    def test_heavy_rewrite_falls_through(self) -> None:
        # 三阶（BM25+SW）已按实测结论删除：这种改写现在直接放行，
        # 需要覆盖时用 vector_screen（向量，产出线索而非证据）
        result = self.casc.scan(
            "乙方要给甲方做数据中台的建设，另外还要负责日常的运维工作，合作时长是三年")
        self.assertEqual(result.findings, [])

    def test_unrelated_is_empty(self) -> None:
        result = self.casc.scan("帮我写一首关于秋天的七言绝句，要求押平声韵。")
        self.assertFalse(result.findings)

    def test_finding_serializable(self) -> None:
        result = self.casc.scan("第二条 合同金额：本合同总金额为人民币壹仟贰佰捌拾万元整")
        payload = json.dumps([f.to_dict() for f in result.findings], ensure_ascii=False)
        self.assertIn("合同A", payload)


if __name__ == "__main__":
    unittest.main()
