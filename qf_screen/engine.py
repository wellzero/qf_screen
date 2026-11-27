"""The screen engine: one screen definition, every honest view of it.

* :meth:`ScreenEngine.run` — the snapshot.  "Which names pass as of date
  D?" with the full funnel (per-filter pass counts, and how many names each
  rule could not judge for lack of data) and per-symbol explanations.
  Decided on D's close.
* :meth:`ScreenEngine.schedule` — the causal wide-panel export.  The SAME
  filters evaluated at ``t-1`` produce membership at ``t`` (the
  strictly-before ``lag=1`` convention of
  :mod:`qf_factor.framework.selection.screens`), written as a long
  ``date,symbol`` schedule that loads straight into the qf_factor
  backtest/live plumbing.
* :meth:`ScreenEngine.validate` — the historical record of a schedule:
  forward returns at several horizons against the screen's own candidate
  pool, plus the path of the equal-weight portfolio that follows it
  (turnover, annualized return, Sharpe, max drawdown, benchmarks) —
  gross, and net of transaction costs with a limit-up entry guard when
  asked (``cost_bps`` / ``skip_limit_up``).
* :meth:`ScreenEngine.diff` — what changed between two snapshots of one
  screen (entered / exited / rank movers).
* :meth:`ScreenEngine.inspect` — one name through every preset.
* :meth:`ScreenEngine.compare` — several presets over one window, side by
  side.

One evaluation function backs every mode, so the snapshot a user sees, the
schedule a backtest consumes and the diff a trader reads cannot drift apart
in semantics.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd

from qf_screen.analytics import limit_up_mask, perf, simulate, spread_stats
from qf_screen.data import BOARD_LABELS, BOARD_LIMITS, CNDataStore
from qf_screen.presets import REQUIRED_HYGIENE, list_presets, load_preset, validate_filters
from qf_screen.rules import (
    BoardFilter, CombinedRanker, ExprFilter, ExprRanker, FeaturePanel, Filter,
    SymbolSetFilter, expr_names, feature_names,
)

logger = logging.getLogger(__name__)

#: tag carried by names with no industry classification (no local
#: ``lxg/industries.csv`` and no ``--live`` 东财 fallback)
UNCATEGORIZED = "未分类"

#: index codes ``validate`` compares against when the store carries their
#: lxg file under ``cn/index/`` (CSI 300 / CSI 500)
DEFAULT_BENCHMARKS: Tuple[str, ...] = ("000300", "000905")
BENCHMARK_LABELS: Dict[str, str] = {
    "000001": "sse", "000300": "csi300", "000905": "csi500", "000852": "csi1000",
    "399006": "szse",
}

#: the per-name scorecard of ``inspect``: family -> features, in display
#: order (features the registry does not know are skipped)
INSPECT_STATS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("valuation", ("pe_ttm", "pb", "ps_ttm", "peg", "market_cap_bn",
                   "float_market_cap_bn", "earnings_yield")),
    ("quality", ("roe_best", "roe", "roa", "gross_margin", "net_margin",
                 "debt_assets", "fcf_yield")),
    ("growth", ("rev_growth", "profit_growth", "net_margin_py")),
    ("dividend", ("div_yield_ttm",)),
    ("momentum", ("mom_63s21", "mom_126s21", "ret_21d", "ma_ratio_60",
                  "ma_ratio_200", "high252_ratio")),
    ("risk", ("vol_60d", "max21")),
    ("liquidity", ("amount_ma20_mn", "turnover_ma20", "close_raw")),
    ("alt", ("nb_hold_chg_20d", "margin_chg_20d", "holder_num_chg", "pledge_ratio",
             "report_cnt_90d", "buy_report_ratio_90d", "insider_net_180d_mc",
             "block_discount_180d")),
)


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
        if self.symbols:
            out.append(SymbolSetFilter(self.symbols))
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
    #: diversity cap: at most this many members per industry.  Applied
    #: AFTER the ranking: a name whose industry has filled its quota is
    #: skipped and the next-best names take the slots, so the list stays
    #: ``top_n`` long.  The tag is the local 申万 first-level industry,
    #: else the ``--live`` 东财 board (:meth:`CNDataStore.enrich_industry_tags`);
    #: names with neither are never capped (they share no industry).  The
    #: classification is TODAY's — a historical schedule is capped with it
    #: too, which is the one non-point-in-time input of the engine.
    max_per_industry: Optional[int] = None

    def __post_init__(self) -> None:
        cap = self.max_per_industry
        if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int)
                                or cap < 1):
            raise ValueError(f"max_per_industry must be a positive integer, "
                             f"got {cap!r}")

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
                   description=doc.get("description", ""), source=doc.get("source"),
                   max_per_industry=doc.get("max_per_industry"))
        validate_filters(spec.filters)
        return spec

    @classmethod
    def adhoc(cls, where: Sequence[str] = (), rank: Optional[str] = None,
              top_n: Optional[int] = None, boards: Sequence[str] = ("all",),
              name: str = "adhoc") -> "ScreenSpec":
        """A screen with no preset behind it (``qf-screen run --where ...``):
        the required A-share hygiene block — traded, price >= 2 CNY, 20d
        mean traded value >= 5e7 — over the whole universe minus ST names
        and names listed for less than a year, plus the given DSL filters.
        Nothing else is hidden in it."""
        filters = [{"name": rule, "expr": expr, "description": "hygiene"}
                   for rule, expr in REQUIRED_HYGIENE]
        filters += [{"name": f"where{i + 1}", "expr": expr, "description": "CLI --where"}
                    for i, expr in enumerate(where)]
        validate_filters(filters)
        return cls(name=name, filters=filters,
                   rank=[{"expr": rank, "weight": 1.0}] if rank else [],
                   top_n=top_n, universe=UniverseSpec(
                       boards=tuple(boards), exclude_st=True, min_listed_days=365),
                   description="ad-hoc screen (hygiene block + --where filters)",
                   source="CLI")

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


#: the reference portfolio of :meth:`ScreenEngine.compare`: every name
#: with a price the session before, equal-weighted -- ONE benchmark common
#: to presets that screen different boards (each preset's own candidate
#: pool stays its baseline in ``excess_ann`` / ``spread_Nd``)
BENCHMARK_UNIVERSE = ScreenSpec(
    name="universe", filters=[{"name": "priced", "expr": "close_raw > 0"}],
    universe=UniverseSpec(boards=("all",), exclude_st=False, min_listed_days=None))


# --------------------------------------------------------------- results
def _text(value) -> str:
    """A tag as ``str`` (``""`` for missing / NaN / None)."""
    return value.strip() if isinstance(value, str) else ""


@dataclass
class ScreenResult:
    """The outcome of one snapshot run."""
    spec: ScreenSpec
    asof: pd.Timestamp
    members: pd.Index                       # ranked, capped, truncated to top_n
    scores: pd.Series                       # composite score per member
    funnel: pd.DataFrame                    # rule | passing | unknown | na_pass | cumulative | members
    panel_values: pd.DataFrame              # feature snapshot of every candidate
    rule_masks: Dict[str, np.ndarray] = field(repr=False, default_factory=dict)
    symbols: pd.Index = field(repr=False, default=None)   # alignment of rule_masks
    store: Optional[CNDataStore] = field(repr=False, default=None)
    #: DSL text of each rule, for explain output: name -> expression
    rule_exprs: Dict[str, str] = field(repr=False, default_factory=dict)
    #: data caveats (stale alt sources ...) discovered while running
    warnings: List[str] = field(default_factory=list)
    #: per rule: the names it could not judge (every mentioned feature missing)
    rule_unknown: Dict[str, np.ndarray] = field(repr=False, default_factory=dict)
    #: names the ``max_per_industry`` cap kept OUT of the final list
    capped: pd.Index = field(repr=False, default_factory=lambda: pd.Index([], name="symbol"))

    def explain(self, symbol: str) -> Dict:
        """Why is this name in or out?  Per-rule pass/fail WITH the
        expression that produced it (available for every candidate, members
        and rejects alike), the feature values the rules read (every
        candidate), the score and its percentile, and the rules the name
        passed only because their data is missing (``na_pass``)."""
        symbol = str(symbol)
        row = {"symbol": symbol, "asof": self.asof,
               "member": symbol in set(self.members)}
        row["name"] = self.store.universe.get(symbol, "name", "") \
            if self.store is not None else ""
        row["rules"] = {}
        row["unknown"] = []
        if self.symbols is not None and symbol in self.symbols:
            loc = self.symbols.get_loc(symbol)
            row["rules"] = {name: bool(mask[loc]) for name, mask in self.rule_masks.items()}
            row["unknown"] = [name for name, unknown in self.rule_unknown.items()
                              if unknown[loc]]
        row["rule_exprs"] = dict(self.rule_exprs)
        row["features"] = self.panel_values.loc[symbol].to_dict() \
            if symbol in self.panel_values.index else {}
        if symbol in set(self.members):
            score = self.scores.get(symbol, np.nan)
            if np.isfinite(score):
                row["score"] = float(score)
                row["score_percentile"] = float((self.scores < score).mean())
                row["rank"] = int(self.members.get_loc(symbol)) + 1
        return row

    def to_frame(self) -> pd.DataFrame:
        out = self.panel_values.reindex(self.members).copy()
        out.insert(0, "score", self.scores.reindex(self.members).to_numpy())
        out.insert(0, "symbol", self.members)
        return out.reset_index(drop=True)

    def industry_mix(self) -> pd.Series:
        """Members per industry, largest first (``未分类`` for names with no
        classification)."""
        if len(self.members) == 0 or "industry_l1" not in self.panel_values.columns:
            return pd.Series(dtype=int, name="members")
        tags = [_text(tag) or UNCATEGORIZED
                for tag in self.panel_values.loc[self.members, "industry_l1"]]
        return pd.Series(tags).value_counts().rename("members")

    def notes(self) -> List[str]:
        """Caveats a reader of this snapshot should not miss: rules that let
        names through for lack of data (and how many), and the names the
        industry cap displaced."""
        out: List[str] = []
        if {"unknown", "na_pass"} <= set(self.funnel.columns):
            for _, row in self.funnel[self.funnel["na_pass"]
                                      & (self.funnel["unknown"] > 0)].iterrows():
                out.append(f"{row['rule']}: {int(row['unknown'])} names passed WITHOUT "
                           f"data (na_pass) -- no source on disk to judge them")
        if len(self.capped):
            out.append(f"max_per_industry {self.spec.max_per_industry}: "
                       f"{', '.join(self.capped)} ranked in but was displaced")
        return out

    def summary(self) -> str:
        names = ""
        if self.store is not None:
            names = ", ".join(f"{s} {self.store.universe.get(s, 'name', '')}"
                              for s in self.members[:5])
        return (f"{self.spec.name}: {len(self.members)} members as of "
                f"{self.asof.date()}" + (f" -- {names} ..." if names else ""))


@dataclass
class ScreenDiff:
    """Membership change of one screen between two snapshots: the current
    one and an earlier one (by default the session before)."""
    spec: ScreenSpec
    current: ScreenResult
    previous: ScreenResult

    @property
    def asof(self) -> pd.Timestamp:
        return self.current.asof

    @property
    def prev(self) -> pd.Timestamp:
        return self.previous.asof

    @property
    def ranked(self) -> bool:
        return bool(self.spec.rank)

    def _with_rank(self, frame: pd.DataFrame, result: ScreenResult) -> pd.DataFrame:
        out = frame.copy()
        if self.ranked:
            ranks = {symbol: i + 1 for i, symbol in enumerate(result.members)}
            out.insert(1, "rank", [ranks[s] for s in out["symbol"]])
        return out

    @property
    def entered(self) -> pd.DataFrame:
        """Current rows of the names that were NOT members before."""
        frame = self.current.to_frame()
        return self._with_rank(frame[~frame["symbol"].isin(set(self.previous.members))],
                               self.current)

    @property
    def exited(self) -> pd.DataFrame:
        """Previous rows (with the rank they held) of the names that are no
        longer members."""
        frame = self.previous.to_frame()
        return self._with_rank(frame[~frame["symbol"].isin(set(self.current.members))],
                               self.previous)

    @property
    def retained(self) -> int:
        return len(set(self.current.members) & set(self.previous.members))

    @property
    def movers(self) -> pd.DataFrame:
        """Names in both snapshots whose rank moved: ``symbol, name,
        rank_prev, rank_now, change`` (positive = up), ``score_prev``,
        ``score_now`` — biggest gains first.  Empty for a screen with no
        ranking (its order carries no meaning)."""
        columns = ["symbol", "name", "rank_prev", "rank_now", "change",
                   "score_prev", "score_now"]
        if not self.ranked:
            return pd.DataFrame(columns=columns)
        before = {symbol: i + 1 for i, symbol in enumerate(self.previous.members)}
        after = {symbol: i + 1 for i, symbol in enumerate(self.current.members)}
        names = self.current.panel_values["name"] if "name" in self.current.panel_values \
            else pd.Series("", index=self.current.panel_values.index)
        rows = []
        for symbol in self.current.members:
            if symbol in before and before[symbol] != after[symbol]:
                rows.append({
                    "symbol": symbol, "name": _text(names.get(symbol, "")),
                    "rank_prev": before[symbol], "rank_now": after[symbol],
                    "change": before[symbol] - after[symbol],
                    "score_prev": float(self.previous.scores.get(symbol, np.nan)),
                    "score_now": float(self.current.scores.get(symbol, np.nan))})
        out = pd.DataFrame(rows, columns=columns)
        return out.sort_values("change", ascending=False, kind="stable") \
            .reset_index(drop=True) if len(out) else out

    def summary(self) -> str:
        kept = self.retained
        share = kept / len(self.previous.members) if len(self.previous.members) else np.nan
        return (f"{self.spec.name}: {len(self.current.members)} members as of "
                f"{self.asof.date()} vs {len(self.previous.members)} as of "
                f"{self.prev.date()} -- entered {len(self.entered)}, exited "
                f"{len(self.exited)}, retained {kept}"
                + (f" ({share:.0%})" if np.isfinite(share) else ""))


@dataclass
class StockCard:
    """One name through every preset (:meth:`ScreenEngine.inspect`)."""
    symbol: str
    asof: pd.Timestamp
    info: Dict[str, object]
    presets: List[Dict[str, object]]
    stats: pd.DataFrame                        # family | feature | value | pct_rank

    @property
    def passed(self) -> List[str]:
        return [str(card["preset"]) for card in self.presets if card["member"]]


def _number(value, digits: int = 2) -> str:
    return f"{value:.{digits}f}" if value is not None and np.isfinite(value) else "  n/a"


@dataclass
class ValidationReport:
    """Forward-return validation of a schedule.

    ``horizons`` keeps the per-horizon statistics (members vs the screen's
    own candidate pool); ``stats`` the path statistics of the portfolios —
    ``portfolio`` (the schedule, equal-weight, rebalanced on each schedule
    date), ``universe`` (every name passing the universe constraints, the
    same weighting) and each benchmark that the store carries; ``spread``
    the portfolio's excess over the universe; ``turnover`` how much of the
    list changes per rebalance; ``curve`` the cumulative value of each
    series on every session of the window.

    Execution (both opt-in): with ``cost_bps > 0`` the report also carries
    the NET figures — ``stats["portfolio_net"]``, the ``portfolio_net``
    curve and ``net`` (the net portfolio's excess over the same gross
    universe baseline) — and ``per_date["cost"]`` what each rebalance
    paid; with ``skip_limit_up`` the ``skipped`` tally and
    ``per_date["skipped"]`` count the entries the guard refused.  Both act
    on the portfolio path only: the horizon block measures the SCREEN
    (every schedule member, gross), the path measures the tradable book.
    """
    horizons: Dict[int, Dict[str, float]]
    per_date: pd.DataFrame
    stats: Dict[str, Dict[str, float]] = field(default_factory=dict)
    spread: Dict[str, float] = field(default_factory=dict)
    turnover: Dict[str, float] = field(default_factory=dict)
    curve: Optional[pd.DataFrame] = None
    notes: List[str] = field(default_factory=list)
    #: transaction cost per side the ``*_net`` figures are net of (0 = gross only)
    cost_bps: float = 0.0
    #: the net portfolio's excess over the candidate pool (empty while ``cost_bps`` is 0)
    net: Dict[str, float] = field(default_factory=dict)
    #: the limit-up guard's tally — ``entries``, ``skipped``, ``share`` (empty when off)
    skipped: Dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        lines = []
        for h in sorted(self.horizons):
            s = self.horizons[h]
            lines.append(
                f"{h:>3}d  members {s['members_mean']:+.2%} (median "
                f"{s['members_median']:+.2%})  baseline {s['baseline_mean']:+.2%}  "
                f"spread {s['spread_mean']:+.2%}  hit {s['hit_rate']:.0%}  "
                f"t={s['spread_t']:.1f}  ({int(s['dates'])} dates)")
        if self.stats:
            lines.append("")
            for label, stats in self.stats.items():
                lines.append(
                    f"{label:13s} ann {stats['ann_return']:+.1%}  "
                    f"vol {stats['ann_vol']:.1%}  Sharpe {_number(stats['sharpe'])}  "
                    f"maxDD {stats['max_drawdown']:+.1%}  cum {stats['cum_return']:+.1%}"
                    f"  ({int(stats['days'])} sessions)")
        for label, spread in (("excess", self.spread), ("excess_net", self.net)):
            if spread:
                lines.append(
                    f"{label:13s} vs universe {spread['excess_ann']:+.1%}/yr  "
                    f"information ratio {_number(spread['information_ratio'])}  "
                    f"tracking error {spread['tracking_error']:.1%}")
        if self.turnover:
            lines.append(
                f"turnover    {self.turnover['mean']:.0%} of the list per rebalance "
                f"(max {self.turnover['max']:.0%}, {int(self.turnover['rebalances'])} "
                f"rebalances, {self.turnover['members_mean']:.1f} names each)")
        if self.cost_bps:
            lines.append(f"costs        {self.cost_bps:g} bps per side, on the initial "
                         "purchase, each rebalance's sells and buys and the final "
                         "liquidation; the baselines are gross")
        if self.skipped:
            lines.append(
                f"limit-up     skipped {int(self.skipped['skipped'])} of "
                f"{int(self.skipped['entries'])} entries ({self.skipped['share']:.1%}) "
                "that closed at the daily limit on their entry day")
        lines.extend(self.notes)
        return "\n".join(lines) or "no validation dates"


# ---------------------------------------------------------------- engine
class ScreenEngine:
    """Runs a :class:`ScreenSpec` over a store in its several modes."""

    def __init__(self, store: CNDataStore, panel: Optional[FeaturePanel] = None):
        self.store = store
        self.panel = panel if panel is not None else FeaturePanel(store)
        self._st_blocked: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------ shared
    def _evaluate(self, spec: ScreenSpec, pos: int, filters: Optional[Sequence[Filter]] = None,
                  ranker: Optional[object] = None
                  ) -> Tuple[np.ndarray, Dict[str, np.ndarray], Optional[np.ndarray],
                             Dict[str, np.ndarray]]:
        """Universe + screen filters ANDed at row ``pos``.

        Returns ``(mask, rule_masks, scores, rule_unknown)`` —
        ``rule_masks`` keeps each rule's own pass array for the funnel and
        :meth:`ScreenResult.explain`, ``rule_unknown`` the names each rule
        could not judge, and ``scores`` is the blended rank over the whole
        cross-section.  ``filters`` / ``ranker`` may be passed prebuilt (a
        schedule over many dates compiles the DSL once, not once per date).
        """
        rule_masks: Dict[str, np.ndarray] = {}
        rule_unknown: Dict[str, np.ndarray] = {}
        mask = np.ones(len(self.panel.symbols), dtype=bool)
        for f in filters if filters is not None else spec.all_filters():
            passed, unknown = f.evaluate(self.panel, pos)
            rule_masks[f.name] = passed
            if unknown is not None:
                rule_unknown[f.name] = unknown
            mask &= passed
        if ranker is None:
            ranker = spec.ranker()
        scores = ranker.score(self.panel, pos) if ranker is not None \
            else np.full(len(self.panel.symbols), np.nan)
        return mask, rule_masks, scores, rule_unknown

    @staticmethod
    def _rank_members(spec: ScreenSpec, mask: np.ndarray, scores: Optional[np.ndarray],
                      symbols: pd.Index, industry: Optional[np.ndarray] = None,
                      capped_out: Optional[List[str]] = None) -> pd.Index:
        """Mask passers as a RANKED index — the one selection every mode
        shares.

        With a ranker, only names with a finite composite score are
        eligible (a missing score cannot rank) and the order is
        score-descending.  ``spec.max_per_industry`` (with ``industry``, a
        tag per symbol where ``""`` means unclassified) is applied WHILE
        the list is filled: a name whose industry already holds the quota
        is skipped and the next-best name takes the slot, so the cap costs
        the screen nothing but crowding; names the cap displaced from the
        final list are appended to ``capped_out``.  ``top_n`` truncates.
        """
        if scores is not None:
            eligible = np.flatnonzero(mask & np.isfinite(scores))
            order = eligible[np.argsort(-scores[eligible], kind="stable")]
        else:
            order = np.flatnonzero(mask)
        top = int(spec.top_n) if spec.top_n else None
        cap = int(spec.max_per_industry) if spec.max_per_industry else 0
        if cap and industry is not None:
            quota: Dict[str, int] = {}
            keep = np.zeros(len(order), dtype=bool)
            taken = 0
            for row, i in enumerate(order):
                tags = [tag for tag in _text(industry[i]).split("/") if tag]
                if any(quota.get(tag, 0) >= cap for tag in tags):
                    continue                        # this industry is full
                for tag in tags:
                    quota[tag] = quota.get(tag, 0) + 1
                keep[row] = True
                taken += 1
                if top is not None and taken >= top:
                    break
            chosen = order[keep]
            if capped_out is not None:
                reference = order[:top] if top is not None else order
                picked = set(int(i) for i in chosen)
                capped_out.extend(str(symbols[i]) for i in reference
                                  if int(i) not in picked)
        else:
            chosen = order[:top] if top is not None else order
        return pd.Index([symbols[i] for i in chosen], name="symbol")

    def _industry_tags(self) -> np.ndarray:
        """Industry tag per loaded symbol (``""`` = unclassified): the
        static ``industry_l1`` feature — local 申万, or the 东财 board a
        ``--live`` run filled in."""
        values = self.panel.cross_section("industry_l1", 0)
        return np.array([_text(tag) for tag in values], dtype=object)

    def _funnel_frame(self, rule_masks: Dict[str, np.ndarray],
                      rule_unknown: Dict[str, np.ndarray], mask: np.ndarray,
                      filters: Sequence[Filter]) -> pd.DataFrame:
        """CUMULATIVE funnel: each row shows how many names survive after
        ANDing that rule in — the narrowing that explains the result — and
        how many names the rule could not judge (``unknown``: every feature
        it mentions missing; they fail a strict rule and pass a
        ``na_pass`` one)."""
        na_rules = {f.name for f in filters if getattr(f, "na_pass", False)}
        rows = []
        running = np.ones(len(self.panel.symbols), dtype=bool)
        for name, passed in rule_masks.items():
            running &= passed
            rows.append({"rule": name, "passing": int(passed.sum()),
                         "unknown": int(rule_unknown[name].sum())
                         if name in rule_unknown else 0,
                         "na_pass": name in na_rules,
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
        filters = spec.all_filters()
        ranker = spec.ranker()
        mask, rule_masks, scores, rule_unknown = self._evaluate(spec, pos, filters, ranker)
        if spec.universe.st_band:
            mask = self._apply_st_band(mask, pos)
        symbols = self.panel.symbols
        industry = self._industry_tags() if spec.max_per_industry else None
        capped: List[str] = []
        members = self._rank_members(spec, mask, scores if ranker is not None else None,
                                     symbols, industry, capped)
        member_scores = pd.Series(
            [scores[symbols.get_loc(s)] for s in members], index=members,
            dtype=float) if ranker is not None \
            else pd.Series(np.nan, index=members, dtype=float)

        funnel = self._funnel_frame(rule_masks, rule_unknown, mask, filters)
        exprs = {f.name: getattr(f, "expr", f.description or "")
                 for f in filters}
        return ScreenResult(spec=spec, asof=date, members=members,
                            scores=member_scores, funnel=funnel,
                            panel_values=self._candidate_snapshot(spec, pos),
                            rule_masks=rule_masks, symbols=symbols, store=self.store,
                            rule_exprs=exprs, rule_unknown=rule_unknown,
                            capped=pd.Index(sorted(capped), name="symbol"),
                            warnings=list(self.panel.warnings)
                            + [f"feature {name} ends {last.date()} "
                               f"({share:.0%} of covered symbols stale) -- screens on "
                               f"it decide on old data"
                               for name, last, share in self.panel.stale_features()])

    def _candidate_snapshot(self, spec: ScreenSpec, pos: int) -> pd.DataFrame:
        """Feature values of EVERY candidate at ``pos`` — members and
        rejects alike, so :meth:`ScreenResult.explain` can show what a
        rejected name's filters actually read.  The result table takes the
        member rows (:meth:`ScreenResult.to_frame` reindexes)."""
        frame = pd.DataFrame(
            {name: self.panel.cross_section(name, pos)
             for name in spec.mentioned_features()},
            index=self.panel.symbols)
        if self.store is not None:
            meta = self.store.universe.meta
            for col, at in (("name", 0), ("industry_l1", 1)):
                values = meta.get(col)
                if values is not None:
                    frame.insert(at, col, values.reindex(frame.index).to_numpy())
        return frame

    # -------------------------------------------------------- schedule
    def schedule(self, spec: ScreenSpec, start=None, end=None, freq: str = None,
                 out_path: Optional[str] = None, apply_top_n: bool = True
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

        Ranking semantics MATCH :meth:`run`: with a ``rank`` block the
        schedule keeps only names with a finite composite score, ordered
        score-descending, ``top_n`` truncates each date to the best N
        and ``max_per_industry`` caps each date the same way (pass
        ``apply_top_n=False`` for the historical all-passers export — what
        v0.1 wrote, e.g. to study the pre-truncation pool: no truncation
        and no cap).  The industry tags are TODAY's classification (see
        :class:`ScreenSpec`).
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
        filters = spec.all_filters()          # compiled once, not per date
        ranker = spec.ranker()
        eff_spec = spec if apply_top_n else ScreenSpec(
            name=spec.name, filters=spec.filters, rank=spec.rank, top_n=None,
            universe=spec.universe, description=spec.description, source=spec.source)
        industry = self._industry_tags() if apply_top_n and spec.max_per_industry \
            else None

        rows: List[Dict] = []
        funnel_sum: Dict[str, List[float]] = {}
        for pos in positions:
            mask, rule_masks, scores, rule_unknown = self._evaluate(spec, pos - 1, filters, ranker)
            if blocked is not None:
                mask = self._apply_st_band(mask, pos - 1)
            date = calendar[pos]
            members = self._rank_members(eff_spec, mask,
                                          scores if ranker is not None else None,
                                          self.panel.symbols, industry)
            for symbol in members:
                rows.append({"date": date, "symbol": symbol})
            for name, passed in rule_masks.items():
                tally = funnel_sum.setdefault(name, [0.0, 0.0, 0.0])
                tally[0] += int(passed.sum())
                if name in rule_unknown:
                    tally[1] += int(rule_unknown[name].sum())
                tally[2] += 1
        schedule = pd.DataFrame(rows, columns=["date", "symbol"])
        funnel = pd.DataFrame(
            [{"rule": name, "avg_passing": passed / max(count, 1.0),
              "avg_unknown": unknown / max(count, 1.0)}
             for name, (passed, unknown, count) in funnel_sum.items()])
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
    def _limit_up_table(self) -> np.ndarray:
        """``date x symbol``: each session's raw close sits at the daily
        limit-up of its board (:func:`~qf_screen.analytics.limit_up_mask`)."""
        meta = self.store.universe.meta
        boards = meta["board"].reindex(self.panel.symbols) if "board" in meta.columns \
            else [""] * len(self.panel.symbols)
        return limit_up_mask(self.store.wide("close_raw").to_numpy(), boards, BOARD_LIMITS)

    def validate(self, spec: ScreenSpec, start=None, end=None,
                 horizons: Sequence[int] = (5, 10, 20), freq: str = "M",
                 apply_top_n: bool = True,
                 benchmarks: Sequence[str] = DEFAULT_BENCHMARKS,
                 cost_bps: float = 0.0, skip_limit_up: bool = False
                 ) -> ValidationReport:
        """The historical record of the schedule over ``start`` .. ``end``.

        For each rebalance date ``t`` (period-ends of ``freq``), members
        bought at ``t``'s close are held ``h`` sessions; the horizon
        statistics compare the members' mean forward return against the
        baseline of every name passing the universe constraints at ``t-1``
        — the screen's own candidate pool, so the spread isolates the
        FILTERS rather than the universe definition.

        On top of that the report follows the equal-weight portfolio that
        acts on the schedule (rebalanced at every schedule date, held in
        between; see :mod:`qf_screen.analytics`) and the same portfolio of
        the candidate pool, and benchmarks it against each index whose lxg
        file the store carries (:data:`DEFAULT_BENCHMARKS`).  ``apply_top_n``
        passes to :meth:`schedule`.

        Two execution assumptions are opt-in (``qf-screen validate
        --cost-bps / --skip-limit-up``), and both act on the portfolio
        path alone:

        * ``cost_bps`` — the transaction cost per side of a trade, in basis
          points; the report then carries the net figures next to the gross
          ones (:class:`ValidationReport`, :func:`~qf_screen.analytics.simulate`).
        * ``skip_limit_up`` — a member whose raw close sits at the daily
          limit-up on its entry day cannot be bought at that close, and is
          not: it is left out of that holding period (its share of the
          book stays in cash) and counted in ``skipped``.  The guard reads
          the close against the previous traded one, by board (10 % main,
          20 % ChiNext / STAR, 30 % Beijing; see
          :func:`~qf_screen.analytics.limit_up_mask`).
        """
        close = self.store.wide("close").astype(float)
        symbols = self.panel.symbols
        calendar = self.panel.calendar
        reach = int(max(horizons))
        limit_up = self._limit_up_table() if skip_limit_up else None
        schedule, _funnel = self.schedule(spec, start, end, freq=freq,
                                          apply_top_n=apply_top_n)
        if schedule.empty:
            return ValidationReport({}, pd.DataFrame())
        uni_filters = spec.universe.filters()
        by_date = {pd.Timestamp(d): set(g["symbol"])
                   for d, g in schedule.groupby("date")}

        closes = close.to_numpy()
        prices = close.ffill().to_numpy()     # a suspended name is held at its last close
        rows = []
        holdings: List[Tuple[int, np.ndarray]] = []
        base_holdings: List[Tuple[int, np.ndarray]] = []
        tally = {"entries": 0.0, "skipped": 0.0}
        for date, members in sorted(by_date.items()):
            t = int(calendar.searchsorted(date))
            if t + reach >= len(calendar):
                continue
            base_mask = self._universe_mask(uni_filters, t - 1)
            base_idx = np.flatnonzero(base_mask)
            member_idx = np.array([symbols.get_loc(s) for s in sorted(members)
                                   if s in symbols], dtype=int)
            # what a book could act on at this close: a name locked at
            # limit-up has no seller at that price
            tradable = member_idx
            if limit_up is not None and len(member_idx):
                tradable = member_idx[~limit_up[t, member_idx]]
            tally["entries"] += len(member_idx)
            tally["skipped"] += len(member_idx) - len(tradable)
            row: Dict = {"date": date, "members": len(member_idx),
                         "baseline": int(base_mask.sum())}
            if limit_up is not None:
                row["skipped"] = len(member_idx) - len(tradable)
            for h in horizons:
                member_ret = _forward_returns(closes, t, h, member_idx)
                base_ret = _forward_returns(closes, t, h, base_idx)
                row[f"members_{h}d"] = float(member_ret.mean()) if len(member_ret) \
                    else np.nan
                row[f"baseline_{h}d"] = float(base_ret.mean()) if len(base_ret) \
                    else np.nan
            rows.append(row)
            holdings.append((t, tradable))
            base_holdings.append((t, base_idx))
        per_date = pd.DataFrame(rows)
        if per_date.empty or not holdings:
            return ValidationReport({}, per_date, notes=[
                "no rebalance date leaves enough sessions after it for the "
                f"horizons (needs {reach}) -- widen --end or shorten --horizons"])

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
        notes: List[str] = []
        exits = [t for t, _ in holdings[1:]] + [holdings[-1][0] + reach]
        portfolio = simulate(prices, holdings, exits)
        baseline = simulate(prices, base_holdings, exits)
        report = ValidationReport(stats, per_date, notes=notes, cost_bps=float(cost_bps))
        if skip_limit_up:
            report.skipped = {**tally, "share": tally["skipped"] / tally["entries"]
                              if tally["entries"] else np.nan}
        if portfolio.empty or baseline.empty:
            notes.append("no holding period could be simulated (no prices after the "
                         "first rebalance)")
            return report
        port_returns = pd.Series(portfolio.returns(), index=calendar[portfolio.positions])
        base_returns = pd.Series(baseline.returns(), index=calendar[baseline.positions]) \
            .reindex(port_returns.index)
        net = simulate(prices, holdings, exits, cost_bps=cost_bps) if cost_bps > 0 \
            else None
        if net is not None and net.empty:
            net = None
        curve: Dict[str, pd.Series] = {
            "portfolio": pd.Series(portfolio.values, index=port_returns.index),
            "universe": pd.Series(baseline.values, index=port_returns.index),
        }
        report.stats["portfolio"] = perf(port_returns)
        report.stats["universe"] = perf(base_returns)
        if net is not None:
            net_returns = pd.Series(net.returns(), index=calendar[net.positions])
            report.stats["portfolio_net"] = perf(net_returns)
            curve["portfolio_net"] = pd.Series(net.values, index=net_returns.index)
        for code in benchmarks or ():
            label = BENCHMARK_LABELS.get(str(code), str(code))
            series = self.store.index_close(code)
            if series is None:
                notes.append(f"{label}: no lxg index file in the store -- skipped")
                continue
            aligned = series.reindex(calendar, method="ffill")
            window = aligned.iloc[portfolio.positions[0] - 1: portfolio.positions[-1] + 1]
            returns = window.pct_change().dropna()
            if returns.empty:
                notes.append(f"{label}: the index file does not cover the window -- skipped")
                continue
            if len(returns) < len(port_returns):
                notes.append(f"{label}: the lxg index file ends "
                             f"{returns.index[-1].date()} -- its curve stops there")
            report.stats[label] = perf(returns)
            curve[label] = pd.Series((1.0 + returns).cumprod().to_numpy(),
                                     index=returns.index)
        report.curve = pd.DataFrame(curve)
        report.spread = spread_stats(port_returns - base_returns)
        report.spread["excess_cum"] = float(
            report.curve["portfolio"].iloc[-1] - report.curve["universe"].iloc[-1])
        if net is not None:
            # the net book against the same (gross) baseline
            report.net = spread_stats(net_returns - base_returns.reindex(net_returns.index))
            report.net["excess_cum"] = float(
                report.curve["portfolio_net"].iloc[-1] - report.curve["universe"].iloc[-1])
            report.net["cost_drag_ann"] = float(
                report.stats["portfolio"]["ann_return"]
                - report.stats["portfolio_net"]["ann_return"])
        turnover = portfolio.periods["turnover"]
        report.turnover = {
            "mean": float(turnover.mean()) if turnover.notna().any() else np.nan,
            "max": float(turnover.max()) if turnover.notna().any() else np.nan,
            "rebalances": float(turnover.notna().sum()),
            "members_mean": float(portfolio.periods["members"].mean()),
        }
        periods = portfolio.periods.assign(
            date=calendar[portfolio.periods["entry"].to_numpy()])
        report.per_date = per_date.merge(
            periods[["date", "period_return", "turnover"]]
            .rename(columns={"period_return": "portfolio"}),
            on="date", how="left")
        if net:
            # the net walk holds the same book over the same periods
            report.per_date["cost"] = net.periods["cost"].to_numpy()
            values = report.curve["portfolio_net"]
            report.per_date["portfolio_net_cum"] = [
                float(values.get(date, 1.0)) if date >= values.index[0] else 1.0
                for date in report.per_date["date"]]
        for column in report.curve.columns:      # cumulative value at each rebalance
            values = report.curve[column]
            report.per_date[f"{column}_cum"] = [
                float(values.get(date, 1.0)) if date >= values.index[0] else 1.0
                for date in report.per_date["date"]]
        return report

    # ------------------------------------------------------------ diff
    def diff(self, spec: ScreenSpec, asof=None, prev=None) -> ScreenDiff:
        """What changed in ``spec`` between the snapshot decided on
        ``prev``'s close and the one decided on ``asof``'s (default: the
        decision date, and the session just before it).  Both snapshots are
        the ordinary :meth:`run` — the same rules, the same ranking — so a
        name that left did so because a rule or the ranking moved it."""
        current = self.run(spec, asof=asof)
        if prev is None:
            pos = self.panel.position_of(current.asof)
            if pos == 0:
                raise ValueError(f"no session before {current.asof.date()} in the "
                                 f"panel ({self.panel.calendar[0].date()}.."
                                 f"{self.panel.calendar[-1].date()})")
            prev = self.panel.calendar[pos - 1]
        previous = self.run(spec, asof=prev)
        if previous.asof >= current.asof:
            raise ValueError(f"--prev {previous.asof.date()} is not an earlier "
                             f"session than {current.asof.date()}")
        return ScreenDiff(spec=spec, current=current, previous=previous)

    # --------------------------------------------------------- inspect
    def inspect(self, symbol: str, asof=None, presets: Optional[Sequence[str]] = None
                ) -> StockCard:
        """One name through every preset (or the named ones): does it pass,
        which rules reject it and what those rules read, where it ranks
        when it is a member — or, when it clears every rule and still is
        not one, where it sits among the passers (``pool_rank`` of
        ``pool``: only the top ``members`` are kept) — and a scorecard of
        its own features with each one's cross-sectional percentile."""
        symbol = str(symbol).strip()
        panel = self.panel
        if symbol not in panel.symbols:
            raise ValueError(
                f"{symbol!r} is not in the loaded universe ({len(panel.symbols)} names "
                f"with price data); check the spelling (SH600519, SZ000001, BJ920001)")
        date = self.store.decision_date if asof is None \
            else pd.Timestamp(asof).normalize()
        cards: List[Dict[str, object]] = []
        for name in list(presets) if presets else list_presets():
            spec = ScreenSpec.from_yaml(name)
            result = self.run(spec, asof=date)
            info = result.explain(symbol)
            values = info["features"]
            unknown = set(info["unknown"])
            failed = []
            for rule, passed in info["rules"].items():
                if passed:
                    continue
                expression = info["rule_exprs"].get(rule, "")
                failed.append({
                    "rule": rule, "expr": expression,
                    "features": {feature: values[feature] for feature in expr_names(expression)
                                 if feature in values}})
            card = {"preset": spec.name, "member": info["member"],
                    "rank": info.get("rank"), "members": len(result.members),
                    "score": info.get("score"), "unknown": sorted(unknown),
                    "failed": failed, "passes_rules": not failed}
            if not info["member"] and not failed:
                # every rule passed and it is still out: the truncation did
                # it -- show where it ranks among the whole passing pool
                pool = self.run(replace(spec, top_n=None, max_per_industry=None),
                                asof=date)
                card["pool_rank"] = (int(pool.members.get_loc(symbol)) + 1
                                     if symbol in pool.members else None)
                card["pool"] = len(pool.members)
                card["capped_out"] = symbol in set(result.capped)   # the industry cap
            cards.append(card)
        rows = []
        pos = panel.position_of(date)
        column = panel.symbols.get_loc(symbol)
        for family, names in INSPECT_STATS:
            for feature in names:
                if feature not in feature_names():
                    continue
                values = panel.cross_section(feature, pos)
                if values.dtype == object:
                    continue
                numeric = np.asarray(values, dtype=float)
                value = float(numeric[column])
                finite = numeric[np.isfinite(numeric)]
                share = float((finite < value).mean()) if np.isfinite(value) \
                    and len(finite) else np.nan
                rows.append({"family": family, "feature": feature,
                             "value": value, "pct_rank": share,
                             "covered": int(len(finite))})
        board = self.store.universe.get(symbol, "board", "")
        industry = _text(self.store.universe.get(symbol, "industry_l1", "")) \
            or UNCATEGORIZED
        return StockCard(symbol=symbol, asof=panel.calendar[pos],
                         info={"symbol": symbol,
                               "name": _text(self.store.universe.get(symbol, "name", "")),
                               "board": BOARD_LABELS.get(board, board),
                               "industry": industry},
                         presets=cards,
                         stats=pd.DataFrame(rows, columns=["family", "feature", "value",
                                                            "pct_rank", "covered"]))

    # --------------------------------------------------------- compare
    def compare(self, specs: Sequence[ScreenSpec], start=None, end=None,
                horizons: Sequence[int] = (5, 10, 20), freq: str = "M",
                apply_top_n: bool = True,
                benchmarks: Sequence[str] = DEFAULT_BENCHMARKS,
                cost_bps: float = 0.0, skip_limit_up: bool = False
                ) -> Tuple[pd.DataFrame, Dict[str, ValidationReport]]:
        """Several screens over ONE window: each is :meth:`validate`d with
        the same dates, horizons and benchmarks (and, with ``cost_bps`` /
        ``skip_limit_up``, the same execution assumptions: the ``*_net``
        rows then sit under their gross twins, and the reference columns
        stay gross).

        Returns ``(table, reports)`` — ``table`` is metrics (rows) by
        preset (columns).  ``excess_ann``, ``information_ratio`` and
        ``spread_Nd`` are each preset's edge over ITS OWN candidate pool
        (the universe its YAML names); the reference columns appended after
        the presets are one benchmark common to them all --
        ``universe`` (:data:`BENCHMARK_UNIVERSE`: every name with a price
        the session before, equal-weighted) and each available index -- so
        presets that screen different boards are still compared like for
        like.
        """
        reports: Dict[str, ValidationReport] = {}
        columns: Dict[str, Dict[str, float]] = {}
        notes: Set[str] = set()
        for spec in specs:
            report = self.validate(spec, start, end, horizons=horizons, freq=freq,
                                   apply_top_n=apply_top_n, benchmarks=benchmarks,
                                   cost_bps=cost_bps, skip_limit_up=skip_limit_up)
            reports[spec.name] = report
            notes.update(report.notes)
            if not report.stats:
                columns[spec.name] = {}
                continue
            # each net row sits right under its gross twin
            after = report.stats.get("portfolio_net")
            row: Dict[str, float] = {
                "members": report.turnover.get("members_mean", np.nan),
                "turnover": report.turnover.get("mean", np.nan),
                "rebalances": report.turnover.get("rebalances", np.nan)}
            pairs: List[Tuple[str, float]] = []
            for metric in ("ann_return", "ann_vol", "sharpe", "max_drawdown", "cum_return"):
                pairs.append((metric, report.stats["portfolio"][metric]))
                if after is not None and metric != "ann_vol":
                    pairs.append((f"{metric}_net", after[metric]))
            for metric in ("excess_ann", "information_ratio"):
                pairs.append((metric, report.spread.get(metric, np.nan)))
                if metric in report.net:
                    pairs.append((f"{metric}_net", report.net[metric]))
            for h in sorted(report.horizons):
                pairs += [(f"spread_{h}d", report.horizons[h]["spread_mean"]),
                          (f"hit_{h}d", report.horizons[h]["hit_rate"])]
            row.update(pairs)
            columns[spec.name] = row
        if any(columns.values()):
            market = self.validate(BENCHMARK_UNIVERSE, start, end, horizons=horizons,
                                   freq=freq, apply_top_n=False, benchmarks=benchmarks)
            reports[BENCHMARK_UNIVERSE.name] = market
            notes.update(market.notes)
            for label, stats in market.stats.items():
                if label == "universe":              # the market's own pool: itself
                    continue
                columns["universe" if label == "portfolio" else label] = {
                    "ann_return": stats["ann_return"],
                                  "ann_vol": stats["ann_vol"],
                                  "sharpe": stats["sharpe"],
                                  "max_drawdown": stats["max_drawdown"],
                                  "cum_return": stats["cum_return"]}
        table = pd.DataFrame(columns)
        table.index.name = "metric"
        table.attrs["notes"] = sorted(notes)
        return table, reports


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
