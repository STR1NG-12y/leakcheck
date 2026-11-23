"""BM25：零依赖的稀疏检索，用作三阶的粗排。

中文用「字符二元组」当词，英文/数字按词切分——不需要分词模型，
对"领域术语重合"这件事已经足够敏感。
"""

from __future__ import annotations

import math
import re
from collections import Counter

_LATIN = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]*")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """字符二元组 + 英文整词。"""
    tokens: list[str] = []
    for word in _LATIN.findall(text):
        tokens.append(word.lower())
    chars = _CJK.findall(text)
    for i in range(len(chars) - 1):
        tokens.append(chars[i] + chars[i + 1])
    if len(chars) == 1:
        tokens.append(chars[0])
    return tokens


class BM25:
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs: list[str] = []
        self._tf: list[Counter] = []
        self._len: list[int] = []
        self._df: Counter = Counter()
        self._avgdl: float = 0.0

    def add(self, doc_id: str, text: str) -> None:
        tokens = tokenize(text)
        self._docs.append(doc_id)
        self._tf.append(Counter(tokens))
        self._len.append(len(tokens))
        for term in set(tokens):
            self._df[term] += 1
        self._avgdl = sum(self._len) / max(len(self._len), 1)

    @property
    def doc_count(self) -> int:
        return len(self._docs)

    def _idf(self, term: str) -> float:
        n = len(self._docs)
        df = self._df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, k: int = 20) -> list[tuple[str, float]]:
        if not self._docs:
            return []
        q_terms = tokenize(query)
        if not q_terms:
            return []
        scores: list[tuple[str, float]] = []
        avgdl = self._avgdl or 1.0
        for i, doc_id in enumerate(self._docs):
            tf = self._tf[i]
            dl = self._len[i] or 1
            score = 0.0
            for term in q_terms:
                f = tf.get(term, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * dl / avgdl)
                score += self._idf(term) * (f * (self.k1 + 1)) / denom
            if score > 0:
                scores.append((doc_id, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]
