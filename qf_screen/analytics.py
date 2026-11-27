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
  price table for this, and :func:`simulate` carries the last finite price
  forward WITHIN a holding block itself as well, so a raw table with gaps
  (a suspension, a delisted tail) poisons no date: one missing price used
  to turn ``NaN`` the whole cross-sectional mean, and with it every later
  value of the path;
* a name with no price at all on its entry date (it has never traded
  inside the window) is dropped from that period rather than priced — and
  a period left with NOTHING to hold is a period in cash (flat rows), not a
  hole in the path.

With ``cost_bps > 0`` the path is NET of transaction costs (v0.4): the
rate is paid on every side of every trade — the initial purchase, each
rebalance's sells and buys, and the final liquidation — and the gross
holding return of each period stays visible in ``periods``.

:func:`perf` reduces a daily return series to the statistics a validation
prints.  Nothing here looks past a date it is valuing: the value on day
``t`` uses closes through ``t`` and membership decided before ``t``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

#: trading days per year (annualization only; cross-sectional constant)
ANN = 252.0

#: how far below the nominal limit a close may sit and still count as a
#: limit-up: the exchange rounds the limit price to 0.01 CNY, so a 2.04
#: close limits at 2.24 (+9.80 %, not +10 %)
LIMIT_TOLERANCE = 0.002


def _carry_forward(values: np.ndarray) -> np.ndarray:
    """Each column forward-filled over its rows: a missing / non-positive
    price takes the last valid one above it (leading gaps stay NaN)."""
    out = np.array(values, dtype=float, copy=True)
    if out.size == 0:
        return out
    valid = np.isfinite(out) & (out > 0)
    rows = np.arange(out.shape[0])[:, None]
    last = np.maximum.accumulate(np.where(valid, rows, -1), axis=0)
    filled = np.take_along_axis(out, np.maximum(last, 0), axis=0)
    filled[last < 0] = np.nan
    return filled


@dataclass
class PortfolioPath:
    """The simulated path of one schedule-following portfolio.

    ``positions`` are calendar rows, ``values`` the portfolio's value at
    each of those rows' close (the chain starts at 1.0 before the first
    period; NET of costs when ``cost_bps`` is set, a rebalance row showing
    the value after that rebalance's cost) and ``periods`` one row per
    holding period: ``entry``, ``members``, ``period_return`` (the GROSS
    return of the names held), ``turnover`` (the share of names that are
    NEW versus the previous period; NaN for the first) and ``cost`` (the
    fraction of the portfolio the rebalance paid in transaction costs).
    """

    positions: np.ndarray
    values: np.ndarray
    periods: pd.DataFrame
    cost_bps: float = 0.0

    @property
    def empty(self) -> bool:
        return self.values.size == 0

    def returns(self) -> np.ndarray:
        """Daily returns of the path (first day measured from 1.0)."""
        if self.empty:
            return np.array([])
        return np.r_[self.values[0] - 1.0, self.values[1:] / self.values[:-1] - 1.0]


