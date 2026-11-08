"""④ 评测（五）：规模实验。

回答一个问题：**语料翻十倍，检测延迟会不会跟着翻？**

暴力法（逐条 `find`）的成本是 O(语料 × 明文)，索引法的查询只与明文长度有关，
所以两条曲线会交叉——交叉点之后，索引方案是唯一可行的选择。
"""

from __future__ import annotations

import random
import statistics
import time

from ..config import Config
from ..matcher import Matcher
from ..normalize import normalize

VOCAB = ("甲方 乙方 服务 验收 条款 金额 支付 保密 违约 责任 期限 项目 数据 系统 "
         "平台 开发 交付 测试 维护 文档 报告 阶段 需求 设计 评审 变更 风险 质量 "
         "进度 成本 资源 团队 沟通 会议 记录 确认 签字 盖章 附件 目录 编号").split()


def _make_docs(n: int, chars_each: int, seed: int) -> list[tuple[str, str, str]]:
    """造 n 份内容互不相同的文本（避免高度重复导致倒排项爆炸，扭曲测量）。"""
    rng = random.Random(seed)
    docs = []
    for i in range(n):
        token = f"{rng.randint(0x10000, 0xFFFFFF):06X}"
        lines, length = [], 0
        while length < chars_each:
            words = " ".join(rng.choice(VOCAB) for _ in range(rng.randint(8, 14)))
            line = f"{words}（{token}-{len(lines)}）"
            lines.append(line)
            length += len(line) + 1
        docs.append((f"批量合同_{i:04d}.txt", "contract", "\n".join(lines)))
    return docs


def _median_ms(fn, repeat: int = 5) -> float:
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return round(statistics.median(times), 2)


def _brute_match(docs: list[str], needle: str) -> int:
    hits = 0
    for doc in docs:
        if needle in doc or (len(doc) <= len(needle) and doc in needle):
            hits += 1
    return hits


def scaling(sizes: tuple[int, ...] = (100, 300, 1000), chars_each: int = 1200,
            seed: int = 0) -> list[dict]:
    cfg = Config()
    all_docs = _make_docs(max(sizes), chars_each, seed)
    matcher = Matcher(cfg)
    rows: list[dict] = []
    loaded = 0

    for target in sizes:
        t0 = time.perf_counter()
        for doc_id, tag, text in all_docs[loaded:target]:
            matcher.add(doc_id, text, tag, path=doc_id)
        ingest_ms = (time.perf_counter() - t0) * 1000
        loaded = target

        # 固定净荷：从第一份文档中间切 800 字
        first = normalize(all_docs[0][2], cfg)
        needle = first[len(first) // 3: len(first) // 3 + 800]
        norm_docs = [normalize(d[2], cfg) for d in all_docs[:target]]

        match_ms = _median_ms(lambda: matcher.match(needle, "bench"))
        brute_ms = _median_ms(lambda: _brute_match(norm_docs, needle))

        rows.append({
            "docs": target,
            "chars": sum(len(d) for d in norm_docs),
            "fingerprints": matcher.stats()["fingerprints"],
            "ingest_ms": round(ingest_ms, 1),
            "match_ms": match_ms,
            "brute_ms": brute_ms,
            "speedup": round(brute_ms / match_ms, 1) if match_ms else 0.0,
            "mem_mb": matcher.stats()["est_mem_mb"],
        })
    return rows


def format_table(rows: list[dict]) -> str:
    header = (f"{'文件数':>6}{'语料字符':>10}{'指纹数':>10}{'索引查询ms':>12}"
              f"{'暴力ms':>9}{'加速比':>8}{'内存MB':>8}")
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(f"{r['docs']:>6}{r['chars']:>10}{r['fingerprints']:>10}"
                     f"{r['match_ms']:>12.2f}{r['brute_ms']:>9.2f}"
                     f"{r['speedup']:>8.1f}{r['mem_mb']:>8.2f}")
    return "\n".join(lines)


def run_bench(sizes: tuple[int, ...] = (100, 300, 1000), chars_each: int = 1200) -> int:
    print(f"造语料：{max(sizes)} 份 × 约 {chars_each} 字符，内容互不相同")
    rows = scaling(sizes=sizes, chars_each=chars_each)
    print(format_table(rows))
    print("\n说明：索引查询只与明文长度有关；暴力法随语料线性增长。")
    return 0
