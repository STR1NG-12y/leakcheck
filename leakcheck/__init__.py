"""leakcheck —— 敏感内容"包含检测"算法。

分层结构（详见 README / 方案_三阶对照.md）：

    ⓪ parse/         T0 解析：任意格式 → Block 流（一个 Block = 一个虚拟文档）
    ① normalize.py   预处理：把文本变成规范形式（两侧共用）
    ② index.py       索引：长内容走指纹倒排，短内容走 AC 自动机
    ③ matcher.py     一阶比对：查表 → 对角线聚合 → 校验 → 判定
    ④ skeleton.py    二阶比对：骨架（去虚词 + 数字归一）走同一套算法
    ⑤ semantic/      三阶比对：BM25 粗排 + banded Smith-Waterman 局部验证
    ⑥ cascade.py     三阶级联门面：A/B/C 五级证据
    ⑦ eval/          评测：数据集 / P·R·F1 / 基线

对外三组入口：

    Matcher.add() / Matcher.match()      → 只要一阶（最快）
    Cascade.add() / Cascade.scan()       → 三阶级联（抓改写）
    parse(path)                          → 任意格式 → Block
"""

from .cascade import Cascade, Finding, ScanResult
from .config import Config
from .decision import Judge, Verdict
from .matcher import Matcher
from .parse import Block, ParseResult, parse
from .skeleton import SkeletonIndex, skeletonize
from .types import Hit, Span

__version__ = "2.0.0"
__all__ = [
    "Matcher", "Config", "Hit", "Span", "Judge", "Verdict",
    "Cascade", "Finding", "ScanResult",
    "parse", "Block", "ParseResult",
    "SkeletonIndex", "skeletonize",
]
