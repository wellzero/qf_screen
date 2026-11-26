"""Schedule → portfolio: the arithmetic behind ``validate`` / ``compare``.

An exported schedule is a list of "hold these names from this date" rows.
:func:`simulate` turns one into the path of the equal-weight portfolio
that acts on it:

* formed at each schedule date's close (the schedule is point-in-time, so
  acting ON the date is safe) and held until the next schedule date;
* equal-weighted AT the rebalance and NOT re-weighted in between — weights
  drift with each name's return, which is what a real position does;
* a name that is suspended is valued at its last close (it cannot be sold,
  but it is not worth nothing either) — the caller passes a forward-filled
  price table for this;
* a name with no price at all on its entry date (it has never traded
  inside the window) is dropped from that period rather than priced.

:func:`perf` reduces a daily return series to the statistics a validation
prints.  Nothing here looks past a date it is valuing: the value on day
``t`` uses closes through ``t`` and membership decided before ``t``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

#: trading days per year (annualization only; cross-sectional constant)
ANN = 252.0


@dataclass
class PortfolioPath:
    """The simulated path of one schedule-following portfolio.

    ``positions`` are calendar rows, ``values`` the portfolio's value at
    each of those rows' close (the chain starts at 1.0 before the first
    period) and ``periods`` one row per holding period: ``entry``,
    ``members``, ``period_return`` and ``turnover`` (the share of names
    that are NEW versus the previous period; NaN for the first).
    """

    positions: np.ndarray
    values: np.ndarray
    periods: pd.DataFrame

    @property
    def empty(self) -> bool:
        return self.values.size == 0

    def returns(self) -> np.ndarray:
        """Daily returns of the path (first day measured from 1.0)."""
        if self.empty:
            return np.array([])
        return np.r_[self.values[0] - 1.0, self.values[1:] / self.values[:-1] - 1.0]


def simulate(prices: np.ndarray, holdings: Sequence[Tuple[int, np.ndarray]],
             exits: Sequence[int]) -> PortfolioPath:
    """Walk ``holdings`` — ``(entry row, member columns)`` per schedule
    date, in order — over the ``date x symbol`` ``prices`` matrix.

    ``exits[i]`` is the row at which holding ``i`` ends (the next schedule
    date, or the end of the validation window for the last one); the
    holding's period return is measured over ``[entry, exit]``.
    """
    total = 1.0
    positions: list = []
    values: list = []
    rows: list = []
    previous: Optional[np.ndarray] = None
    for (entry, members), exit_row in zip(holdings, exits):
        if exit_row <= entry or len(members) == 0:
            continue
        block = prices[entry: exit_row + 1][:, members]
        known = np.isfinite(block[0]) & (block[0] > 0)
        if not known.all():
            members, block = members[known], block[:, known]
        if len(members) == 0 or block.shape[0] < 2:
            continue
        with np.errstate(invalid="ignore", divide="ignore"):
            growth = block / block[0]              # each name's own path
        path = growth.mean(axis=1)                 # equal weight at entry, drifting
        total = total * path[-1]
        positions.extend(range(entry + 1, exit_row + 1))
        values.extend(total * path[1:] / path[-1])
        turnover = np.nan
        if previous is not None:
            turnover = 1.0 - len(np.intersect1d(previous, members)) / len(members)
        rows.append({"entry": entry, "members": int(len(members)),
                     "period_return": float(path[-1] - 1.0),
                     "turnover": float(turnover)})
        previous = members
    columns = ["entry", "members", "period_return", "turnover"]
    periods = pd.DataFrame(rows, columns=columns)
    return PortfolioPath(np.array(positions, dtype=int), np.array(values, dtype=float),
                         periods)


def perf(returns) -> Dict[str, float]:
    """Annualized return / volatility, Sharpe (risk-free rate 0), maximum
    drawdown and cumulative return of a daily return series."""
    values = np.asarray(pd.Series(returns).to_numpy(dtype=float), dtype=float)
    values = values[np.isfinite(values)]
    out = {"days": float(len(values)), "cum_return": np.nan, "ann_return": np.nan,
           "ann_vol": np.nan, "sharpe": np.nan, "max_drawdown": np.nan}
    if len(values) == 0:
        return out
    growth = np.cumprod(1.0 + values)
    total = growth[-1] - 1.0
    out["cum_return"] = float(total)
    out["ann_return"] = float((1.0 + total) ** (ANN / len(values)) - 1.0) \
        if 1.0 + total > 0 else -1.0
    if len(values) > 1:
        dev = values.std(ddof=1)
        out["ann_vol"] = float(dev * np.sqrt(ANN))
        if dev > 0:
            out["sharpe"] = float(values.mean() * ANN / (dev * np.sqrt(ANN)))
    out["max_drawdown"] = float((growth / np.maximum.accumulate(growth) - 1.0).min())
    return out


def spread_stats(excess) -> Dict[str, float]:
    """``portfolio − baseline`` daily returns: annualized excess,
    tracking error and the information ratio."""
    values = np.asarray(excess, dtype=float)
    values = values[np.isfinite(values)]
    out = {"excess_ann": np.nan, "tracking_error": np.nan, "information_ratio": np.nan}
    if len(values) < 2:
        return out
    dev = values.std(ddof=1)
    out["excess_ann"] = float(values.mean() * ANN)
    out["tracking_error"] = float(dev * np.sqrt(ANN))
    if dev > 0:
        out["information_ratio"] = float(values.mean() * ANN / (dev * np.sqrt(ANN)))
    return out


__all__ = ["ANN", "PortfolioPath", "perf", "simulate", "spread_stats"]
