"""③ 比对层门面：对外只有 ``add()`` 和 ``match()``。

这一层负责把三条支路串起来，并保证**两侧走同一套归一化**：

    add(path)  → 归一化 → 索引（长内容进倒排，短内容进 AC）
    match(text) → 归一化 → 指纹查表 → 对角线聚合 → 校验 → 判定
                        ↘ AC 精确匹配 → 短内容命中

调用方不需要知道 k、w、指纹、对角线这些细节。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .align import RunAccumulator
from .config import Config
from .fingerprint import fingerprint
from .index import Index
from .normalize import normalize
from .score import build_hits
from .types import Hit, Span


class Matcher:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        self.index = Index(self.cfg)

    # ------------------------------------------------------------ 建索引
    def add(self, doc_id: str, text: str, tag: str = "", path: str = "") -> int:
        """登记一份敏感内容，返回写入的指纹数。

        文本会先归一化再入库，所以调用方给原始文本即可。
        ``path`` 是这份内容的来源路径，决策层会用它复核路径规则；不给则用 ``doc_id``。
        """
        if not text:
            raise ValueError("敏感内容不能为空")
        return self.index.add(doc_id, normalize(text, self.cfg), tag, path)

    def add_many(self, items: Iterable[tuple[str, str, str]]) -> int:
        """批量登记：[(id, 标签, 文本), ...]。"""
        return sum(self.add(doc_id, text, tag) for doc_id, tag, text in items)

    # ------------------------------------------------------------ 检测
    def match(self, text: str, payload_id: str = "") -> list[Hit]:
        """判断一条待上传明文是否包含本地敏感内容。"""
        if not text or self.index.doc_count == 0:
            return []
        norm = normalize(text, self.cfg)
        if len(norm) > self.cfg.max_text_chars:
            norm = norm[: self.cfg.max_text_chars]
        if not norm:
            return []

        hits: list[Hit] = []

        # 支路一：长内容（指纹 + 倒排 + 对角线）
        fps = fingerprint(norm, self.cfg)
        if fps:
            acc = RunAccumulator(self.cfg.k, self.cfg.max_gap)
            acc.extend(self.index.hits(fps))
            hits.extend(build_hits(payload_id, norm, acc.finish(), self.index, self.cfg))

        # 支路二：短内容（精确匹配）
        found = self.index.short_find(norm)
        if found:
            hits.extend(self._short_hits(payload_id, norm, found))

        hits.sort(key=lambda h: (h.score, h.longest_run), reverse=True)
        return hits

    def _short_hits(self, payload_id: str, payload: str,
                    found: list[tuple[int, int, int]]) -> list[Hit]:
        by_doc: dict[int, list[tuple[int, int]]] = {}
        for doc_idx, start, end in found:
            by_doc.setdefault(doc_idx, []).append((start, end))

        out: list[Hit] = []
        for doc_idx, ranges in by_doc.items():
            uniq = sorted(set(ranges))
            spans: list[Span] = []
            matched = 0
            for start, end in uniq:
                length = end - start
                if length < self.cfg.short_min_run:
                    continue
                spans.append(Span(doc_start=0, doc_end=length,
                                  payload_start=start, payload_end=end))
                matched += length
            if not spans:
                continue
            longest = max(s.payload_end - s.payload_start for s in spans)
            out.append(Hit(
                doc_id=self.index.name_of(doc_idx),
                payload_id=payload_id,
                detector="short",
                score=1.0,  # 逐字符精确命中，无需打分
                longest_run=longest,
                matched=matched,
                coverage_payload=round(matched / max(len(payload), 1), 3),
                coverage_doc=1.0,
                spans=spans,
                tag=self.index.tag_of(doc_idx),
                path=self.index.path_of(doc_idx),
                reason=f"精确命中短敏感内容（{longest} 字，共 {len(spans)} 处）",
            ))
        return out

    # ------------------------------------------------------------ 元信息
    def stats(self) -> dict:
        return {
            **self.index.stats(),
            "k": self.cfg.k,
            "w": self.cfg.w,
            "guarantee_len": self.cfg.guarantee_len,
        }

    def __len__(self) -> int:
        return self.index.doc_count

    # ------------------------------------------------------------ 持久化
    def save(self, path: str | Path) -> None:
        self.index.save(path)

    @classmethod
    def load(cls, path: str | Path) -> "Matcher":
        index = Index.load(path)
        matcher = cls(index.cfg)
        matcher.index = index
        return matcher
