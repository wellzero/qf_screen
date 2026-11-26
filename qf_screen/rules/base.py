"""Rule primitives: the feature panel, filters, rankers.

Everything a screen can see lives in a :class:`FeaturePanel` — wide
``date x symbol`` tables (as-traded: row ``t`` = day ``t``'s values) built
lazily over a :class:`~qf_screen.data.CNDataStore` and cached by feature
name.  A rule never touches the store directly; it reads feature rows.

Causality convention (matches :mod:`qf_factor.framework.selection.screens`):

* feature tables are AS-TRADED — a trailing window ending at row ``t``
  uses bars through ``t``'s close;
* :meth:`ScreenEngine.run <qf_screen.engine.ScreenEngine.run>` (snapshot)
  evaluates the row of the as-of date: "decided at that close";
* :meth:`ScreenEngine.schedule <qf_screen.engine.ScreenEngine.schedule>`
  (PIT export) evaluates row ``t-1`` and writes membership at ``t`` — a
  schedule row is safe to act ON at its date, zero look-ahead.

One evaluation function, two callers — snapshot and export can never drift
apart in semantics.

NA semantics: a filter's NaN comparisons are False (a name with missing
data does not pass) unless the filter explicitly opts out with
``na_pass=True``.  Ranks / z-scores leave NaN as NaN.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from qf_screen.data import CNDataStore

logger = logging.getLogger(__name__)

#: trading days per year (annualization only; cross-sectional constant)
ANN = 252.0

#: feature builder signature: (panel) -> wide DataFrame (or a 1-row frame
#: for static cross-sectional features, see :func:`register_feature`)
FeatureBuilder = Callable[["FeaturePanel"], pd.DataFrame]

#: registered features: name -> (builder, description, kind) where kind is
#: "table" (date x symbol) or "static" (one value per symbol)
FEATURES: Dict[str, Tuple[FeatureBuilder, str, str]] = {}

#: disk-cache policy per feature name (only entries that have one):
#: ``"store"`` marks a table feature that is a pure function of the store's
#: own base tables and nothing else -- its builder reads ``store.wide(...)``
#: and never a per-symbol source file, the universe census or another
#: feature.  Such a table is cached under ``qf_screen_cache/feature_<name>``
#: behind the STORE fingerprint, so a warm run reads it (~15 ms) instead of
#: recomputing it (the trailing-window math costs 0.1-0.5 s per feature at
#: store width).  Anything with a source of its own (alt / statement /
#: factor data) keeps its own source fingerprint elsewhere and must NOT be
#: listed here.
FEATURE_CACHE: Dict[str, str] = {}


def register_feature(name: str, builder: FeatureBuilder, description: str = "",
                     kind: str = "table", replace: bool = False,
                     cache: Optional[str] = None) -> None:
    """Declare a feature the DSL and the rule helpers can reference.

    ``kind="static"`` builders return a frame indexed by SYMBOL (one row per
    name — board, industry ...); ``kind="table"`` builders return a
    ``date x symbol`` frame aligned to the panel calendar.

    ``cache="store"`` (table features only) declares the builder a pure
    function of the store's base tables and disk-caches its table behind
    the store fingerprint (:data:`FEATURE_CACHE`).
    """
    if not replace and name in FEATURES:
        raise ValueError(f"feature {name!r} is already registered")
    if kind not in ("table", "static"):
        raise ValueError(f"kind must be 'table' or 'static', got {kind!r}")
    if cache is not None and (cache != "store" or kind != "table"):
        raise ValueError(f"cache must be None or 'store' (table features only), "
                         f"got {cache!r}")
    FEATURES[name] = (builder, description, kind)
    if cache is None:
        FEATURE_CACHE.pop(name, None)
    else:
        FEATURE_CACHE[name] = cache


def feature_names() -> List[str]:
    """Sorted names of every registered feature."""
    return sorted(FEATURES)


#: dynamic feature prefix: ``factor_<family>__<column>`` resolves a
#: precomputed qf_factor / qf_mine per-symbol factor CSV on first use (see
#: :mod:`qf_screen.data.factor_bridge`)
_FACTOR_PREFIX = "factor_"


def is_dynamic_feature(name: str) -> bool:
    """Whether ``name`` is a lazily-resolved factor-bridge feature
    (``factor_<family>__<column>``; the family may itself contain
    underscores, the column may not)."""
    return name.startswith(_FACTOR_PREFIX) and "__" in name[len(_FACTOR_PREFIX):]


def dynamic_feature_parts(name: str) -> Tuple[str, str]:
    """``factor_<family>__<column>`` -> ``("<family>", "<column>")``."""
    family, column = name[len(_FACTOR_PREFIX):].rsplit("__", 1)
    return family, column


# ------------------------------------------------------------------ panel
class FeaturePanel:
    """Lazy feature tables + cross-sections over one store.

    Parameters
    ----------
    store : the data store the features are computed from.
    """

    def __init__(self, store: CNDataStore):
        self.store = store
        self.calendar = store.calendar
        self.symbols = store.wide("close").columns
        self._cache: Dict[str, pd.DataFrame] = {}
        #: human-readable data caveats feature builders discovered while
        #: building their tables (e.g. stale alt-data sources); the engine
        #: copies these into :attr:`ScreenResult.warnings`.
        self.warnings: List[str] = []

    # ------------------------------------------------------------ tables
    def table(self, name: str) -> pd.DataFrame:
        """The feature's wide table (``date x symbol``), computed on first
        use and cached.  Static features are returned as a frame indexed by
        symbol."""
        if name in self._cache:
            return self._cache[name]
        if name not in FEATURES and is_dynamic_feature(name):
            family, column = dynamic_feature_parts(name)
            from ..data.factor_bridge import factor_feature_table
            table = factor_feature_table(self, family, column)
            self._cache[name] = table
            return table
        try:
            builder, _doc, kind = FEATURES[name]
        except KeyError:
            raise KeyError(
                f"unknown feature {name!r}; known: {', '.join(feature_names())} "
                f"(or factor_<family>__<column> for a qf_factor CSV bridge)") \
                from None
        if self._store_cacheable(name):
            table = self._store_cached(name, builder)
        else:
            table = builder(self)
        if kind == "table" and not table.empty and not table.index.equals(self.calendar):
            table = table.reindex(self.calendar)     # a cached table already is
        self._cache[name] = table
        return table

    def _store_cacheable(self, name: str) -> bool:
        """Whether ``name``'s table may be served from the store-keyed disk
        cache: the builder must have declared ``cache="store"``, the store's
        cache must be on (it supplies the fingerprint) and no live row may
        be attached (a provisional row is not worth a cache entry, and must
        never overwrite the end-of-day one)."""
        store = self.store
        return (FEATURE_CACHE.get(name) == "store" and store.use_cache
                and store.live_date is None and store.fingerprint is not None)

    def _store_cached(self, name: str, builder: FeatureBuilder) -> pd.DataFrame:
        """Read-through disk cache of a pure-of-store feature table, keyed
        on the store fingerprint, the calendar bounds, the symbol list and
        the feature name (see :data:`FEATURE_CACHE`)."""
        from qf_screen.data.table_cache import cached_wide_table, derived_fingerprint

        store = self.store
        fingerprint = derived_fingerprint(
            [], self.calendar, self.symbols, extra=f"store:{store.fingerprint}")
        return cached_wide_table(store.table_cache_dir, f"feature_{name}", fingerprint,
                                 lambda: builder(self))

    def cross_section(self, name: str, pos: int) -> np.ndarray:
        """One feature's values at calendar row ``pos``.

        Numeric table features come back as ``float64`` with NaN for
        missing; categorical ones (object dtype, e.g. the industry) as the
        raw objects.  Static features (indexed by symbol, one column) ignore
        ``pos``.
        """
        table = self.table(name)
        if table.empty:
            return np.full(len(self.symbols), np.nan)
        if isinstance(table.index, pd.DatetimeIndex):
            row = table.iloc[pos]
            if not row.index.equals(self.symbols):
                row = row.reindex(self.symbols)   # stale cache / foreign table
            if row.dtype == bool:
                return row.to_numpy(dtype=bool)
            return row.to_numpy() if row.dtype == object \
                else row.to_numpy(dtype=float, na_value=np.nan)
        if table.shape[1] != 1:
            raise ValueError(f"static feature {name!r} must have exactly one column")
        column = table.iloc[:, 0].reindex(self.symbols)
        if column.dtype == bool:
            return column.to_numpy(dtype=bool)
        return column.to_numpy() if column.dtype == object \
            else column.to_numpy(dtype=float, na_value=np.nan)

    def env(self, pos: int, names: Optional[Sequence[str]] = None) -> Dict[str, np.ndarray]:
        """Evaluation environment for the DSL: every (or the named)
        feature's cross-section at row ``pos``.  The categorical
        ``_industry`` key rides along when the industry feature exists."""
        wanted = list(names) if names is not None else feature_names()
        out = {name: self.cross_section(name, pos)
               for name in wanted
               if name in FEATURES or is_dynamic_feature(name)}
        if "industry_l1" in FEATURES:
            industry = self.table("industry_l1")
            if not industry.empty:
                out["_industry"] = industry.iloc[:, 0] \
                    .reindex(self.symbols).to_numpy()
        return out

    def position_of(self, asof) -> int:
        """Calendar row of ``asof`` (Timestamp-like); the last row when
        ``asof`` is None; the row BEFORE the first strictly-greater date
        when ``asof`` falls between sessions."""
        if asof is None:
            return len(self.calendar) - 1
        ts = pd.Timestamp(asof).normalize()
        pos = int(self.calendar.searchsorted(ts, side="right")) - 1
        if pos < 0:
            raise ValueError(f"as-of date {ts.date()} precedes the panel calendar "
                             f"({self.calendar[0].date()}..{self.calendar[-1].date()})")
        return pos

    def stale_features(self, tol_sessions: int = 20) -> List[Tuple[str, pd.Timestamp, float]]:
        """Built table features whose data ends well before the calendar.

        Returns ``(name, last date any symbol has a value, share of symbols
        whose own last value is older than ``tol_sessions``)`` for every
        BUILT table whose cross-section is materially stale — the honest
        signal that an alt-data download (northbound, margin ...) lags the
        price files, so a screen on it is deciding on old data.
        """
        end = len(self.calendar) - 1
        out: List[Tuple[str, pd.Timestamp, float]] = []
        for name, table in self._cache.items():
            if name.startswith("_") or not isinstance(table.index, pd.DatetimeIndex) \
                    or table.empty or name in FEATURES and FEATURES[name][2] != "table":
                continue
            last_row = table.iloc[end].to_numpy(dtype=float) if len(table) > end \
                else table.iloc[-1].to_numpy(dtype=float)
            covered = np.isfinite(last_row)
            if not covered.any():
                continue
            # last valid date per symbol, vectorized: argmax over the
            # reversed finiteness mask is the last finite row
            values = table.to_numpy(dtype=float)
            finite = np.isfinite(values)
            any_finite = finite.any(axis=0)
            last_pos = values.shape[0] - 1 - np.argmax(finite[::-1], axis=0)
            stale = (end - last_pos) > tol_sessions
            stale &= any_finite
            share = float(stale.sum()) / float(any_finite.sum())
            newest = table.index[last_pos[any_finite]].max()
            if share > 0.5 and (end - last_pos[any_finite]).max() > tol_sessions:
                out.append((name, pd.Timestamp(newest), share))
        return out


# ------------------------------------------------------------------ rules
class Filter:
    """One named boolean rule over the panel.

    ``passes(panel, pos)`` returns a bool ndarray over ``panel.symbols``.
    The default :attr:`na_pass` is False: a NaN comparison is False, so a
    name without the data does not pass.  Subclasses may set ``na_pass`` to
    relax that for filters where missing data is not disqualifying.
    """

    na_pass: bool = False

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description

    def passes(self, panel: FeaturePanel, pos: int) -> np.ndarray:
        raise NotImplementedError

    def evaluate(self, panel: FeaturePanel, pos: int
                 ) -> Tuple[np.ndarray, "Optional[np.ndarray]"]:
        """``(passed, unknown)``: the verdict and, for a rule that can be
        left without data, the mask of names it could not judge (``None``
        for rules that always judge, the default here)."""
        return self.passes(panel, pos), None

    def __repr__(self) -> str:
        return f"<Filter {self.name}>"


class Ranker:
    """One named scoring rule: higher score = better (invert inside the
    expression for lower-is-better raw measures)."""

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description

    def score(self, panel: FeaturePanel, pos: int) -> np.ndarray:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<Ranker {self.name}>"


class CombinedRanker(Ranker):
    """Blend several rankers: each becomes a cross-sectional percentile
    rank (NaN excluded) and the weighted mean is taken over the scores a
    symbol actually has — a missing score neither zeroes nor invents a
    value (:func:`qf_factor.framework.selection.screener.composite_score`
    semantics, on one date)."""

    def __init__(self, rankers: Sequence[Ranker], weights: Optional[Sequence[float]] = None,
                 name: str = "composite"):
        super().__init__(name, " + ".join(r.name for r in rankers))
        self.rankers = list(rankers)
        self.weights = [1.0] * len(self.rankers) if weights is None else list(weights)
        if len(self.weights) != len(self.rankers):
            raise ValueError("weights must match rankers")
        if any(w <= 0 for w in self.weights):
            raise ValueError("weights must be > 0")

    def score(self, panel: FeaturePanel, pos: int) -> np.ndarray:
        num = np.zeros(len(panel.symbols))
        den = np.zeros(len(panel.symbols))
        for ranker, weight in zip(self.rankers, self.weights):
            values = ranker.score(panel, pos)
            ok = np.isfinite(values)
            ranks = pd.Series(values).rank(pct=True).to_numpy()  # NaN stays NaN
            num[ok] += ranks[ok] * weight
            den[ok] += weight
        out = np.full(len(panel.symbols), np.nan)
        np.divide(num, den, out=out, where=den > 0)
        return out


# ------------------------------------------------------- feature builders
def _safe_div(num, den) -> pd.DataFrame:
    """``num / den`` over wide frames as PLAIN NUMPY.

    pandas routes wide ``DataFrame / DataFrame`` through numexpr past the
    ~10k-cell threshold, which this environment silently corrupts on
    float32 + NaN input (see the package docstring) — so the feature
    builders divide through numpy explicitly.  The result keeps the LEFT
    frame's index/columns; a scalar or Series denominator is accepted
    positionally.
    """
    a = num.to_numpy(dtype=float)
    b = den.to_numpy(dtype=float) if isinstance(den, pd.DataFrame) \
        else np.asarray(den, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.divide(a, b)
    return pd.DataFrame(out, index=num.index, columns=num.columns)


def _safe_sub(num, den) -> pd.DataFrame:
    """``num - den`` over wide frames, plain numpy (see :func:`_safe_div`)."""
    a = num.to_numpy(dtype=float)
    b = den.to_numpy(dtype=float) if isinstance(den, pd.DataFrame) \
        else np.asarray(den, dtype=float)
    return pd.DataFrame(np.subtract(a, b), index=num.index, columns=num.columns)


def _coalesce(primary: pd.DataFrame, fallback: pd.DataFrame) -> pd.DataFrame:
    """``primary`` where it is not NaN, else ``fallback`` — what pandas'
    ``combine_first`` means for two equally-shaped float frames — as PLAIN
    NUMPY.

    ``combine_first`` is the single slowest step of a warm-cache screen:
    it routes through the element-wise ``combine`` path and costs ~2 s per
    call on a store-scale frame (two calls per ``value_dividend`` run),
    which quietly handed back most of the time the store cache saves.  The
    result keeps the PRIMARY frame's axes; a fallback on other axes is
    reindexed onto them first (a fallback with rows/columns the primary
    lacks contributes nothing, unlike ``combine_first``, which would add
    them — every caller here passes equally-aligned tables).
    """
    if not (primary.index.equals(fallback.index)
            and primary.columns.equals(fallback.columns)):
        fallback = fallback.reindex(index=primary.index, columns=primary.columns)
    out = primary.to_numpy(dtype=float, copy=True)
    np.copyto(out, fallback.to_numpy(dtype=float), where=np.isnan(out))
    return pd.DataFrame(out, index=primary.index, columns=primary.columns)


def _inverse_positive(table: pd.DataFrame) -> pd.DataFrame:
    """``1 / table`` where the value is positive, NaN elsewhere (a negative
    P/E is a loss — its "yield" must not rank as a bargain)."""
    values = table.to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.divide(1.0, np.where(values > 0, values, np.nan))
    return pd.DataFrame(out, index=table.index, columns=table.columns)


def _pct_change(close: pd.DataFrame) -> pd.DataFrame:
    """Daily returns of an as-traded close table; suspension gaps and the
    leading row stay NaN (pandas' deprecated implicit ffill must not
    fabricate flat returns through a halt)."""
    return close.pct_change(fill_method=None)


def register_price_features() -> None:
    """The price / volume / valuation features every screen can reach.

    All trailing windows END at the row they are labeled on (as-traded);
    the engine applies the one-day strictly-before shift when it exports a
    schedule.  Momentum uses the skip-most-recent-month construction
    (``mom_<L>s21`` = return over ``L`` days skipping the last 21) and the
    plain ``ret_<n>d`` family skips nothing.
    """

    def momentum(lookback: int, skip: int) -> FeatureBuilder:
        def build(panel: FeaturePanel) -> pd.DataFrame:
            close = panel.store.wide("close").astype(float)
            return _safe_sub(_safe_div(close.shift(skip), close.shift(lookback)), 1.0)
        return build

    def volatility(window: int) -> FeatureBuilder:
        def build(panel: FeaturePanel) -> pd.DataFrame:
            ret = _pct_change(panel.store.wide("close").astype(float))
            return ret.rolling(window, min_periods=max(2, window // 2)).std() * np.sqrt(ANN)
        return build

    def ma_ratio(window: int) -> FeatureBuilder:
        def build(panel: FeaturePanel) -> pd.DataFrame:
            close = panel.store.wide("close").astype(float)
            ma = close.rolling(window, min_periods=max(2, window // 2)).mean()
            return _safe_div(close, ma)
        return build

    def rolling_mean(field: str, window: int) -> FeatureBuilder:
        def build(panel: FeaturePanel) -> pd.DataFrame:
            values = panel.store.wide(field).astype(float)
            return values.rolling(window, min_periods=max(2, window // 2)).mean()
        return build

    def max_lottery(window: int = 21, k: int = 5) -> FeatureBuilder:
        """Mean of the ``k`` largest daily returns in the trailing
        ``window`` (Bali–Cakici–Whitelaw MAX) — vectorized over dates; the
        qf_factor primitive this replaces is a Python row loop (~15 s at
        full-universe width).  Equivalence is asserted by a unit test."""
        def build(panel: FeaturePanel) -> pd.DataFrame:
            ret = _pct_change(panel.store.wide("close").astype(float))
            values = ret.to_numpy(dtype=float)
            n_rows = values.shape[0]
            out = np.full(values.shape, np.nan)
            for t in range(n_rows):                # qf_factor starts at row 0
                lo = max(0, t - window + 1)         # truncated leading windows
                block = values[lo: t + 1]
                kk = min(k, block.shape[0])         # partition bound: rows < k
                finite = np.isfinite(block)
                counts = finite.sum(axis=0)
                # mean of the k largest finite returns, matching qf_factor's
                # primitive exactly: a window holding FEWER than k finite
                # values averages the ones it has (only all-NaN -> NaN)
                safe = np.where(finite, block, -np.inf)
                top = np.partition(safe, -kk, axis=0)[-kk:]
                mean_top = top.mean(axis=0)
                with np.errstate(invalid="ignore"):
                    mean_all = np.where(counts > 0,
                                        np.nansum(block, axis=0)
                                        / np.maximum(counts, 1), np.nan)
                out[t] = np.where(counts >= k, mean_top,
                                  np.where(counts > 0, mean_all, np.nan))
            return pd.DataFrame(out, index=ret.index, columns=ret.columns)
        return build

    def high_ratio(window: int = 252) -> FeatureBuilder:
        def build(panel: FeaturePanel) -> pd.DataFrame:
            close = panel.store.wide("close").astype(float)
            hi = close.rolling(window, min_periods=max(2, window // 2)).max()
            return _safe_div(close, hi)
        return build

    # every window feature below reads nothing but ``store.wide(...)`` and
    # costs real arithmetic at store width (0.1 s per rolling mean, 0.5 s
    # for max21), so each is disk-cached behind the store fingerprint
    cached = {"cache": "store"}
    for lookback in (21, 63, 126, 252):
        register_feature(
            f"ret_{lookback}d", momentum(lookback, 0),
            f"{lookback}-day total return (adjusted close, no skip)", **cached)
        if lookback >= 63:
            register_feature(
                f"mom_{lookback}s21", momentum(lookback, 21),
                f"momentum: {lookback}-day return skipping the most recent 21 days",
                **cached)
    for window in (20, 60, 120):
        register_feature(f"vol_{window}d", volatility(window),
                         f"annualized trailing {window}-day return volatility",
                         **cached)
    for window in (20, 60, 200):
        register_feature(f"ma_ratio_{window}", ma_ratio(window),
                         f"adjusted close / its trailing {window}-day mean (>1 = above)",
                         **cached)
    for window in (20, 60):
        register_feature(f"amount_ma{window}", rolling_mean("amount", window),
                         f"trailing {window}-day mean traded value (CNY)", **cached)
        register_feature(f"turnover_ma{window}", rolling_mean("turnoverrate", window),
                         f"trailing {window}-day mean turnover rate (% of float)",
                         **cached)
    register_feature("high252_ratio", high_ratio(252),
                     "adjusted close / trailing 252-day high (George–Hwang)",
                     **cached)
    register_feature("max21", max_lottery(21, 5),
                     "mean of the 5 largest daily returns in the trailing 21 days (MAX)",
                     **cached)
    for field, doc in (
            ("pe_ttm", "trailing P/E"), ("pe_static", "static P/E"), ("pb", "price/book"),
            ("ps_ttm", "trailing P/S"), ("pcf_ttm", "trailing P/CF"), ("peg", "PEG"),
            ("market_cap", "total market cap (CNY)"),
            ("float_market_cap", "float market cap (CNY)"),
            ("total_shares", "total shares"), ("float_shares", "float shares"),
            ("close_raw", "unadjusted close"), ("turnoverrate", "daily turnover rate (%)"),
            ("amount", "daily traded value (CNY)")):
        register_feature(field, lambda p, f=field: p.store.wide(f).astype(float), doc)
    register_feature("close", lambda p: p.store.wide("close").astype(float),
                     "chain-adjusted close (rebuilt by the store)")
    register_feature(
        "earnings_yield",
        lambda p: _inverse_positive(p.store.wide("pe_ttm").astype(float)),
        "1 / pe_ttm (earnings yield; zero/negative/NaN earnings stay NaN)")


register_price_features()

