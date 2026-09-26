"""学院数据质量评分领域模块。

口径（指标定义、规则版本）、观察窗口、输入游标在计算批次中固定；
历史批次与已签发评分卡不可变，迟到数据只能形成修订批次。
"""

from __future__ import annotations

from .engine import (
    BatchComputation,
    CollegeScore,
    MetricResult,
    ObservationWindow,
    calculate_batch,
    rank_colleges,
    recompute_metric,
    scorecard_fingerprint,
    window_bounds,
)
from .metrics import (
    DEFAULT_RULE_VERSION,
    MetricSpec,
    build_default_specs,
)

__all__ = [
    "BatchComputation",
    "CollegeScore",
    "MetricResult",
    "ObservationWindow",
    "calculate_batch",
    "rank_colleges",
    "recompute_metric",
    "scorecard_fingerprint",
    "window_bounds",
    "DEFAULT_RULE_VERSION",
    "MetricSpec",
    "build_default_specs",
]
