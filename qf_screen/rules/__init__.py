"""Screen rules: feature panel, filters, rankers, the expression DSL.

Importing this package registers every built-in feature (the topic modules
register on import), so ``from qf_screen.rules import ExprFilter`` is
enough for the full registry to exist.
"""
from __future__ import annotations

from .base import (
    ANN, FEATURES, CombinedRanker, FeatureBuilder, FeaturePanel, Filter, Ranker,
    feature_names, register_feature,
)
from .dsl import (
    DslError, allowed_functions, compile_bool, compile_expr, compile_score, expr_names,
)
from .hygiene import (
    BoardFilter, ExprFilter, NotSuspended, exclude_st, liquidity_floor, listing_age,
    price_floor, turnover_ceiling,
)
from .technical import (
    ExprRanker, drawdown_guard, low_volatility, momentum, near_high, not_lottery, trend,
)
from .fundamental import (
    debt_max, div_yield_min, fcf_yield_min, growth_min, pb_under, pe_range, roe_min,
)

__all__ = [
    # base
    "ANN", "FEATURES", "CombinedRanker", "FeatureBuilder", "FeaturePanel", "Filter",
    "Ranker", "feature_names", "register_feature",
    # dsl
    "DslError", "allowed_functions", "compile_bool", "compile_expr", "compile_score",
    "expr_names",
    # hygiene
    "BoardFilter", "ExprFilter", "NotSuspended", "exclude_st", "liquidity_floor",
    "listing_age", "price_floor", "turnover_ceiling",
    # technical
    "ExprRanker", "drawdown_guard", "low_volatility", "momentum", "near_high",
    "not_lottery", "trend",
    # fundamental
    "debt_max", "div_yield_min", "fcf_yield_min", "growth_min", "pb_under",
    "pe_range", "roe_min",
]

# registers the fundamental + alt features (side effect of import)
from . import fundamental as _fundamental  # noqa: E402,F401
from . import ashare_alt as _ashare_alt    # noqa: E402,F401
