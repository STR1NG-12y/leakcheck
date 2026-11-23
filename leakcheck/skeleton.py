"""二阶：骨架对照（零模型）。

思路
----
改写会换词、换语序，但**实词骨架**往往还在：

    原文：丙方须在三十六个月内完成数据中台一期建设，验收合格后甲方支付尾款。
    改写：乙方要在三年内把数据平台首期工程做完，验收通过后再结清余款。

骨架 = 原文 去掉虚词 + 数字归一 之后的字符串：

    原文：丙方三十六个月完成数据中台一期建设验收合格甲方支付尾款
    改写：乙方三年数据平台首期工程验收通过结清余款

然后**对骨架字符串跑与一阶完全相同的算法**（k=sk_k, w=sk_w），
只是把位置映射回原文再做一次 LCS 校验。

为什么保留字符而不是 token：
- 不需要分词模型（零依赖、无分词错误）
- 直接复用现有的 fingerprint / Index / RunAccumulator
- 只要维护"骨架字符 → 原文区间"的映射表即可
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .align import RunAccumulator
from .config import Config
from .fingerprint import fingerprint
from .index import Index
from .normalize import normalize
from .types import Hit, Span

# ---------------------------------------------------------------- 虚词表
STOPWORDS: frozenset[str] = frozenset("""
的 了 在 是 和 与 或 为 被 把 将 对 对于 关于 以及 及其 且 而 也 都 就 又 还 并 并且
之 以 于 则 因 所以 因为 如果 虽然 但是 然而 因此 由于 通过 根据 按照 依照 依据 经过
从 由 向 至 到 给 让 使 得 地 着 过 吗 呢 吧 啊 呀 哦 嗯 这 那 此 该 其 它 他 她 们
我 你 您 我们 你们 他们 自己 本 各 每 某 该 上述 下列 如下 如下所示 其中 之一 等等
等 及 或者 还是 不是 没有 可以 能够 应该 需要 必须 应当 一条 一款 一项 若干 有关 相关
上述 前述 以下 以上 之前 之后 之间 之内 之外 方面 情况 问题 内容 部分 方式 进行 加以
""".split())

#: 英文虚词（改写时经常增减，去掉可提升骨架重合度）
EN_STOPWORDS: frozenset[str] = frozenset("""
a an the and or but if then than that this these those of to in on at by for with from
as is are was were be been being do does did have has had will would can could shall should
may might must not no nor so such we you they he she it i our your their its
""".split())

#: 时间单位归一：把不同写法换算到同一基准（月 / 日 / 分）
_UNIT_TABLE: dict[str, tuple[int, str]] = {
    "年": (12, "月"), "个月": (1, "月"), "月": (1, "月"), "月份": (1, "月"),
    "周": (7, "日"), "星期": (7, "日"), "天": (1, "日"), "日": (1, "日"), "号": (1, "日"),
    "小时": (60, "分"), "时": (60, "分"), "分钟": (1, "分"),
}

# ---------------------------------------------------------------- 数字归一
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2,
             "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6,
             "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9}
_CN_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_BIG = {"万": 10000, "萬": 10000, "亿": 100000000, "億": 100000000}


def cn_to_int(text: str) -> int | None:
    """中文数字（含大写金额）→ 整数。失败返回 None。"""
    total = 0
    section = 0
    number = 0
    seen = False
    for ch in text:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
            seen = True
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if number == 0:
                number = 1          # 十六 → 16
            section += number * unit
            number = 0
            seen = True
        elif ch in _CN_BIG:
            section += number
            total += (section or 1) * _CN_BIG[ch]
            section = 0
            number = 0
            seen = True
        else:
            return None
    return total + section + number if seen else None


@dataclass
class Skeleton:
    text: str
    spans: list[tuple[int, int]] = field(default_factory=list)   # 骨架字符 → 原文 [start, len)

    def orig_range(self, start: int, end: int) -> tuple[int, int]:
        """骨架区间 [start, end) → 原文区间。"""
        if not self.spans or start >= len(self.spans):
            return (0, 0)
        end = min(end, len(self.spans))
        if end <= start:
            return (0, 0)
        o_start = self.spans[start][0]
        last = self.spans[end - 1]
        return (o_start, last[0] + last[1])


def _is_cn_num(ch: str) -> bool:
    return ch in _CN_DIGIT or ch in _CN_UNIT or ch in _CN_BIG


def _match_unit(text: str, pos: int) -> tuple[int, str, int]:
    """匹配时间单位，返回 (换算倍数, 基准单位, 消耗字符数)。

    把"三年""三十六个月""36 个月"统一成同一个 token，改写里最常见的
    单位换写就能对上。
    """
    for size in (2, 1):
        unit = text[pos:pos + size]
        if len(unit) < size:          # 文本末尾截断，跳过
            continue
        if unit in _UNIT_TABLE:
            mult, base = _UNIT_TABLE[unit]
            return mult, base, len(unit)
    return 1, "", 0


#: 中文大数单位（只对阿拉伯数字生效，避免"五万"被重复放大）
_MAGNITUDE = {"万": 10000, "萬": 10000, "亿": 100000000, "億": 100000000}

#: 允许出现在数字前的负号
_SIGNS = "-−－"


def _scale(value: str, mult: float) -> str:
    """按倍数放大，整数就写成整数。"""
    try:
        v = float(value) * mult
    except ValueError:
        return value
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _number_token(value: str, text: str, pos: int) -> tuple[str, int]:
    """数字 + 可选单位 → ``[NUM:值]``，返回 (token, 额外消耗的字符数)。

    先看时间单位（三年→36月），再看大数单位（1.5万→15000）。
    """
    mult, base, used = _match_unit(text, pos)
    if base:
        # 4 位年份（1900~2100）跟"年"在一起时是日期，不是时长，别换算成月
        if (base == "月" and used == 1 and text[pos:pos + 1] == "年"
                and "." not in value and value.isdigit()
                and 1900 <= int(value) <= 2100):
            return f"[NUM:{value}年]", used
        return f"[NUM:{_scale(value, mult)}{base}]", used
    if pos < len(text) and text[pos] in _MAGNITUDE:
        return f"[NUM:{_scale(value, _MAGNITUDE[text[pos]])}]", 1
    return f"[NUM:{value}]", 0


def _sign_allowed(text: str, i: int) -> bool:
    """连字符何时算负号：前面是空格/标点/中文才允许，A-15、2024-11 里的不算。"""
    if i == 0:
        return True
    prev = text[i - 1]
    if prev.isdigit():
        return False
    if prev.isascii() and (prev.isalnum() or prev == "_"):
        return False
    return True


def _scan_arabic(text: str, i: int) -> tuple[str, int]:
    """解析阿拉伯数字：支持负号、小数、千分位。返回 (规范化数值, 结束位置)。"""
    n = len(text)
    j = i
    sign = ""
    if (text[j] in _SIGNS and _sign_allowed(text, j)
            and j + 1 < n and text[j + 1].isdigit()):
        sign = "-"
        j += 1

    buf: list[str] = []
    dot_seen = False
    while j < n:
        ch = text[j]
        if ch.isdigit():
            buf.append(ch)
            j += 1
        elif ch == "," and buf and j + 1 < n and text[j + 1].isdigit():
            j += 1                                   # 千分位，直接丢掉
        elif (ch == "." and not dot_seen and buf
              and j + 1 < n and text[j + 1].isdigit()):
            buf.append(".")
            dot_seen = True
            j += 1
        else:
            break

    if not buf or set(buf) == {"."}:
        return "", i
    value = "".join(buf)
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    whole, _, frac = value.partition(".")
    whole = whole.lstrip("0") or "0"
    value = whole + (f".{frac}" if frac else "")
    return sign + value, j


def _scan_chinese(text: str, i: int) -> tuple[str, int]:
    """解析中文数字：支持"一点五"这种小数写法。"""
    n = len(text)
    j = i
    while j < n and _is_cn_num(text[j]):
        j += 1
    run = text[i:j]

    # 一点五 / 三点一四
    if j + 1 < n and text[j] == "点" and text[j + 1] in _CN_DIGIT:
        k = j + 1
        frac: list[str] = []
        while k < n and text[k] in _CN_DIGIT:
            frac.append(str(_CN_DIGIT[text[k]]))
            k += 1
        whole = cn_to_int(run) if run else 0
        if whole is None:
            return "", i
        value = f"{whole}.{''.join(frac)}".rstrip("0").rstrip(".")
        return value, k

    value = cn_to_int(run)
    if value is None:
        return "", i
    return str(value), j


def _skip_word(text: str, pos: int) -> int:
    """英文虚词：按词边界匹配，返回消耗的字符数（0 表示不是虚词）。"""
    n = len(text)
    j = pos
    while j < n and (text[j].isascii() and (text[j].isalnum() or text[j] in "_-")):
        j += 1
    if j == pos:
        return 0
    word = text[pos:j].lower()
    return j - pos if word in EN_STOPWORDS else 0


def skeletonize(text: str) -> Skeleton:
    """生成骨架 + 字符级位置映射。**入参必须是已归一化的文本。**"""
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)

    while i < n:
        ch = text[i]

        # 1) 阿拉伯数字（含负号/小数/千分位）→ [NUM:v]
        if ch.isdigit() or (ch in _SIGNS and _sign_allowed(text, i)
                            and i + 1 < n and text[i + 1].isdigit()):
            value, j = _scan_arabic(text, i)
            if value:
                token, used = _number_token(value, text, j)
                out.append(token)
                # token 可能占多个字符，位置只有一个：每个字符都指向同一段原文，
                # 否则 text 下标和 spans 下标会错位（数字出现在句中时必现）
                spans.extend([(i, j - i + used)] * len(token))
                i = j + used
                continue

        # 2) 中文数字串（含"一点五"这种小数）
        if _is_cn_num(ch):
            value, j = _scan_chinese(text, i)
            run = text[i:j]
            followed_by_unit = _match_unit(text, j)[2] > 0
            decimal = "." in value
            if value and (len(run) >= 2 or followed_by_unit or decimal
                          or any(c in _CN_UNIT or c in _CN_BIG for c in run)):
                token, used = _number_token(value, text, j)
                out.append(token)
                # token 可能占多个字符，位置只有一个：每个字符都指向同一段原文，
                # 否则 text 下标和 spans 下标会错位（数字出现在句中时必现）
                spans.extend([(i, j - i + used)] * len(token))
                i = j + used
                continue

        # 3) 中文虚词（最长匹配 4 字）
        matched = False
        for size in (4, 3, 2, 1):
            if i + size <= n and text[i:i + size] in STOPWORDS:
                i += size
                matched = True
                break
        if matched:
            continue

        # 4) 英文虚词（按词边界）
        if ch.isascii() and ch.isalpha():
            skipped = _skip_word(text, i)
            if skipped:
                i += skipped
                continue

        # 5) 其余字符（实词、英文、标点）原样保留
        out.append(ch)
        spans.append((i, 1))
        i += 1

    return Skeleton(text="".join(out), spans=spans)


# ---------------------------------------------------------------- LCS
def lcs_ratio(a: str, b: str, cap: int = 2000) -> float:
    """最长公共子序列占比（以较短者为分母），用于骨架命中的二次校验。"""
    a, b = a[:cap], b[:cap]
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    prev = [0] * (len(b) + 1)
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if ch == b[j - 1]:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = cur[j - 1] if cur[j - 1] > prev[j] else prev[j]
        prev = cur
    return prev[-1] / len(a)


class SkeletonIndex:
    """骨架层的索引与查询。内部复用 Index / RunAccumulator，只是换了 k、w 和位置含义。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.sk_cfg = cfg.with_(k=cfg.sk_k, w=cfg.sk_w)
        self.index = Index(self.sk_cfg)
        self.skeletons: list[Skeleton] = []   # 每份文档的骨架 + 位置映射
        self.orig: list[str] = []             # 每份文档的原文（已归一化）

    def add(self, doc_id: str, text: str, tag: str = "", path: str = "") -> int:
        norm = normalize(text, self.cfg)
        sk = skeletonize(norm)
        self.skeletons.append(sk)
        self.orig.append(norm)
        return self.index.add(doc_id, sk.text, tag, path)

    @property
    def doc_count(self) -> int:
        return self.index.doc_count

    def scan(self, payload_norm: str) -> list[Hit]:
        """对一条（已归一化的）待检测文本做骨架对照。"""
        if not payload_norm or not self.index.doc_count:
            return []
        sk = skeletonize(payload_norm)
        if len(sk.text) < self.cfg.sk_k:
            return []

        fps = fingerprint(sk.text, self.sk_cfg)
        if not fps:
            return []

        acc = RunAccumulator(self.sk_cfg.k, self.sk_cfg.max_gap,
                             max_deltas_per_pos=self.cfg.max_deltas_per_pos,
                             bucket_keep=self.cfg.bucket_keep)
        acc.extend(self.index.hits(fps))

        hits: list[Hit] = []
        payload_len = max(len(payload_norm), 1)
        for cluster in acc.finish():
            if cluster.length < self.cfg.sk_min_run:
                continue
            doc_sk = self.skeletons[cluster.doc_idx]
            p_start, p_end = sk.orig_range(cluster.payload_start, cluster.payload_end)
            d_start, d_end = doc_sk.orig_range(cluster.doc_start, cluster.doc_end)
            if p_end <= p_start or d_end <= d_start:
                continue

            # 回原文做 LCS 校验：骨架相同 ≠ 原文相同（"不"字会被当虚词删掉）
            a = payload_norm[p_start:p_end]
            b = self.orig[cluster.doc_idx][d_start:d_end]
            ratio = lcs_ratio(a, b)
            if ratio < self.cfg.sk_lcs:
                continue

            length = p_end - p_start
            doc_len = max(len(self.orig[cluster.doc_idx]), 1)
            hits.append(Hit(
                doc_id=self.index.name_of(cluster.doc_idx),
                payload_id="",
                detector="skeleton",
                score=round(min(1.0, 0.5 * ratio + 0.5 * min(1.0, length / 128)), 3),
                longest_run=length,
                matched=length,
                coverage_payload=round(length / payload_len, 3),
                coverage_doc=round(length / doc_len, 3),
                spans=[Span(doc_start=d_start, doc_end=d_end,
                            payload_start=p_start, payload_end=p_end)],
                tag=self.index.tag_of(cluster.doc_idx),
                path=self.index.path_of(cluster.doc_idx),
                reason=f"骨架连续 {cluster.length} 字符（LCS {ratio:.0%}）",
            ))
        hits.sort(key=lambda h: h.longest_run, reverse=True)
        return hits
