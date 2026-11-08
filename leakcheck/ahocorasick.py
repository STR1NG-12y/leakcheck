"""②b 精确支路：Aho-Corasick 多模式匹配。

为什么需要这条支路
------------------
长度 < k+w-1 的敏感内容（短口令、身份证号、项目代号）用指纹抓不到：
窗口都凑不满，或者凑满了也只有一个指纹，形成不了证据链。

但这类内容有个特点：**必须逐字符精确**。所以换成 AC 自动机：
建一次 trie，扫一遍文本就能找出所有出现位置，复杂度 O(文本长度 + 命中数)，
与模式数量无关。

这也是本框架相比"正则识别密钥"的改进：不需要猜"什么像密钥"，
上游给什么就保护什么，按长度自动分流。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterator


class AhoCorasick:
    """经典 AC 自动机：goto 表 + 失配指针 + 输出合并。"""

    def __init__(self) -> None:
        self._goto: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._out: list[list[tuple[str, Any]]] = [[]]
        self._built = False

    # ------------------------------------------------------------ 构建
    def add(self, word: str, value: Any) -> None:
        if not word:
            return
        state = 0
        for ch in word:
            nxt = self._goto[state].get(ch)
            if nxt is None:
                self._goto.append({})
                self._fail.append(0)
                self._out.append([])
                nxt = len(self._goto) - 1
                self._goto[state][ch] = nxt
            state = nxt
        self._out[state].append((word, value))
        self._built = False

    def build(self) -> None:
        queue: deque[int] = deque()
        for child in self._goto[0].values():
            self._fail[child] = 0
            queue.append(child)
        while queue:
            state = queue.popleft()
            for ch, child in self._goto[state].items():
                f = self._fail[state]
                while f and ch not in self._goto[f]:
                    f = self._fail[f]
                self._fail[child] = self._goto[f].get(ch, 0)
                # 失配链上的输出直接并进来，扫描时不用再爬链
                self._out[child].extend(self._out[self._fail[child]])
                queue.append(child)
        self._built = True

    # ------------------------------------------------------------ 查询
    def find(self, text: str) -> Iterator[tuple[int, int, str, Any]]:
        """产出 (start, end, 模式串, 附加值)。"""
        if not self._built:
            self.build()
        state = 0
        for i, ch in enumerate(text):
            while state and ch not in self._goto[state]:
                state = self._fail[state]
            state = self._goto[state].get(ch, 0)
            for word, value in self._out[state]:
                yield i - len(word) + 1, i + 1, word, value

    def __len__(self) -> int:
        return sum(len(o) for o in self._out)