def simulate(prices: np.ndarray, holdings: Sequence[Tuple[int, np.ndarray]],
             exits: Sequence[int], cost_bps: float = 0.0) -> PortfolioPath:
    """Walk ``holdings`` — ``(entry row, member columns)`` per schedule
    date, in order — over the ``date x symbol`` ``prices`` matrix.

    ``exits[i]`` is the row at which holding ``i`` ends (the next schedule
    date, or the end of the validation window for the last one); the
    holding's period return is measured over ``[entry, exit]``.

    ``cost_bps`` is the transaction cost PER SIDE of a trade, in basis
    points of the value traded (``15`` = 0.15 % to buy, another 0.15 % to
    sell).  A rebalance sells the share of the previous list that leaves
    and buys the share of the new list that is new — for two equally long
    lists that is ``2 x turnover x cost_bps`` — assuming equal weights at
    the rebalance; the initial purchase costs one full side and the final
    liquidation another, so a round trip always pays both of its sides
    whatever the window.  Re-weighting the names that stay is not charged.
    """
    rate = float(cost_bps) / 1e4
    total = 1.0
    positions: list = []
    values: list = []
    rows: list = []
    previous: Optional[np.ndarray] = None
    held = False
    for (entry, members), exit_row in zip(holdings, exits):
        if exit_row <= entry:
            continue
        members = np.asarray(members, dtype=int)
        window = np.asarray(prices[entry: exit_row + 1])
        length = len(window)                       # the rows the table really has
        if length < 2:
            continue
        if members.size:
            block = _carry_forward(np.asarray(window[:, members], dtype=float))
            known = np.isfinite(block[0]) & (block[0] > 0)
            members, block = members[known], block[:, known]
        else:
            block = np.empty((length, 0))
        kept = 0 if previous is None else int(np.intersect1d(previous, members).size)
        # what the rebalance trades, as a share of the portfolio: the names
        # that leave are sold, the names that are new are bought (an empty
        # list is a full liquidation; nothing before the first period)
        sold = 1.0 - kept / len(previous) if previous is not None and len(previous) \
            else 0.0
        bought = 1.0 - kept / len(members) if len(members) else 0.0
        turnover = np.nan if previous is None else (bought if len(members) else 1.0)
        cost = rate * (sold + bought)
        if cost:                                   # paid on the rebalance row
            total *= 1.0 - cost
            if values:
                values[-1] = total
        if members.size:
            with np.errstate(invalid="ignore", divide="ignore"):
                growth = block / block[0]          # each name's own path
            path = growth.mean(axis=1)             # equal weight at entry, drifting
        else:
            path = np.ones(length)                 # nothing to hold: cash
        total = total * path[-1]
        positions.extend(range(entry + 1, entry + length))
        values.extend(total * path[1:] / path[-1])
        rows.append({"entry": entry, "members": int(len(members)),
                     "period_return": float(path[-1] - 1.0),
                     "turnover": float(turnover), "cost": float(cost)})
        previous, held = members, bool(members.size)
    if rate and values and held:                   # liquidating the book
        values[-1] *= 1.0 - rate
    columns = ["entry", "members", "period_return", "turnover", "cost"]
    periods = pd.DataFrame(rows, columns=columns)
    return PortfolioPath(np.array(positions, dtype=int), np.array(values, dtype=float),
                         periods, cost_bps=float(cost_bps))


def limit_up_mask(close_raw, boards, limits: Optional[Mapping[str, float]] = None,
                  tolerance: float = LIMIT_TOLERANCE) -> np.ndarray:
    """``date x symbol`` bool: the session's RAW close sits at the daily
    limit-up — the entry guard of ``validate --skip-limit-up``.

    The limit is measured against the previous TRADED close (the raw series
    forward-filled, one session back), as the exchanges define it, and a
    close within ``tolerance`` of the nominal limit counts: the limit
    price is rounded to 0.01 CNY, so a 2.04 CNY close limits at 2.24,
    +9.80 %.  ``boards`` names each column's board and ``limits`` the
    limit per board (default: :data:`qf_screen.data.universe.BOARD_LIMITS`
    — 10 % main, 20 % ChiNext / STAR, 30 % Beijing); a board the table
    does not know is treated as a main-board name.

    This is a test on the CLOSE.  The store keeps raw closes for every
    name but opening prices only for the lxg layout-A subset, and the
    validation buys at the close: a name that opened limit-up and closed
    lower was buyable, one that closed limit-up was not.  An ST name's
    5 % band is not recognised (the presets exclude ST).
    """
    if limits is None:
        from qf_screen.data.universe import BOARD_LIMITS

        limits = BOARD_LIMITS
    raw = np.asarray(close_raw, dtype=float)
    caps = np.array([float(limits.get(str(board), 0.10)) - tolerance
                     for board in boards], dtype=float)
    if raw.shape[0] < 2:
        return np.zeros(raw.shape, dtype=bool)
    previous = np.vstack([np.full((1, raw.shape[1]), np.nan), _carry_forward(raw)[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        gain = raw / previous - 1.0
    return np.greater_equal(gain, caps[None, :], where=np.isfinite(gain),
                            out=np.zeros(raw.shape, dtype=bool))


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


__all__ = ["ANN", "LIMIT_TOLERANCE", "PortfolioPath", "limit_up_mask", "perf", "simulate",
           "spread_stats"]
