"""④ 评测（三）：基线方法。

``BruteForce``
    对每条明文，逐个敏感内容做子串包含判断（双向：整篇上传 / 片段粘贴）。
    检测能力其实不错，但成本是 O(语料 × 明文)，语料一大就不可用——用来对照**延迟**。

``SimHash``
    整篇 64 位 simhash + 汉明距离。快，但只能回答"像不像"，
    短粘贴会被长文稀释，而且无法定位片段——用来对照**检出能力**。
"""

from __future__ import annotations

import hashlib

from ..config import Config
from ..normalize import normalize


class BruteForce:
    name = "bruteforce"

    def __init__(self, units: list[tuple[str, str]], cfg: Config) -> None:
        self.units = [(doc_id, normalize(text, cfg)) for doc_id, text in units]

    def match(self, text: str, cfg: Config) -> list[str]:
        needle = normalize(text, cfg)
        if not needle:
            return []
        hits = []
        for doc_id, doc in self.units:
            if needle in doc or (len(doc) <= len(needle) and doc in needle):
                hits.append(doc_id)
        return hits


class SimHash:
    name = "simhash"

    def __init__(self, units: list[tuple[str, str]], cfg: Config,
                 bits: int = 64, threshold: int = 3) -> None:
        self.bits = bits
        self.threshold = threshold
        self.units = [(doc_id, self._hash(normalize(text, cfg))) for doc_id, text in units]

    def _hash(self, text: str) -> int:
        if not text:
            return 0
        acc = [0] * self.bits
        grams = [text[i:i + 3] for i in range(max(len(text) - 2, 1))]
        for gram in grams:
            h = int.from_bytes(
                hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "big")
            for i in range(self.bits):
                acc[i] += 1 if (h >> i) & 1 else -1
        out = 0
        for i in range(self.bits):
            if acc[i] > 0:
                out |= 1 << i
        return out

    def match(self, text: str, cfg: Config) -> list[str]:
        target = self._hash(normalize(text, cfg))
        best, best_dist = None, self.bits + 1
        for doc_id, sig in self.units:
            dist = bin(sig ^ target).count("1")
            if dist < best_dist:
                best, best_dist = doc_id, dist
        return [best] if best is not None and best_dist <= self.threshold else []
