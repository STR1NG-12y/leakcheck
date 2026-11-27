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
    short_min_run: int = 12  # 短模式命中后，片段至少这么长才报（太短会噪声）
    # ---- 归一化开关 ----
    nfkc: bool = True
    collapse_ws: bool = True
    lower: bool = False
    # ---- 索引与聚合限流（取代 max_hits 的日常作用）----
    max_dup_per_doc: int = 64      # 同一 (doc, k-gram) 最多登记几个位置
    max_deltas_per_pos: int = 8    # 每个待检测位置最多开几个新桶
    bucket_keep: int = 128         # 每个桶保留首/末各多少个点
    max_verify_per_doc: int = 64   # 每份文档最多做多少次逐字符校验
    # ---- 骨架层（二阶）----
    sk_k: int = 8                  # 骨架 token 窗口
    sk_w: int = 4                  # 骨架抽稀窗口
    sk_min_run: int = 16           # 骨架命中所需的最少 token 数
    sk_lcs: float = 0.6            # 骨架命中后，字符级 LCS 相似度门槛
    # ---- 语义层（三阶）----
    sw_top_k: int = 3              # Smith-Waterman 最多跑几个候选
    sw_window: int = 200           # 局部对齐的窗口半径（字符）
    sw_band: int = 32              # banded SW 的带宽
    sw_min_len: int = 16           # SW 对齐出这么长才算证据
    stage3_budget_ms: int = 200    # 单条文本三阶时间预算
    rerank: bool = False           # 是否启用 cross-encoder 精排（默认为关）
    # ---- OCR ----
    ocr_min_run: int = 16          # 来自 OCR 的块，告警门槛可以更低
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
        if self.max_dup_per_doc < 1:
            raise ValueError("max_dup_per_doc 必须 >= 1")
        if self.max_deltas_per_pos < 1:
            raise ValueError("max_deltas_per_pos 必须 >= 1")
        if self.bucket_keep < 1:
            raise ValueError("bucket_keep 必须 >= 1")
        if self.sk_k < 2 or self.sk_w < 1:
            raise ValueError("骨架层的 sk_k / sk_w 不合法")

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
