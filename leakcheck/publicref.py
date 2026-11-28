"""公共引用库：名言、诗词、法律通用表述、成语、公文套话。

作用只有一个：命中片段如果属于公共文本，就不算"抄了本地敏感文件"。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 内置语料（可持续扩充；也可由 公共引用.txt 追加）
_BUILTIN = [
    # 名言 / 谚语
    "千里之行始于足下", "不积跬步无以至千里", "路漫漫其修远兮吾将上下而求索",
    "天行健君子以自强不息", "地势坤君子以厚德载物", "己所不欲勿施于人",
    "三人行必有我师焉", "学而不思则罔思而不学则殆", "知之为知之不知为不知",
    "生于忧患死于安乐", "得道多助失道寡助", "工欲善其事必先利其器",
    "君子坦荡荡小人长戚戚", "海纳百川有容乃大", "壁立千仞无欲则刚",
    "纸上得来终觉浅绝知此事要躬行", "不畏浮云遮望眼只缘身在最高层",
    "长风破浪会有时直挂云帆济沧海", "宝剑锋从磨砺出梅花香自苦寒来",
    "业精于勤荒于嬉行成于思毁于随", "天下兴亡匹夫有责",
    "Where there is a will there is a way", "Knowledge is power",
    # 法律 / 合同通用表述
    "平等自愿诚实信用", "双方本着平等自愿诚实信用的原则",
    "依照中华人民共和国民法典及相关法律法规",
    "本合同一式两份甲乙双方各执一份具有同等法律效力",
    "因不可抗力致使合同无法履行的双方互不承担违约责任",
    "本合同未尽事宜由双方协商解决并签订补充协议",
    "争议提交仲裁委员会仲裁", "任何一方违约应承担违约责任并赔偿损失",
    "以上条款经双方确认无误后签字生效",
    # 公文套话
    "高度重视", "统筹推进", "狠抓落实", "压实责任", "形成合力",
    "以问题为导向", "闭环管理", "提质增效", "稳中求进",
    # 成语
    "实事求是", "与时俱进", "因地制宜", "循序渐进", "举一反三",
    "防患未然", "精益求精", "持之以恒", "相辅相成", "水到渠成",
]

_PUNCT = re.compile(r"[\s，。；：、！？,.;:!?\"'“”‘’（）()《》【】\[\]—\-…·]+")


def _norm(text: str) -> str:
    """去空白与标点，只留实字，便于比对。"""
    return _PUNCT.sub("", text)


class PublicRef:
    """公共引用判定。"""

    def __init__(self, extra_path: Path | None = None,
                 min_len: int = 6, extra_lines: list[str] | None = None) -> None:
        self.min_len = min_len
        self.entries: set[str] = {_norm(e) for e in _BUILTIN if len(_norm(e)) >= 8}
        lines: list[str] = list(extra_lines or [])
        if extra_path is not None and Path(extra_path).exists():
            lines += Path(extra_path).read_text(encoding="utf-8").splitlines()
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#") and len(_norm(line)) >= 6:
                self.entries.add(_norm(line))

    def match(self, span: str) -> str | None:
        """命中返回对应的公共条目，否则 None。"""
        norm = _norm(span)
        if len(norm) < self.min_len:
            return None
        for entry in self.entries:
            if entry in norm:
                return entry
            # 片段本身是名言的一部分（覆盖 ≥ 80%）
            if norm in entry and len(norm) / len(entry) >= 0.8:
                return entry
        return None

    def __len__(self) -> int:
        return len(self.entries)


DEFAULT_PUBLIC = """# 公共引用库（一行一条，命中则不计入泄露）
# 放名言、法条、公司固定表述等；至少 6 个字
"""
