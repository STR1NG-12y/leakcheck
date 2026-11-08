"""参数集中定义 + 合法性校验。

所有可调项都在这里，改行为不用改算法。

关键约束：``min_run >= k + w - 1``。
k + w - 1 是算法**保证能检出**的最短公共子串长度（winnowing 定理）；
如果告警阈值比它还小，就会出现"检出了但被阈值卡掉"的配置矛盾。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class Config:
    # ---- 指纹参数 ----
    k: int = 16            # 指纹窗口：k 个字符一个指纹
    w: int = 8             # winnowing 窗口：每 w 个指纹留 1 个
    # ---- 判定参数 ----
    min_run: int = 32      # 告警所需的最短连续命中（字符）
    min_hits: int = 3      # 一条 run 至少要有几个指纹命中
    coverage: float = 0.30  # 净荷覆盖率门槛（短净荷场景）
    max_gap_mult: int = 4  # 同一对角线上允许的最大指纹间隔 = max_gap_mult * k
    # ---- 短内容支路 ----
    short_min_len: int = 8   # 短于该长度不登记（避免"的、了"这类噪音）
    short_min_run: int = 6   # 短模式命中后，片段至少这么长才报
    # ---- 归一化开关 ----
    nfkc: bool = True
    collapse_ws: bool = True
    lower: bool = False
    # ---- 资源上限 ----
    max_text_chars: int = 2_000_000  # 单条文本处理上限

    def __post_init__(self) -> None:
        if self.k < 4:
            raise ValueError("k 太小（<4）：指纹几乎没有区分度")
        if self.w < 1:
            raise ValueError("w 必须 >= 1")
        if self.min_run < self.guarantee_len:
            raise ValueError(
                f"min_run({self.min_run}) 必须 >= 算法保证长度 k+w-1({self.guarantee_len})"
            )
        if not 0.0 < self.coverage <= 1.0:
            raise ValueError("coverage 必须在 (0, 1] 内")
        if self.short_min_len < 2:
            raise ValueError("short_min_len 太小")

    # ------------------------------------------------------------ 派生量
    @property
    def guarantee_len(self) -> int:
        """算法保证能检出的最短公共子串（字符）。"""
        return self.k + self.w - 1

    @property
    def max_gap(self) -> int:
        return self.max_gap_mult * self.k

    @property
    def density(self) -> float:
        """指纹密度：winnowing 约为 2/(w+1)。"""
        return 2.0 / (self.w + 1)

    def with_(self, **kw) -> "Config":
        """派生一份改过参数的配置（用于消融实验）。"""
        return replace(self, **kw)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(guarantee_len=self.guarantee_len, density=round(self.density, 3))
        return d
