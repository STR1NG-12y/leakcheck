"""leakcheck —— 敏感内容"包含检测"算法。

四层结构（详见 README）：

    ① normalize.py   预处理：把文本变成规范形式（两侧共用）
    ② index.py       索引：长内容走指纹倒排，短内容走 AC 自动机
    ③ matcher.py     比对：查表 → 对角线聚合 → 校验 → 判定
    ④ eval/          评测：数据集 / P·R·F1 / 基线

对外只有两个动作：``add()`` 建索引，``match()`` 判一条待上传明文。
"""

from .config import Config
from .decision import Judge, Verdict
from .matcher import Matcher
from .types import Hit, Span

__version__ = "1.1.0"
__all__ = ["Matcher", "Config", "Hit", "Span", "Judge", "Verdict"]
