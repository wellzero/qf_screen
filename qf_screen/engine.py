"""The screen engine: one screen definition, two honest modes.

* :meth:`ScreenEngine.run` — the snapshot.  "Which names pass as of date
  D?" with the full funnel (per-filter pass counts) and per-symbol
  explanations.  Decided on D's close.
* :meth:`ScreenEngine.schedule` — the causal wide-panel export.  The SAME
  filters evaluated at ``t-1`` produce membership at ``t`` (the
  strictly-before ``lag=1`` convention of
  :mod:`qf_factor.framework.selection.screens`), written as a long
  ``date,symbol`` schedule that loads straight into the qf_factor
  backtest/live plumbing.
* :meth:`ScreenEngine.validate` — historical forward returns of the
  schedule's members against the screen's own candidate pool, at several
  horizons.

One evaluation function backs both modes, so the snapshot a user sees and
the schedule a backtest consumes cannot drift apart in semantics.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from qf_screen.data import CNDataStore
from qf_screen.presets import load_preset, validate_filters
from qf_screen.rules import (
    BoardFilter, CombinedRanker, ExprFilter, ExprRanker, FeaturePanel, Filter,
    expr_names,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- specs
@dataclass
class UniverseSpec:
    """Which names may enter the screen at all (before any filter)."""
    boards: Sequence[str] = ("all",)
    symbols: Optional[Sequence[str]] = None
    exclude_st: bool = True
    min_listed_days: Optional[int] = None
    #: opt-in historical ST/*ST exclusion from the qf_factor price-band
    #: detector (main-board 5%-band state machine, ~20 s to build once).
    #: The name-snapshot flag above only speaks from its file date forward.
    st_band: bool = False

    def filters(self) -> List[Filter]:
        out: List[Filter] = [BoardFilter(self.boards)]
        if self.exclude_st:
            out.append(ExprFilter("not_st", "not st_flag",
                                  "not ST/*ST in the company-list snapshot "
                                  "(forward-only)"))
        if self.min_listed_days:
            out.append(ExprFilter("listing_age",
                                  f"listed_days >= {int(self.min_listed_days)}",
                                  f"listed >= {int(self.min_listed_days)} days"))
        return out


@dataclass
class ScreenSpec:
    """A complete screen: universe + ordered filters + ranking."""
    name: str
    filters: List[Dict]
    rank: List[Dict] = field(default_factory=list)
    top_n: Optional[int] = None
    universe: UniverseSpec = field(default_factory=UniverseSpec)
    description: str = ""
    source: Optional[str] = None

    @classmethod
    def from_yaml(cls, name_or_path: str) -> "ScreenSpec":
        doc = load_preset(name_or_path)
        uni = doc.get("universe") or {}
        universe = UniverseSpec(
            boards=tuple(uni.get("boards", ("all",))),
            symbols=uni.get("symbols"),
            exclude_st=bool(uni.get("exclude_st", True)),
            min_listed_days=uni.get("min_listed_days"),
            st_band=bool(uni.get("st_band", False)))
        spec = cls(name=doc["name"], filters=doc["filters"], rank=doc.get("rank") or [],
                   top_n=doc.get("top_n"), universe=universe,
                   description=doc.get("description", ""), source=doc.get("source"))
        validate_filters(spec.filters)
        return spec

    def all_filters(self) -> List[Filter]:
        out = list(self.universe.filters())
        for entry in self.filters:
            out.append(ExprFilter(str(entry.get("name", "filter")),
                                  str(entry["expr"]),
                                  description=str(entry.get("description", "")),
                                  na_pass=bool(entry.get("na_pass", False))))
        return out

    def ranker(self) -> Optional[CombinedRanker]:
        if not self.rank:
            return None
        rankers, weights = [], []
        for i, entry in enumerate(self.rank):
            rankers.append(ExprRanker(f"rank{i}", str(entry["expr"])))
            weights.append(float(entry.get("weight", 1.0)))
        return CombinedRanker(rankers, weights, name="score")

    def mentioned_features(self) -> List[str]:
        """Every feature any filter or ranker expression mentions."""
        wanted: List[str] = []
        for entry in list(self.filters) + list(self.rank):
            for name in expr_names(str(entry["expr"])):
                if name not in wanted:
                    wanted.append(name)
        return wanted


# --------------------------------------------------------------- results
@dataclass
class ScreenResult:
    """The outcome of one snapshot run."""
    spec: ScreenSpec
    asof: pd.Timestamp
    members: pd.Index                       # ranked, truncated to top_n
    scores: pd.Series                       # composite score per member
    funnel: pd.DataFrame                    # rule | passing | members
    panel_values: pd.DataFrame              # feature snapshot of members
    rule_masks: Dict[str, np.ndarray] = field(repr=False, default_factory=dict)
    symbols: pd.Index = field(repr=False, default=None)   # alignment of rule_masks
    store: Optional[CNDataStore] = field(repr=False, default=None)

    def explain(self, symbol: str) -> Dict:
        """Why is this name in or out?  Per-rule pass/fail (available for
        every candidate, members and rejects alike), the feature values the
        rules read (members), the score and its percentile."""
        row = {"symbol": symbol, "asof": self.asof,
               "member": symbol in set(self.members)}
        row["name"] = self.store.universe.get(symbol, "name", "") \
            if self.store is not None else ""
        if self.symbols is not None and symbol in self.symbols:
            loc = self.symbols.get_loc(symbol)
            row["rules"] = {name: bool(mask[loc])
                            for name, mask in self.rule_masks.items()}
        else:
            row["rules"] = {}
        row["features"] = self.panel_values.loc[symbol].to_dict() \
            if symbol in self.panel_values.index else {}
        if symbol in set(self.members):
            score = self.scores.get(symbol, np.nan)
            if np.isfinite(score):
                row["score"] = float(score)
                row["score_percentile"] = float((self.scores < score).mean())
        return row

    def to_frame(self) -> pd.DataFrame:
        out = self.panel_values.reindex(self.members).copy()
        out.insert(0, "score", self.scores.reindex(self.members).to_numpy())
        out.insert(0, "symbol", self.members)
        return out.reset_index(drop=True)

    def summary(self) -> str:
        names = ""
        if self.store is not None:
            names = ", ".join(f"{s} {self.store.universe.get(s, 'name', '')}"
                              for s in self.members[:5])
        return (f"{self.spec.name}: {len(self.members)} members as of "
                f"{self.asof.date()}" + (f" -- {names} ..." if names else ""))


@dataclass
class ValidationReport:
    """Forward-return validation of a schedule."""
    horizons: Dict[int, Dict[str, float]]
    per_date: pd.DataFrame

    def summary(self) -> str:
        lines = []
        for h in sorted(self.horizons):
            s = self.horizons[h]
            lines.append(
                f"{h:>3}d  members {s['members_mean']:+.2%} (median "
                f"{s['members_median']:+.2%})  baseline {s['baseline_mean']:+.2%}  "
                f"spread {s['spread_mean']:+.2%}  hit {s['hit_rate']:.0%}  "
                f"t={s['spread_t']:.1f}  ({int(s['dates'])} dates)")
        return "\n".join(lines) or "no validation dates"


# ---------------------------------------------------------------- engine
class ScreenEngine:
    """Runs a :class:`ScreenSpec` over a store in the two modes."""

    def __init__(self, store: CNDataStore, panel: Optional[FeaturePanel] = None):
        self.store = store
        self.panel = panel if panel is not None else FeaturePanel(store)
        self._st_blocked: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------ shared
    def _evaluate(self, spec: ScreenSpec, pos: int
                  ) -> Tuple[np.ndarray, Dict[str, np.ndarray], Optional[np.ndarray]]:
        """Universe + screen filters ANDed at row ``pos``.

        Returns ``(mask, rule_masks, scores)`` — ``rule_masks`` keeps each
        rule's own pass array for the funnel and :meth:`ScreenResult.explain`;
        ``scores`` is the blended rank over the whole cross-section.
        """
        rule_masks: Dict[str, np.ndarray] = {}
        mask = np.ones(len(self.panel.symbols), dtype=bool)
        for f in spec.all_filters():
            passed = f.passes(self.panel, pos)
            rule_masks[f.name] = passed
            mask &= passed
        ranker = spec.ranker()
        scores = ranker.score(self.panel, pos) if ranker is not None \
            else np.full(len(self.panel.symbols), np.nan)
        return mask, rule_masks, scores

    def _funnel_frame(self, rule_masks: Dict[str, np.ndarray],
                      mask: np.ndarray) -> pd.DataFrame:
        """CUMULATIVE funnel: each row shows how many names survive after
        ANDing that rule in — the narrowing that explains the result."""
        rows = []
        running = np.ones(len(self.panel.symbols), dtype=bool)
        for name, passed in rule_masks.items():
            running &= passed
            rows.append({"rule": name, "passing": int(passed.sum()),
                         "cumulative": int(running.sum())})
        return pd.DataFrame(rows).assign(members=int(mask.sum()))

    def _universe_mask(self, filters: Sequence[Filter], pos: int) -> np.ndarray:
        mask = np.ones(len(self.panel.symbols), dtype=bool)
        for f in filters:
            mask &= f.passes(self.panel, pos)
        return mask

    def _st_blocked_table(self) -> Optional[pd.DataFrame]:
        """Wide blocked-dates table from the qf_factor price-band detector
        (built once, opt-in via ``universe.st_band``)."""
        if self._st_blocked is not None:
            return self._st_blocked if not self._st_blocked.empty else None
        from qf_factor.framework.selection.pit_tables import pit_universe_wide
        from qf_factor.framework.selection.st_status import build_st_exclusion_table
        try:
            schedule = build_st_exclusion_table(
                market=self.store.market, provider="lxg",
                start_date=str((self.panel.calendar[0] - pd.Timedelta(days=400)).date()),
                end_date=str(self.panel.calendar[-1].date()),
                freq="M", names_snapshot="", data_dir=str(self.store.data_dir))
        except Exception as exc:  # noqa: BLE001 - the band scan is best-effort
            logger.warning("ST band table unavailable (%s) -- name-snapshot "
                           "exclusion only", exc)
            self._st_blocked = pd.DataFrame()
            return None
        self._st_blocked = pit_universe_wide(schedule) if len(schedule) \
            else pd.DataFrame()
        return self._st_blocked if not self._st_blocked.empty else None

    def _apply_st_band(self, mask: np.ndarray, pos: int) -> np.ndarray:
        blocked = self._st_blocked_table()
        if blocked is None or blocked.empty:
            return mask
        from qf_factor.framework.context import asof_bool
        date = self.panel.calendar[pos]
        keep = np.asarray([not asof_bool(blocked, s, date, default=False)
                           for s in self.panel.symbols], dtype=bool)
        return mask & keep

    # ------------------------------------------------------------- run
    def run(self, spec: ScreenSpec, asof=None) -> ScreenResult:
        """The snapshot: membership decided on ``asof``'s close (default:
        the live row when attached, else the last COMPLETE session — the
        store flags a trailing session whose close count collapsed, which
        is a data update still in flight, not a market move)."""
        if asof is None:
            asof = self.store.decision_date
        pos = self.panel.position_of(asof)
        date = self.panel.calendar[pos]
        mask, rule_masks, scores = self._evaluate(spec, pos)
        if spec.universe.st_band:
            mask = self._apply_st_band(mask, pos)
        ranker = spec.ranker()
        if ranker is not None:
            eligible = np.flatnonzero(mask & np.isfinite(scores))
            order = eligible[np.argsort(-scores[eligible], kind="stable")]
        else:
            order = np.flatnonzero(mask)
        if spec.top_n:
            order = order[:int(spec.top_n)]
        symbols = self.panel.symbols
        members = pd.Index([symbols[i] for i in order], name="symbol")
        member_scores = pd.Series(scores[order], index=members) \
            if ranker is not None else pd.Series(np.nan, index=members, dtype=float)

        funnel = self._funnel_frame(rule_masks, mask)
        return ScreenResult(spec=spec, asof=date, members=members,
                            scores=member_scores, funnel=funnel,
                            panel_values=self._member_snapshot(spec, pos, members),
                            rule_masks=rule_masks, symbols=symbols, store=self.store)

    def _member_snapshot(self, spec: ScreenSpec, pos: int,
                         members: pd.Index) -> pd.DataFrame:
        """Feature values of the members at ``pos`` for the result table —
        every feature any rule mentions, plus name/industry."""
        frame = pd.DataFrame(
            {name: self.panel.cross_section(name, pos)
             for name in spec.mentioned_features()},
            index=self.panel.symbols).loc[members]
        if self.store is not None:
            meta = self.store.universe.meta
            for col, at in (("name", 0), ("industry_l1", 1)):
                values = meta.get(col)
                if values is not None:
                    frame.insert(at, col, values.reindex(members).to_numpy())
        return frame

    # -------------------------------------------------------- schedule
    def schedule(self, spec: ScreenSpec, start=None, end=None, freq: str = None,
                 out_path: Optional[str] = None
                 ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """The causal PIT schedule: membership at ``t`` from data strictly
        before ``t`` (row ``t-1`` of every feature table).

        Returns ``(schedule, funnel)`` — ``schedule`` is a long
        ``date,symbol`` frame (written to ``out_path`` as ``%Y-%m-%d``
        strings when given), the same format
        :func:`qf_factor.framework.selection.universe.build_pit_universe`
        writes and the backtest/live ``pit_universe`` channel reads.
        ``freq`` (``"W"``, ``"M"``, ``"Q"``) snapshots at period-ends
        instead of every session.
        """
        calendar = self.panel.calendar
        lo = max(1, int(calendar.searchsorted(pd.Timestamp(start).normalize())))
        if start is None:
            lo = 1
        hi = len(calendar) - 1 if end is None else min(
            len(calendar) - 1,
            int(calendar.searchsorted(pd.Timestamp(end).normalize(), side="right")) - 1)
        positions = list(range(lo, hi + 1))
        if freq:
            positions = self._period_end_positions(positions, freq)
        blocked = self._st_blocked_table() if spec.universe.st_band else None

        rows: List[Dict] = []
        funnel_sum: Dict[str, List[float]] = {}
        for pos in positions:
            mask, rule_masks, _scores = self._evaluate(spec, pos - 1)
            if blocked is not None:
                mask = self._apply_st_band(mask, pos - 1)
            date = calendar[pos]
            for symbol in self.panel.symbols[mask]:
                rows.append({"date": date, "symbol": symbol})
            for name, passed in rule_masks.items():
                tally = funnel_sum.setdefault(name, [0.0, 0.0])
                tally[0] += int(passed.sum())
                tally[1] += 1
        schedule = pd.DataFrame(rows, columns=["date", "symbol"])
        funnel = pd.DataFrame(
            [{"rule": name, "avg_passing": total / max(count, 1.0)}
             for name, (total, count) in funnel_sum.items()])
        if out_path and not schedule.empty:
            out = Path(out_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            schedule.assign(date=schedule["date"].dt.strftime("%Y-%m-%d")) \
                .to_csv(out, index=False)
            logger.info("wrote PIT schedule (%d rows) to %s", len(schedule), out_path)
        return schedule, funnel

    def _period_end_positions(self, positions: Sequence[int], freq: str) -> List[int]:
        """The LAST session of each ``freq`` period inside ``positions``
        (``"W"``/``"M"``/``"Q"``/``"Y"``)."""
        dates = self.panel.calendar[positions]
        periods = dates.to_period(freq)
        picked: List[int] = []
        last_period = None
        for pos, period in zip(positions, periods):
            if last_period is None or period != last_period:
                picked.append(pos)
                last_period = period
            else:
                picked[-1] = pos          # same period: keep the later session
        return picked

    # -------------------------------------------------------- validate
    def validate(self, spec: ScreenSpec, start=None, end=None,
                 horizons: Sequence[int] = (5, 10, 20), freq: str = "M"
                 ) -> ValidationReport:
        """Forward-return validation of the schedule.

        For each rebalance date ``t`` (period-ends of ``freq``), members
        bought at ``t``'s close are held ``h`` sessions; the report
        compares the members' mean forward return against the baseline of
        every name passing the universe constraints at ``t-1`` — the
        screen's own candidate pool, so the spread isolates the FILTERS
        rather than the universe definition.
        """
        close = self.store.wide("close").astype(float).to_numpy()
        symbols = self.panel.symbols
        calendar = self.panel.calendar
        schedule, _funnel = self.schedule(spec, start, end, freq=freq)
        if schedule.empty:
            return ValidationReport({}, pd.DataFrame())
        uni_filters = spec.universe.filters()
        by_date = {pd.Timestamp(d): set(g["symbol"])
                   for d, g in schedule.groupby("date")}

        rows = []
        for date, members in sorted(by_date.items()):
            t = int(calendar.searchsorted(date))
            if t + max(horizons) >= len(calendar):
                continue
            base_mask = self._universe_mask(uni_filters, t - 1)
            base_idx = np.flatnonzero(base_mask)
            member_idx = [symbols.get_loc(s) for s in members if s in symbols]
            row: Dict = {"date": date, "members": len(member_idx),
                         "baseline": int(base_mask.sum())}
            for h in horizons:
                member_ret = _forward_returns(close, t, h, member_idx)
                base_ret = _forward_returns(close, t, h, base_idx)
                row[f"members_{h}d"] = float(member_ret.mean()) if len(member_ret) \
                    else np.nan
                row[f"baseline_{h}d"] = float(base_ret.mean()) if len(base_ret) \
                    else np.nan
            rows.append(row)
        per_date = pd.DataFrame(rows)
        if per_date.empty:
            return ValidationReport({}, per_date)

        stats: Dict[int, Dict[str, float]] = {}
        for h in horizons:
            joined = per_date[[f"members_{h}d", f"baseline_{h}d"]].dropna()
            spread = joined[f"members_{h}d"] - joined[f"baseline_{h}d"]
            stats[h] = {
                "dates": float(len(joined)),
                "members_mean": float(joined[f"members_{h}d"].mean())
                if len(joined) else np.nan,
                "members_median": float(joined[f"members_{h}d"].median())
                if len(joined) else np.nan,
                "baseline_mean": float(joined[f"baseline_{h}d"].mean())
                if len(joined) else np.nan,
                "spread_mean": float(spread.mean()) if len(spread) else np.nan,
                "spread_t": float(spread.mean()
                                  / (spread.std() / np.sqrt(len(spread))))
                if len(spread) > 1 and spread.std() > 0 else np.nan,
                "hit_rate": float((spread > 0).mean()) if len(spread) else np.nan,
            }
        return ValidationReport(stats, per_date)


def _forward_returns(close: np.ndarray, t: int, h: int,
                     columns: Sequence[int]) -> np.ndarray:
    """``close[t+h] / close[t] - 1`` for the given columns, entries without
    both finite legs dropped."""
    if not len(columns):
        return np.array([])
    cols = np.asarray(columns, dtype=int)
    start = close[t, cols]
    end = close[t + h, cols]
    ok = np.isfinite(start) & (start > 0) & np.isfinite(end)
    return end[ok] / start[ok] - 1.0
