"""决策层：**路径为主判据，内容为二次确认（或独立模式）**。

为什么要分开
------------
"这个文件敏不敏感"和"上传的内容里有没有它"是两个问题：
- 前者靠路径规则就能回答（便宜、可解释、可审计）；
- 后者必须做内容比对（贵，但能给出"哪一段"的铁证）。

把两者合成一张决策矩阵：

                    内容二次确认通过        内容未比对 / 未命中
    路径判定敏感      确认泄露 confirmed     疑似 suspected
    路径判定不敏感    规则缺口 confirmed*    放行 clean

几点说明
--------
- ``confirmed*``：内容命中了，但路径规则没覆盖这个文件。结论仍然是"泄露"（内容就是铁证），
  但会标注"建议补规则"——这也是内容比对的一个副产品：**发现策略漏洞**。
- ``suspected``：只有路径证据。比如文件被访问/上传了，但索引里没有它的可比对文本
  （二进制、编码不同、还没入库）。这一步交给人工复核，不要当放行。
- 两种模式共用同一个 :class:`~leakcheck.matcher.Matcher`，只是入口不同。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .matcher import Matcher
from .pathpolicy import PathDecision, PathPolicy, normalize_path
from .types import Hit

SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1, "none": 0}


@dataclass
class Verdict:
    """一条最终判定。"""

    mode: str            # "path"（路径主模式）/ "content"（纯内容模式）
    level: str           # confirmed / suspected / clean
    path: str
    tag: str = ""
    severity: str = "low"
    rule: str = ""
    score: float = 0.0
    doc_id: str = ""
    reasons: list[str] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        """是否需要处理（clean 之外都要）。"""
        return self.level != "clean"

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "level": self.level, "path": self.path,
            "tag": self.tag, "severity": self.severity, "rule": self.rule,
            "score": self.score, "doc_id": self.doc_id, "reasons": self.reasons,
            "hits": [h.to_dict() for h in self.hits],
        }

    def headline(self) -> str:
        label = {"confirmed": "确认", "suspected": "疑似", "clean": "放行"}[self.level]
        return f"[{label}] {self.tag or '-'}/{self.severity}  {self.path}"


class Judge:
    def __init__(self, matcher: Matcher | None = None,
                 policy: PathPolicy | None = None,
                 listed: Iterable[str] | None = None) -> None:
        self.matcher = matcher
        self.policy = policy or PathPolicy()
        # "可疑路径清单"：安全团队给的、规则未必覆盖的路径；命中即视为敏感
        self._listed = [normalize_path(p) for p in (listed or [])]

    # ------------------------------------------------------------ 内部
    def _decision(self, path: str) -> PathDecision:
        decision = self.policy.check(path)
        if decision.sensitive or not self._listed:
            return decision
        norm = normalize_path(path)
        for item in self._listed:
            if norm == item or norm.startswith(item.rstrip("/") + "/"):
                return PathDecision(path, norm, True, "listed", "high", "清单",
                                    f"命中可疑路径清单：{item}")
        return decision

    # ------------------------------------------------------------ 模式 A
    def by_path(self, path: str, text: str | None = None,
                payload_id: str = "") -> Verdict:
        """路径模式：先判路径，再用内容做二次确认。

        ``text=None`` 时只做路径判定（此时结论最高只能是 suspected）。
        """
        decision = self._decision(path)
        path_reason = f"路径命中『{decision.rule}』：{decision.reason}"

        if not decision.sensitive:
            hits = self._match(text, payload_id) if text else []
            if hits:
                best = hits[0]
                return Verdict(
                    mode="path", level="confirmed", path=path,
                    tag=best.tag or decision.tag, severity="high",
                    rule=decision.rule, score=best.score, doc_id=best.doc_id,
                    reasons=[f"路径未命中任何规则（{decision.reason}）",
                             f"但内容命中 {best.doc_id}：{best.reason}",
                             "→ 该文件未被策略覆盖，建议补充路径规则"],
                    hits=hits,
                )
            return Verdict(mode="path", level="clean", path=path,
                           tag=decision.tag, severity="none",
                           rule=decision.rule, reasons=[f"路径未命中任何规则（{decision.reason}）"])

        # 路径敏感
        if text is None:
            return Verdict(mode="path", level="suspected", path=path,
                           tag=decision.tag, severity=decision.severity,
                           rule=decision.rule, score=0.5,
                           reasons=[path_reason, "未提供内容，仅凭路径判定（需人工确认）"])

        hits = self._match(text, payload_id)
        if hits:
            best = hits[0]
            return Verdict(mode="path", level="confirmed", path=path,
                           tag=decision.tag, severity=decision.severity,
                           rule=decision.rule, score=best.score, doc_id=best.doc_id,
                           reasons=[path_reason, f"内容二次确认：{best.reason}"],
                           hits=hits)
        return Verdict(mode="path", level="suspected", path=path,
                       tag=decision.tag, severity=decision.severity,
                       rule=decision.rule, score=0.5,
                       reasons=[path_reason,
                                "内容未比对成功（可能未入库、是二进制、或已被改写）"])

    # ------------------------------------------------------------ 模式 B
    def by_content(self, text: str, payload_id: str = "") -> list[Verdict]:
        """内容模式：直接比对，再用路径反查类别与级别。"""
        hits = self._match(text, payload_id)
        verdicts: list[Verdict] = []
        for hit in hits:
            decision = self._decision(hit.path or hit.doc_id)
            if decision.sensitive:
                reasons = [f"内容命中：{hit.reason}",
                           f"路径复核命中『{decision.rule}』：{decision.reason}"]
                severity = decision.severity
                tag = decision.tag or hit.tag
            else:
                reasons = [f"内容命中：{hit.reason}",
                           f"路径未命中规则（{decision.reason}）——该文件未被策略覆盖，建议补规则"]
                severity = "high"
                tag = hit.tag or "unknown"
            verdicts.append(Verdict(
                mode="content", level="confirmed", path=hit.path or hit.doc_id,
                tag=tag, severity=severity, rule=decision.rule,
                score=hit.score, doc_id=hit.doc_id, reasons=reasons, hits=[hit],
            ))
        return verdicts

    # ------------------------------------------------------------ 批量
    def scan_paths(self, paths: list[str]) -> list[Verdict]:
        """批量路径模式（例如"这个目录整体要被上传"）。"""
        verdicts = [self.by_path(p) for p in paths]
        verdicts = [v for v in verdicts if v.actionable]
        verdicts.sort(key=lambda v: (SEVERITY_ORDER.get(v.severity, 0), v.score),
                      reverse=True)
        return verdicts

    # ------------------------------------------------------------ 内部
    def _match(self, text: str, payload_id: str) -> list[Hit]:
        if self.matcher is None:
            return []
        return self.matcher.match(text, payload_id)
