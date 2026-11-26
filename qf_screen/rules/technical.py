"""Technical / price-volume screen filters and rankers.

Thin, declarative wrappers over the panel's price features — the screens
with decades of replicated cross-sectional evidence, the same set
``qf_factor.framework.selection.screens`` ships (see that module for the
citations).  Every filter here is a DSL expression, so an exported preset
reads exactly like the code that runs it.
"""
from __future__ import annotations

import numpy as np

from .base import Filter, Ranker
from .hygiene import ExprFilter


def trend(ma_window: int = 200) -> Filter:
    """Close above its long moving average (Faber-style trend filter --
    the single highest-value veto in a long-only stack)."""
    return ExprFilter("trend", f"ma_ratio_{ma_window} > 1",
                      f"adjusted close above its {ma_window}d mean")


def momentum(min_return: float = 0.0, lookback: int = 126, skip: int = 21) -> Filter:
    """Positive trailing return with the most recent month skipped
    (shipped window 126/21 = 6-1)."""
    return ExprFilter("momentum", f"mom_{lookback}s{skip} > {min_return}",
                      f"{lookback}d-{skip}d return > {min_return:.0%}")


def near_high(min_ratio: float = 0.7) -> Filter:
    """Within ``1 - min_ratio`` of the trailing 52-week high (George–Hwang)."""
    return ExprFilter("near_high", f"high252_ratio >= {min_ratio}",
                      f">= {min_ratio:.0%} of the 52w high")


def low_volatility(top_frac: float = 0.5, window: int = 60) -> Filter:
    """Bottom ``top_frac`` by trailing volatility (the low-risk anomaly)."""
    return ExprFilter("low_vol", f"pct_rank(vol_{window}d) <= {top_frac}",
                      f"bottom {top_frac:.0%} by {window}d volatility")


def not_lottery(exclude_frac: float = 0.1, window: int = 21) -> Filter:
    """NOT in the top ``exclude_frac`` by MAX (Bali–Cakici–Whitelaw) --
    lottery-bid names subsequently underperform."""
    if window != 21:
        raise ValueError("only the registered max21 feature is available; "
                         "register max{window}d first")
    return ExprFilter("not_lottery", f"pct_rank(max21) < {1 - exclude_frac}",
                      f"not top {exclude_frac:.0%} by MAX")


def drawdown_guard(max_drawdown: float = 0.5) -> Filter:
    """Trailing drawdown from the 52-week high no deeper than
    ``max_drawdown`` (falling knives)."""
    floor = 1.0 - max_drawdown
    return ExprFilter("drawdown", f"high252_ratio >= {floor}",
                      f"no more than {max_drawdown:.0%} below the 52w high")


class ExprRanker(Ranker):
    """A ranker written as a DSL numeric expression (higher = better)."""

    def __init__(self, name: str, expr: str, description: str = ""):
        super().__init__(name, description or expr)
        from .dsl import compile_score, expr_names
        self.expr = expr
        self.features = expr_names(expr)
        unknown = [f for f in self.features
                   if f not in _known_features() and not _is_dynamic(f)]
        if unknown:
            from .base import feature_names
            raise ValueError(f"ranker {name!r} references unknown feature(s) "
                             f"{unknown}; known: {', '.join(feature_names())} "
                             '(or factor_<family>__<column>)')
        self._fn = compile_score(expr)

    def score(self, panel, pos) -> np.ndarray:
        return self._fn(panel.env(pos, self.features)).astype(float)

    def __repr__(self) -> str:
        return f"<ExprRanker {self.name}: {self.expr}>"


def _known_features() -> frozenset:
    from .base import FEATURES
    return frozenset(FEATURES)


def _is_dynamic(name: str) -> bool:
    from .base import is_dynamic_feature
    return is_dynamic_feature(name)
