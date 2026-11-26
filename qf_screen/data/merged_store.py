"""``CNDataStore`` -- unified snapshot + causal wide-panel data facade.

Merges the two local providers per symbol and date into one series per
stock, then into wide ``date x symbol`` tables that everything downstream
(rules, DSL, engine) consumes.

Merge priority (per the design): **lxg first, ak fills the gaps** -- a
symbol without lxg (~3,050 names) or a date newer than lxg's coverage
seamlessly falls back to ``ak/basic.csv``.  Concretely, per column:

=====================  ==================================  ==========================
column                 primary                             fallback
=====================  ==================================  ==========================
``close_raw``          ak ``close``                        lxg ``close_raw``
``volume``, ``amount`` lxg ``daily``                       proxy (below)
``turnoverrate``       lxg                                 ak
valuation, mcap        lxg ``valuation`` / layout-B daily  ak ``basic``
share counts           ak ``basic``                        lxg layout-B daily
=====================  ==================================  ==========================

Three things in this file are NOT a straight copy of a column:

**Adjusted close.**  Neither provider ships a usable one.  ak's ``close``
is unadjusted, and lxg's 前复权 ``close`` is unreliable (1,740 of 2,446
symbols carry ``close == close_raw`` everywhere; 1,312 of 1,802
bonus-share events unadjusted -- measured 2026-11).  The store therefore
chains the adjusted series itself from the raw price
(:func:`build_adjusted_close`):

* a documented lxg dividend event (``dividend.csv``: cash per share + bonus
  shares per share, ``status == implemented``) on its ex-date gives the
  total-return move ``(P * (1 + bonus) + cash) / P_prev - 1``;
* otherwise a share-count split: ``total_shares`` AND ``float_shares`` both
  scale by ``s`` while the price drops accordingly gives ``(1 + r) * s - 1``
  (the float test is what separates a bonus issue from a placement, whose
  new shares are locked and never scale the float);
* otherwise the raw return.  Cash dividends of names without an lxg
  dividend file are NOT removed.

Against lxg's own adjusted series on the 276 symbols where that series is
self-consistent (1.1 M symbol-days since 2000) this leaves 66 days with a
>15 % discrepancy versus 701 for the raw price.

**Traded value.**  ``ak/basic.csv`` has no ``amount``; where lxg is missing
it is reconstructed as ``turnoverrate / 100 * float_market_capital`` (within
0.2 % of lxg's ``amount`` on common rows).

**Calendar.**  The trading calendar is the contiguous run of complete
sessions ending at the last one, taken over the symbols that trade
regularly and traded recently.  Everything else is aligned onto it: names
that stopped trading long ago, the blank rows of a long-suspended stock
(whose 600-row tail reaches back years), and a trailing partial "today"
session (valuation present, no close yet) -- so the default as-of date is
the last COMPLETE session.

Every wide table is **as-traded**: row ``t`` holds day ``t``'s values, i.e.
what was known at the close of ``t``.  Causality is applied once, by the
engine, as a single ``shift(1)`` -- the strictly-before convention of
:mod:`qf_factor.framework.selection.screens`.  Tables are stored as
``float32`` (a 630 x 5,500 table is 14 MB instead of 28 MB); screening
thresholds never need more.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from quant_free.common.const_filename import get_data_dir
from quant_free.common.symbol_format import to_canonical

from .table_cache import read_wide, write_wide
from ._io import DAY
from .ak_provider import AkProvider, LiveAkClient, LiveDataError
from .lxg_provider import LXG_FILES, LxgProvider
from .universe import ALL_BOARDS, Universe, build_universe

logger = logging.getLogger(__name__)

#: base wide tables (``store.wide(name)``); all as-traded, see the module doc
BASE_FIELDS: Tuple[str, ...] = (
    "close_raw", "close", "volume", "amount", "turnoverrate", "pe_ttm", "pe_static",
    "pb", "ps_ttm", "pcf_ttm", "peg", "market_cap", "float_market_cap",
    "total_shares", "float_shares", "dyr",
)

#: split-detection gates (see :func:`build_adjusted_close`)
MIN_SHARE_CHANGE = 0.05      # share count must move at least this much
BIG_SHARE_CHANGE = 0.15      # ... and at least this much without float data
FLOAT_TOLERANCE = 0.02       # float must scale with total shares to 2 %
SMALL_LIMIT = 0.10           # |reconstructed return| bound, small share change
BIG_LIMIT = 0.21             # ... big share change (allows a ±20 % board day)
EVENT_LIMIT = 0.35           # |dividend-event return| bound (data guard)

#: a symbol counts as "recent" (a calendar source) if its last bar is within
#: this many days of the newest bar in the store ...
_RECENT_DAYS = 30
#: ... and as "regular" if at least this share of the rows it has in the
#: window carry a close (a stock suspended for a year has blank rows
#: throughout and must not define what a session is)
_REGULAR_TRADED_SHARE = 0.5
#: a session is complete when at least this share of the trailing median
#: count of traded names has a close ...
_COMPLETE_FRACTION = 0.2
#: ... and its neighbours on the raw date union are no further away than
#: this (isolated old rows of a sparse file are not sessions)
_DENSE_GAP_DAYS = 20
#: tail mode only: a session counts as complete when at least this share of
#: the calendar-source symbols traded on it (see CNDataStore._set_calendar)
_TAIL_SESSION_SHARE = 0.5
#: below this share of the previous session's traded names, the last
#: session is reported as possibly incomplete (a data update still running)
_INCOMPLETE_SHARE = 0.85
#: a column counts as served by one source when the other contributes less
#: than this share of its rows (lxg's tail normally ends a few days before
#: ak's, which is not "mixed" data)
_MIXED_SHARE = 0.10

_VALUATION_COLS: Tuple[Tuple[str, str], ...] = (
    ("pe_ttm", "pe_ttm"), ("pe_static", "pe_static"), ("pb", "pb"),
    ("ps_ttm", "ps_ttm"), ("pcf_ttm", "pcf_ttm"), ("peg", "peg"),
    ("market_cap", "market_capital"), ("float_market_cap", "float_market_capital"),
    ("total_shares", "total_shares"), ("float_shares", "float_shares"),
)


# --------------------------------------------------------------- adjusted
def prepare_dividend_events(events: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Implemented dividend events aggregated per ex-date.

    Input is :meth:`LxgProvider.dividend_events` output.  Only
    ``status == 'implemented'`` rows with an ex-date and CNY (or unstated)
    currency count -- a ``board_director_plan`` is not an event.  Returns
    columns ``ex_date, bonus, cash`` (bonus shares and cash per pre-event
    share) sorted by ex-date.
    """
    empty = pd.DataFrame({"ex_date": pd.Series(dtype="datetime64[ns]"),
                          "bonus": pd.Series(dtype=float), "cash": pd.Series(dtype=float)})
    if events is None or events.empty or "ex_date" not in events.columns:
        return empty
    ex = events["ex_date"].to_numpy().astype(DAY)
    keep = ~pd.isna(ex)
    if "status" in events.columns:            # a few dozen rows at most
        keep &= np.array([str(s).strip().lower() == "implemented"
                          for s in events["status"].to_numpy()])
    if "currency" in events.columns:
        keep &= np.array([str(c).strip().upper() in ("CNY", "")
                          for c in events["currency"].to_numpy()])

    def _values(*names) -> np.ndarray:
        for name in names:
            if name in events.columns:
                return np.nan_to_num(pd.to_numeric(events[name], errors="coerce")
                                     .to_numpy(dtype=float), nan=0.0)
        return np.zeros(len(events))

    bonus = _values("bonus_profit") + _values("bonus_reserve")
    cash = _values("cash_per_share")
    keep &= (bonus != 0) | (cash != 0)
    if not keep.any():
        return empty
    out = pd.DataFrame({"ex_date": ex[keep], "bonus": bonus[keep], "cash": cash[keep]})
    return out.groupby("ex_date", as_index=False, sort=True)[["bonus", "cash"]].sum()


def _ffill(values: np.ndarray) -> np.ndarray:
    """Forward-fill a float array (leading NaN stay NaN)."""
    ok = np.isfinite(values)
    if not ok.any():
        return values.copy()
    last = np.maximum.accumulate(np.where(ok, np.arange(values.size), -1))
    return values[np.maximum(last, 0)]


def _as_days(index) -> np.ndarray:
    """Any datetime index (``datetime64`` array of any unit, ``DatetimeIndex``,
    aware or not) as a ``datetime64[D]`` array."""
    array = np.asarray(index)
    if array.dtype == np.dtype(DAY):
        return array
    if array.dtype.kind == "M":
        return array.astype(DAY)
    dates = pd.DatetimeIndex(index)
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    return dates.normalize().to_numpy().astype(DAY)


def build_adjusted_close(close_raw, index, total_shares=None, float_shares=None,
                         dividend_events=None) -> Tuple[np.ndarray, Dict[str, int]]:
    """Chain-adjusted close from a RAW close series.  Returns ``(adj, info)``.

    ``adj`` is anchored at the first valid raw close and then follows the
    adjusted daily return, so it is causal (a later event never rewrites an
    earlier value) and suspended days stay NaN.  ``info`` counts the
    adjustments applied: ``split_adj`` (share-count splits), ``dividend_adj``
    (documented dividend events) and ``dividend_rejected`` (events whose
    implied move exceeds ``EVENT_LIMIT`` -- bad data, not adjusted).

    Parameters
    ----------
    close_raw : raw traded close (NaN where not traded).
    index : the sorted dates of ``close_raw`` (any datetime dtype).
    total_shares, float_shares : ak share counts (NaN where unknown).
    dividend_events : :func:`prepare_dividend_events` output.
    """
    close = np.asarray(close_raw, dtype=float)
    n = close.size
    adj = np.full(n, np.nan)
    info = {"split_adj": 0, "dividend_adj": 0, "dividend_rejected": 0}
    valid = np.isfinite(close) & (close > 0)
    if not valid.any():
        return adj, info
    first = int(np.argmax(valid))
    adj[first] = close[first]
    if valid.sum() == 1:
        return adj, info

    days = _as_days(index)
    prev = np.empty(n)
    prev[0] = np.nan
    prev[1:] = _ffill(close)[:-1]              # last valid close strictly before t
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = close / prev - 1.0
    adj_ret = ret.copy()
    gate = np.zeros(n, dtype=bool)

    # ---- 1) share-count split (bonus issue / 转增 / consolidation)
    if total_shares is not None:
        shares = _ffill(np.asarray(total_shares, dtype=float))
        prev_shares = np.r_[np.nan, shares[:-1]]
        known = np.isfinite(prev_shares) & (prev_shares > 0) & np.isfinite(shares)
        float_ok = np.zeros(n, dtype=bool)
        float_known = np.zeros(n, dtype=bool)
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = np.where(known, shares / prev_shares, np.nan)
            if float_shares is not None:
                flt = _ffill(np.asarray(float_shares, dtype=float))
                prev_flt = np.r_[np.nan, flt[:-1]]
                float_known = np.isfinite(prev_flt) & (prev_flt > 0) & np.isfinite(flt)
                fratio = np.where(float_known, flt / prev_flt, np.nan)
                float_ok = float_known & (np.abs(fratio / ratio - 1.0) <= FLOAT_TOLERANCE)
            dev = ratio - 1.0
            cand = (1.0 + ret) * ratio - 1.0
        big = np.abs(dev) >= BIG_SHARE_CHANGE
        allowed = np.where(big, BIG_LIMIT, SMALL_LIMIT)
        gate = (known
                & (np.abs(dev) >= MIN_SHARE_CHANGE)
                & (ret * dev < 0)                            # opposite directions
                & (float_ok | (big & ~float_known))          # no float data: big only
                & (np.abs(cand) <= allowed)
                & (big | (np.abs(ret) >= 0.03)))
        adj_ret[gate] = cand[gate]

    # ---- 2) documented dividend event on its ex-date (overrides 1)
    has_event = np.zeros(n, dtype=bool)
    if dividend_events is not None and len(dividend_events):
        bonus_at = np.zeros(n)
        cash_at = np.zeros(n)
        for ex_date, bonus, cash in dividend_events.itertuples(index=False, name=None):
            pos = int(np.searchsorted(days, np.datetime64(ex_date).astype(DAY)))  # >= ex
            while pos < n and not valid[pos]:                   # suspended on the ex-date
                pos += 1
            if pos < n:
                has_event[pos] = True
                bonus_at[pos] += float(bonus)
                cash_at[pos] += float(cash)
        if has_event.any():
            with np.errstate(invalid="ignore", divide="ignore"):
                event_ret = (close * (1.0 + bonus_at) + cash_at) / prev - 1.0
            ok = has_event & np.isfinite(event_ret) & (np.abs(event_ret) <= EVENT_LIMIT)
            adj_ret[ok] = event_ret[ok]
            info["dividend_adj"] = int(ok.sum())
            info["dividend_rejected"] = int((has_event & ~ok).sum())
    info["split_adj"] = int((gate & ~has_event).sum())

    growth = 1.0 + np.nan_to_num(adj_ret, nan=0.0, posinf=0.0, neginf=0.0)
    growth[first] = 1.0
    adj[first:] = close[first] * np.cumprod(growth[first:])
    return adj, info


# ------------------------------------------------------------------- merge
def _fill(out: np.ndarray, positions: np.ndarray, values: np.ndarray) -> None:
    ok = np.isfinite(values)
    out[positions[ok]] = values[ok]


def _majority_source(counts: Dict[str, int]) -> str:
    """Which source a column came from: the dominant name, ``"mixed"`` when
    the secondary source fills a material share, ``"none"`` without data."""
    ranked = sorted((item for item in counts.items() if item[1]), key=lambda kv: -kv[1])
    if not ranked:
        return "none"
    total = sum(count for _, count in ranked)
    if len(ranked) > 1 and ranked[1][1] / total >= _MIXED_SHARE:
        return "mixed"
    return ranked[0][0]


def merge_symbol(lxg: LxgProvider, ak: AkProvider, dirs: Sequence[str], *,
                 start=None, end=None, tail_rows: Optional[int] = None):
    """One symbol's merged base series -> ``(days, {field: ndarray}, info)``
    or ``None`` when there is neither ak nor lxg data.

    A module-level function (not a method) so it can run unchanged in a
    worker process.  Everything here is positional numpy: the per-symbol
    pandas calls this replaces cost more than the CSV parsing itself.
    """
    basic = ak.basic(dirs, start=start, end=end, tail_rows=tail_rows)
    daily = lxg.daily(dirs, start=start, end=end, tail_rows=tail_rows)
    if basic is None and daily is None:
        return None

    # layout-B daily carries its valuation; layout A keeps it in valuation.csv
    valuation = None
    if daily is not None:
        valuation = lxg.valuation_from_daily(daily) if "pe_ttm" in daily.cols \
            else lxg.valuation(dirs, start=start, end=end, tail_rows=tail_rows)
    tables = [table for table in (basic, daily, valuation) if table is not None]
    days = tables[0].days
    for table in tables[1:]:
        days = np.union1d(days, table.days)
    positions = [np.searchsorted(days, table.days) for table in tables]
    n = days.size

    def pick(*sources: Tuple[int, str]) -> np.ndarray:
        """Column ``sources`` (``(table, column)`` pairs, FIRST = primary)
        positioned on ``days``; the later ones only fill the gaps."""
        out = np.full(n, np.nan)
        for table, column in reversed(sources):
            if column in tables[table].cols:
                _fill(out, positions[table], tables[table].cols[column])
        return out

    # table slots: 0 = basic when present, else daily; resolved by identity
    slot = {id(table): i for i, table in enumerate(tables)}
    B, D, V = (slot.get(id(basic), -1), slot.get(id(daily), -1), slot.get(id(valuation), -1))

    def src(slot_id: int, column: str):
        return (slot_id, column) if slot_id >= 0 else None

    def both(first, *rest):
        return tuple(s for s in (first, *rest) if s is not None)

    close_raw = pick(*both(src(B, "close"), src(D, "close_raw")))
    traded = np.isfinite(close_raw) & (close_raw > 0)
    if not traded.any():
        return None

    # --- traded value: lxg amount, with the ak proxy filling only the gaps
    from_lx = pick(*both(src(D, "amount")))
    proxy_aligned = np.full(n, np.nan)
    if B >= 0 and {"turnoverrate", "float_market_capital"} <= set(tables[B].cols):
        table = tables[B]
        turnover, float_mc = table.cols["turnoverrate"], table.cols["float_market_capital"]
        ok = np.isfinite(turnover) & np.isfinite(float_mc) & (turnover > 0) & (float_mc > 0)
        _fill(proxy_aligned, positions[B], np.where(ok, turnover / 100.0 * float_mc, np.nan))
    amount = np.where(np.isfinite(from_lx), from_lx, proxy_aligned)
    amount_source = _majority_source({
        "lxg": int(np.isfinite(from_lx).sum()),
        "ak_proxy": int((np.isfinite(amount) & ~np.isfinite(from_lx)).sum())})

    fields: Dict[str, np.ndarray] = {
        "close_raw": close_raw,
        "amount": amount,
        "volume": pick(*both(src(D, "volume"))),
        "turnoverrate": pick(*both(src(D, "turnoverrate"), src(B, "turnoverrate"))),
        # lxg layout-B's own trailing dividend yield (a fraction) — the
        # fallback div_yield_ttm uses for names whose dividend events the
        # event builder cannot see
        "dyr": pick(*both(src(D, "dyr"))),
    }
    for name, column in _VALUATION_COLS:
        fields[name] = pick(*both(src(V, column), src(B, column)))
    # NaN -- not zeros -- when there is no lxg table: zeros are finite and
    # would count every row as "served by lxg" in the provenance
    from_lx_val = pick(*both(src(V, "market_capital"))) if V >= 0 else np.full(n, np.nan)
    val_source = _majority_source({
        "lxg": int(np.isfinite(from_lx_val).sum()),
        "ak": int((np.isfinite(fields["market_cap"]) & ~np.isfinite(from_lx_val)).sum())})

    # --- adjusted close
    events = None
    raw_events = lxg.dividend_events(dirs) if D >= 0 else None
    if raw_events is not None:
        events = prepare_dividend_events(raw_events)
        if len(events):
            events = events[events["ex_date"] >= days[0]]
    adj, info = build_adjusted_close(
        close_raw, days, fields["total_shares"], fields["float_shares"], events)
    fields["close"] = adj

    info.update({
        "lxg_daily": daily is not None,
        "ak_basic": basic is not None,
        "dividend_file": raw_events is not None,
        "amount_source": amount_source,
        "val_source": val_source,
        "rows": int(n),
        "traded_rows": int(traded.sum()),
        "first_date": pd.Timestamp(days[traded][0]),
        "last_date": pd.Timestamp(days[traded][-1]),
    })
    return days, fields, info


# ---------------------------------------------------------------- workers
class _Worker:
    """Per-process provider pair for :func:`merge_symbol` in a worker."""

    def __init__(self, data_dir, market, start, end, tail_rows):
        self.lxg = LxgProvider(data_dir, market)
        self.ak = AkProvider(data_dir, market)
        self.start, self.end, self.tail_rows = start, end, tail_rows

    def __call__(self, item):
        symbol, dirs = item
        return merge_symbol(self.lxg, self.ak, dirs, start=self.start, end=self.end,
                            tail_rows=self.tail_rows)


_WORKER: Optional[_Worker] = None


def _init_worker(data_dir, market, start, end, tail_rows) -> None:
    global _WORKER
    _WORKER = _Worker(data_dir, market, start, end, tail_rows)


def _run_worker(item):
    return _WORKER(item)


# --------------------------------------------------------------- fingerprint
def source_fingerprint(root, symbols, dir_names, sources: Sequence[Tuple[str, str]]) -> str:
    """``<file count>:<mtime sum>`` over every ``<dir>/<provider>/<file>``
    the merge reads -- the merged tables are a pure function of exactly
    these (any rewrite of the daily data moves the mtime sum and invalidates
    a cache built on them; the sum rather than the newest mtime, see
    :func:`qf_screen.data.table_cache.fingerprint`).

    ``dir_names(symbol)`` resolves a symbol's directory aliases (a file may
    sit under any of them).  Plain ``os.stat`` on joined strings: pathlib
    costs ~5x more per call, which over ~22,000 source files was 0.2 s of
    every warm run.
    """
    prefix = f"{str(root).rstrip(os.sep)}{os.sep}"
    count, total = 0, 0
    for symbol in symbols:
        for name in dir_names(symbol):
            head = f"{prefix}{name}{os.sep}"
            for provider, filename in sources:
                try:
                    total += os.stat(f"{head}{provider}{os.sep}{filename}").st_mtime_ns
                except OSError:
                    continue
                count += 1
    return f"{count}:{total}"


# ------------------------------------------------------------------- store
class CNDataStore:
    """Local-first CN data facade: per-symbol merge -> wide tables.

    Parameters
    ----------
    data_dir : root of the store; defaults to ``$QUANT_DATA_DIR``.
    symbols : restrict to these symbols (any spelling).  Default: universe.
    start, end : calendar window.  With ``start`` the whole file of every
        symbol is read and clipped; WITHOUT it only the last
        ``warmup_rows`` rows are read (the fast daily-snapshot path) and
        ``end`` must be None.
    warmup_rows : rows read per file in the tail mode (600 ≈ 2.5 years --
        enough for a 250-day window plus the one-day causal shift).
    max_workers : worker PROCESSES for the per-symbol merge (1 = in-process).
        The merge is CPU-bound Python/numpy work, so threads do not help
        (16 threads measured 2.3x slower than sequential); processes do.
    refresh : opt-in live akshare access (``--live``).  Never touches the
        network otherwise.
    akshare : injectable akshare stand-in (tests).
    universe : pre-built :class:`Universe` (default: built here).

    Attributes
    ----------
    calendar : the trading calendar (``DatetimeIndex``).
    last_session : last complete session on the calendar.
    provenance : per-symbol ``DataFrame`` (sources, date range, adjustments).
    warnings : human-readable data caveats found while loading.
    fingerprint : source-file census the merged tables are keyed on
        (``None`` while the cache is off).
    """

    def __init__(self, data_dir=None, market: str = "cn",
                 symbols: Optional[Sequence[str]] = None, start=None, end=None,
                 warmup_rows: int = 600, max_workers: int = 1, refresh: bool = False,
                 ttl_spot: float = 1800.0, ttl_boards: float = 86400.0,
                 akshare=None, universe: Optional[Universe] = None,
                 use_cache: bool = True):
        self.data_dir = Path(data_dir or get_data_dir())
        self.market = market
        self.lxg = LxgProvider(self.data_dir, market)
        self.ak = AkProvider(self.data_dir, market)
        #: read / write the disk caches (``--no-cache`` disables every one:
        #: the universe census, the merged tables and the derived tables)
        self.use_cache = bool(use_cache)
        self.universe = universe if universe is not None \
            else build_universe(self.data_dir, market, use_cache=self.use_cache)

        self.start = pd.Timestamp(start) if start else None
        self.end = pd.Timestamp(end) if end else None
        self.tail_rows: Optional[int] = None if self.start is not None else int(warmup_rows)
        self.symbols = self._select_symbols(symbols)
        self.live: Optional[LiveAkClient] = None
        self.live_date: Optional[pd.Timestamp] = None
        self.live_meta: Dict = {}
        if refresh:
            self.live = LiveAkClient(self.data_dir / market / "ak_live", refresh=True,
                                     ttl_spot=ttl_spot, ttl_boards=ttl_boards, ak=akshare)
        self._wide: Dict[str, pd.DataFrame] = {}
        self._alt: Dict[tuple, object] = {}
        self.provenance = pd.DataFrame()
        self.calendar = pd.DatetimeIndex([], name="date")
        self.last_session = pd.NaT
        self.warnings: List[str] = []
        self._fingerprint: Optional[str] = None
        #: traded names on the last session relative to the one before
        self.last_session_share = 1.0
        self._load(max_workers)

    def __repr__(self) -> str:
        return (f"<CNDataStore {len(self.provenance)} symbols, "
                f"{self.calendar[0].date()}..{self.last_session.date()}>")

    # ----------------------------------------------------------- loading
    def _select_symbols(self, symbols) -> list:
        if symbols is None:
            return list(self.universe.symbols)
        wanted = []
        for raw in symbols:
            canon = to_canonical(str(raw).strip())
            if canon is None:
                raise ValueError(f"{raw!r} is not a CN A-share symbol")
            if canon not in self.universe:
                logger.warning("symbol %s is not in the universe -- skipped", canon)
                continue
            if canon not in wanted:
                wanted.append(canon)
        return wanted

    def _load(self, max_workers: int) -> None:
        t0 = time.time()
        if self.use_cache and self._read_store_cache():
            logger.info("store: %d symbols, %d sessions %s..%s FROM CACHE in %.1fs",
                        len(self.provenance), len(self.calendar),
                        self.calendar[0].date(), self.calendar[-1].date(),
                        time.time() - t0)
            return
        workers = int(max_workers or 1)
        if workers > 1 and len(self.symbols) >= 4 * workers:
            loaded = self._load_parallel(workers)
        else:
            loaded = [merge_symbol(self.lxg, self.ak, self.universe.dir_names(s),
                                   start=self.start, end=self.end, tail_rows=self.tail_rows)
                      for s in self.symbols]
        symbols, day_lists, arrays, rows = [], [], [], []
        for symbol, item in zip(self.symbols, loaded):
            if item is None:
                continue
            days, fields, info = item
            symbols.append(symbol)
            day_lists.append(days)
            arrays.append(fields)
            rows.append({"symbol": symbol, **info})
        if not symbols:
            raise ValueError(f"no lxg/ak price data found for the selected symbols "
                             f"under {self.data_dir}")
        self.provenance = pd.DataFrame(rows).set_index("symbol")
        self._set_calendar(symbols, day_lists, arrays)

        # positional assembly straight onto the calendar; a symbol's rows
        # outside the calendar (before its first session / after its last)
        # are dropped rather than written into a neighbouring row
        calendar = self.calendar.to_numpy().astype(DAY)
        for field in BASE_FIELDS:
            out = np.full((len(calendar), len(symbols)), np.nan, dtype=np.float32)
            for j, fields in enumerate(arrays):
                days = day_lists[j]
                inside = (days >= calendar[0]) & (days <= calendar[-1])
                out[np.searchsorted(calendar, days[inside]), j] = fields[field][inside]
            self._wide[field] = pd.DataFrame(out, index=self.calendar, columns=symbols)
        logger.info("store: %d symbols, %d sessions %s..%s loaded in %.1fs",
                    len(symbols), len(calendar), calendar[0], calendar[-1], time.time() - t0)
        if self.use_cache:
            self._write_store_cache(symbols)

    # ------------------------------------------------------- store cache
    #: per-symbol source files whose stats fingerprint the merged tables
    #: (see :func:`source_fingerprint`)
    _CACHE_SOURCES = (("ak", "basic.csv"), ("lxg", "daily.csv"),
                      ("lxg", "valuation.csv"), ("lxg", "dividend.csv"))

    @staticmethod
    def symbols_key(symbols) -> str:
        """Short hash of a symbol list — the part of a cache directory name
        that tells two symbol sets apart."""
        import hashlib

        return hashlib.sha1("|".join(symbols).encode()).hexdigest()[:10]

    @property
    def window_key(self) -> str:
        """This store's window as a short name — ``tail600_<symbols>`` or
        ``hist<start>-<end>_<symbols>`` — the part that tells the daily
        snapshot and a validation window apart in the cache."""
        mode = f"tail{self.tail_rows}" if self.start is None \
            else f"hist{self.start.date()}-{self.end.date() if self.end else 'open'}"
        return f"{mode}_{self.symbols_key(self.symbols)}"

    @property
    def table_cache_dir(self) -> Path:
        """Where this store's DERIVED feature tables are cached:
        ``qf_screen_cache/tables/<window key>/``.

        Every derived table (alt / statement step / dividend / factor /
        price feature) is aligned to ONE window — this store's calendar and
        symbol list — so each window keeps its own.  With one shared file
        per table, a validation window overwrote the daily snapshot's
        tables and the next morning's ``run`` rebuilt them (~12 s).
        """
        from .table_cache import TABLES_DIR, cache_dir_for

        return cache_dir_for(self.data_dir, self.market) / TABLES_DIR / self.window_key

    def _store_cache_dir(self):
        from qf_screen import __version__
        from .table_cache import cache_dir_for

        return (cache_dir_for(self.data_dir, self.market)
                / f"store_{self.window_key}_v{__version__.replace('.', '')}")

    def _read_store_cache(self) -> bool:
        """Hydrate calendar/provenance/wide tables from the parquet cache.

        Returns False on any miss (absent, stale, unreadable, or the symbol
        alignment changed) — the caller then merges from source.
        """
        directory = self._store_cache_dir()
        try:
            import json

            meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
            if meta.get("fingerprint") != self.fingerprint \
                    or meta.get("base_fields") != list(BASE_FIELDS):
                return False
            provenance = pd.read_parquet(directory / "provenance.parquet")
            if "symbol" in provenance.columns:      # restore the index
                provenance = provenance.set_index("symbol")
            calendar = pd.DatetimeIndex(pd.to_datetime(meta["calendar"]), name="date")
            loaded = list(meta["symbols"])

            # the 16 flat tables decompress in parallel (pyarrow drops the
            # GIL); each read also checks its own alignment
            from concurrent.futures import ThreadPoolExecutor

            def _read(field):
                table = read_wide(directory / f"wide_{field}.parquet",
                                  index=calendar, columns=loaded)
                return field, table

            with ThreadPoolExecutor(max_workers=min(4, len(BASE_FIELDS))) as pool:
                wide = dict(pool.map(_read, BASE_FIELDS))
        except Exception:  # noqa: BLE001 - any cache problem is a miss
            return False
        if len(calendar) == 0 or any(table is None for table in wide.values()):
            return False
        self.calendar = calendar
        self.last_session = calendar[-1]
        self.provenance = provenance
        self._wide = wide
        self.last_complete_session = pd.Timestamp(meta["last_complete_session"])
        self.last_session_share = float(meta.get("last_session_share", 1.0))
        self.warnings.extend(meta.get("warnings", []))
        return True

    def _write_store_cache(self, symbols: List[str]) -> None:
        """Persist the merged tables atomically (best effort — an
        unwritable cache directory only costs speed).  Every table lands
        before the ``meta.json`` that names the fingerprint, so a process
        killed mid-write leaves a cache the next run rebuilds, never one it
        trusts."""
        import json

        directory = self._store_cache_dir()
        try:
            import pyarrow  # noqa: F401

            directory.mkdir(parents=True, exist_ok=True)
            provenance = self.provenance.reset_index()
            tmp = directory / "provenance.parquet.tmp"
            provenance.to_parquet(tmp, index=False)
            tmp.replace(directory / "provenance.parquet")
            for field in BASE_FIELDS:
                tmp = directory / f"wide_{field}.parquet.tmp"
                write_wide(tmp, self._wide[field])
                tmp.replace(directory / f"wide_{field}.parquet")
            meta = {
                "fingerprint": self.fingerprint,
                "symbols": list(symbols),
                "calendar": [str(d.date()) for d in self.calendar],
                "last_complete_session": str(self.last_complete_session.date()),
                "last_session_share": self.last_session_share,
                "warnings": list(self.warnings),
                "base_fields": list(BASE_FIELDS),
            }
            tmp = directory / "meta.json.tmp"
            tmp.write_text(json.dumps(meta), encoding="utf-8")
            tmp.replace(directory / "meta.json")
        except Exception as exc:  # noqa: BLE001
            logger.warning("store cache write failed (%s) -- continuing without", exc)

    def _load_parallel(self, workers: int) -> list:
        """``merge_symbol`` over ``workers`` (processes when the caller's
        ``__main__`` is pool-safe, threads otherwise — see
        :mod:`qf_screen.data._parallel` for why a spawn-family worker can
        be unsafe in some interpreters)."""
        from ._parallel import map_workers

        items = [(symbol, self.universe.dir_names(symbol)) for symbol in self.symbols]
        chunk = max(1, min(64, len(items) // (workers * 4) or 1))
        return map_workers(_run_worker, items, workers=workers, chunksize=chunk,
                           initializer=_init_worker,
                           initargs=(str(self.data_dir), self.market, self.start,
                                     self.end, self.tail_rows))

    def _set_calendar(self, symbols: List[str], day_lists: List[np.ndarray],
                      arrays: List[Dict[str, np.ndarray]]) -> None:
        """Calendar = the contiguous run of complete sessions ending at the
        last one, over the symbols that trade regularly and traded recently.

        Derived from the per-symbol date arrays BEFORE any wide table
        exists.  Several kinds of rows must stay out of it: the old tail of
        a name that stopped trading long ago, the blank rows a long
        suspension leaves in a file, the isolated rows of a sparse file, and
        a trailing partial "today" session.
        """
        prov = self.provenance
        cutoff = prov["last_date"].max() - pd.Timedelta(days=_RECENT_DAYS)
        recent = prov["last_date"] >= cutoff
        regular = prov["traded_rows"] >= _REGULAR_TRADED_SHARE * prov["rows"]
        source = np.flatnonzero((recent & regular).to_numpy())
        if len(source) == 0:
            source = np.flatnonzero(recent.to_numpy())
        if len(source) == 0:
            source = np.arange(len(symbols))
        all_days = np.unique(np.concatenate([day_lists[j] for j in range(len(symbols))]))
        row_days = np.concatenate([day_lists[j] for j in source])
        valid_days = np.concatenate([day_lists[j][np.isfinite(arrays[j]["close_raw"])
                                                 & (arrays[j]["close_raw"] > 0)]
                                     for j in source])
        counts = np.bincount(np.searchsorted(all_days, row_days),
                             minlength=len(all_days)).astype(float)
        valid = np.bincount(np.searchsorted(all_days, valid_days),
                            minlength=len(all_days)).astype(float)

        gap = np.diff(all_days.astype("datetime64[D]").astype(np.int64))
        dense = np.ones(len(all_days), dtype=bool)
        dense[:-1] &= gap <= _DENSE_GAP_DAYS
        dense[1:] &= gap <= _DENSE_GAP_DAYS
        if self.tail_rows is not None:
            # tail mode: every source holds at most `tail_rows` rows ending
            # near the last date, so a real session has rows for (almost)
            # ALL of them.  A reference relative to the trailing median
            # would adapt to a sparse stretch -- e.g. the 2021-22 rows of a
            # stock whose 600-row tail spans a two-year trading gap -- and
            # accept it; an absolute bar does not.
            reference = np.full(len(all_days), _TAIL_SESSION_SHARE * len(source))
        else:
            # history mode: the listed universe grows over the window, so
            # completeness has to be relative to the trailing level
            reference = pd.Series(counts).rolling(250, min_periods=1).median() \
                .clip(lower=1.0).to_numpy()
        is_complete = dense & (valid >= _COMPLETE_FRACTION * reference)
        complete = np.flatnonzero(is_complete)
        if len(complete) == 0:
            raise ValueError("could not derive a trading calendar from the data")
        end = int(complete[-1])
        broken = np.flatnonzero(~is_complete[:end])
        start = int(broken[-1]) + 1 if len(broken) else 0
        if start > 0:
            logger.info("calendar: %d rows before %s left out (rows of names that "
                        "stopped trading, suspended stretches or sparse files)",
                        start, str(all_days[start]))
        days = all_days[start:end + 1].astype("datetime64[ns]")
        self.calendar = pd.DatetimeIndex(days, name="date")
        self.last_session = self.calendar[-1]
        self.provenance["stale"] = prov["last_date"].notna() & (prov["last_date"] < self.last_session)

        # walk trailing sessions whose close count collapsed vs the one
        # before (a daily download still in flight) -- the last one that
        # looks complete is the default DECISION date, not the last row
        tail = valid[start:end + 1]
        cut = len(tail) - 1
        while cut > 0 and tail[cut] < _INCOMPLETE_SHARE * max(tail[cut - 1], 1.0):
            cut -= 1
        self.last_complete_session = self.calendar[cut]
        if cut < len(tail) - 1:
            share = tail[-1] / max(tail[-2], 1.0)
            self.last_session_share = float(share)
            self.warnings.append(
                f"only {share:.0%} as many names have a close on the last session "
                f"{self.last_session.date()} as on the one before -- the daily data "
                f"update may be incomplete; the default decision date is "
                f"{self.last_complete_session.date()}")

    # ------------------------------------------------------------ access
    @property
    def fingerprint(self) -> Optional[str]:
        """``source_fingerprint`` over the files this store's tables were
        merged from — computed once per store, and only while the cache is
        on (``--no-cache`` neither reads nor writes anything keyed on it).
        The price-feature cache (:mod:`qf_screen.rules.base`) keys on it
        too: those features are pure functions of these tables."""
        if self._fingerprint is None and self.use_cache:
            self._fingerprint = source_fingerprint(
                self.lxg.root, self.symbols, self.universe.dir_names, self._CACHE_SOURCES)
        return self._fingerprint

    def wide(self, name: str) -> pd.DataFrame:
        """As-traded wide table ``date x symbol`` (row ``t`` = day ``t``)."""
        if name not in self._wide:
            raise KeyError(f"unknown base field {name!r}; known: {', '.join(BASE_FIELDS)}")
        return self._wide[name]

    @property
    def decision_date(self) -> pd.Timestamp:
        """The date a snapshot should be decided on: the provisional live
        row when one is attached, else the last COMPLETE session (a
        trailing session whose close count collapsed is an update in
        flight, not a market move)."""
        return self.live_date if self.live_date is not None \
            else self.last_complete_session

    @property
    def asof_default(self) -> pd.Timestamp:
        """Alias of :attr:`decision_date`."""
        return self.decision_date

    # ------------------------------------------------- per-symbol sources
    def symbols_with_file(self, provider: str, filename: str) -> List[str]:
        """Loaded symbols whose directory tree actually carries
        ``<provider>/<filename>`` (a skip list: the alt/dividend/factor
        builders iterate only these instead of stat-ing ~3,400 empty dirs
        every build).  ``provider`` is ``"lxg"`` or ``"ak"``.

        Cached per (provider, filename) for the life of the store.
        """
        key = ("have_file", provider, filename)
        if key not in self._alt:
            backend = self.lxg if provider == "lxg" else self.ak
            dirs = self.universe.dirs
            self._alt[key] = [s for s in self.symbols
                              if backend.find(dirs.get(s, ()), filename) is not None]
        return self._alt[key]

    def _cached(self, key: tuple, loader):
        if key not in self._alt:
            self._alt[key] = loader()
        return self._alt[key]

    def alt(self, symbol: str, kind: str, columns: Optional[Sequence[str]] = None):
        """A per-symbol lxg alternative-data frame (``kind`` in
        :data:`~.lxg_provider.LXG_FILES` minus daily/valuation/dividend), or
        ``None``.  Cached per ``(symbol, kind)``."""
        if kind in ("daily", "valuation", "dividend"):
            raise KeyError(f"use the dedicated accessor for {kind!r}")
        return self._cached((symbol, kind), lambda: self.lxg.alt(
            self.universe.dir_names(symbol), kind, columns=columns))

    def pledge_ratio(self, symbol: str):
        """Share of total equity under pledge (fraction) by date, or ``None``."""
        return self._cached((symbol, "pledge_ratio"),
                            lambda: self.lxg.pledge_ratio(self.universe.dir_names(symbol)))

    def research_reports(self, symbol: str):
        """Analyst reports of a symbol (``report_date, rating, org, title,
        eps_fy1``), or ``None``."""
        return self._cached((symbol, "reports"),
                            lambda: self.ak.research_reports(self.universe.dir_names(symbol)))

    def dividend_events(self, symbol: str):
        """Raw lxg dividend records of a symbol (ALL statuses; see
        :meth:`LxgProvider.dividend_events`), or ``None``.  Cached."""
        return self._cached((symbol, "dividend"), lambda: self.lxg.dividend_events(
            self.universe.dir_names(symbol)))

    def index_close(self, code: str):
        """Daily close of an index from the store's lxg index tree
        (:meth:`LxgProvider.index_close`; ``"000300"`` ...), or ``None``
        when the store has no such file.  A benchmark series — nothing in
        a screen reads it.  Cached per code."""
        return self._cached(("index", code), lambda: self.lxg.index_close(code))

    def finance(self, symbol: str):
        """TTM canonical statement frame from the lxg bridge
        (:func:`qf_factor.factory.lxg_finance.lxg_trade_finance_data_adapter`:
        index ``(symbol, announce_date)``, statutory-lag PIT dating), or
        ``None``.  Cached."""
        def _load():
            from qf_factor.factory.lxg_finance import lxg_trade_finance_data_adapter
            org, _quarterly, _ttm = lxg_trade_finance_data_adapter(
                symbol, self.market, str(self.data_dir))
            return org
        return self._cached((symbol, "finance"), _load)

    # ------------------------------------------------------------ live
    def _live(self) -> LiveAkClient:
        if self.live is None:
            raise LiveDataError("live refresh is off for this store "
                                "(construct with refresh=True / --live)")
        return self.live

    def attach_live_spot(self) -> Dict:
        """Append the live spot snapshot as a provisional last row.

        The row is dated with the fetch date and carries the live close,
        valuation and turnover; the adjusted close extends the chain by
        ``live_close / last_raw_close``.  History is untouched.  Returns the
        cache metadata (``fetched_at``, ``fresh`` ...).
        """
        spot, meta = self._live().spot()
        quote_date = pd.Timestamp(meta["fetched_at"]).normalize()
        close_raw = self.wide("close_raw")
        row = spot.set_index("symbol").reindex(close_raw.columns)
        prev_raw = close_raw.ffill().iloc[-1].astype(float)
        prev_adj = self.wide("close").ffill().iloc[-1].astype(float)
        live_close = pd.to_numeric(row["close"], errors="coerce")
        scale = live_close / prev_raw
        scale = scale.where(np.isfinite(scale) & (scale > 0))
        sources = {"close_raw": "close", "amount": "amount", "turnoverrate": "turnoverrate",
                   "pe_ttm": "pe_ttm", "pb": "pb", "market_cap": "market_capital",
                   "float_market_cap": "float_market_capital"}
        for field, source in sources.items():
            table = self._wide[field]
            values = pd.to_numeric(row[source], errors="coerce").reindex(table.columns)
            table.loc[quote_date] = values.to_numpy().astype("float32")
        adjusted = (prev_adj * scale).reindex(self.wide("close").columns)
        self._wide["close"].loc[quote_date] = adjusted.to_numpy().astype("float32")
        if self.live_date is None or quote_date > self.calendar[-1]:
            self.calendar = self.calendar.append(pd.DatetimeIndex([quote_date]))
        self.live_date = quote_date
        self.live_meta = dict(meta)
        logger.info("live spot row %s attached (%d names quoted, fresh=%s)",
                    quote_date.date(), int(live_close.notna().sum()), meta.get("fresh"))
        return self.live_meta

    def board_members(self, kind: str = "industry") -> pd.DataFrame:
        """Live 东财 board constituents (``board, code, symbol, name``);
        ``kind`` is ``"industry"`` or ``"concept"``.  Disk-cached with a TTL."""
        client = self._live()
        frame, _meta = client.industry_members() if kind == "industry" \
            else client.concept_members()
        return frame

    def enrich_industry_tags(self) -> int:
        """Give names WITHOUT a local lxg industry an ``industry_l1`` tag
        from the 东财 industry boards (``--live``; the board file is
        disk-cached for the day).

        The 申万 classification of ``lxg/industries.csv`` covers only the
        ~2,000 names with an lxg tree; the other ~3,400 carry no industry
        at all, which is where a sector breakdown or a ``max_per_industry``
        cap goes blind.  A local tag is never overwritten -- the fallback
        only fills gaps -- and a name in several boards gets them joined
        with ``/``.  Returns the number of tags filled.
        """
        if "industry_l1" not in self.universe.meta.columns:
            return 0
        meta = self.universe.meta
        frame = self.board_members("industry")
        boards: Dict[str, List[str]] = {}
        for symbol, group in frame.groupby("symbol")["board"]:
            boards[str(symbol)] = sorted({str(b) for b in group})
        filled = 0
        for symbol in self.symbols:
            if symbol not in meta.index or symbol not in boards:
                continue
            current = meta.at[symbol, "industry_l1"]
            if isinstance(current, str) and current.strip():
                continue                            # a local tag always wins
            meta.at[symbol, "industry_l1"] = "/".join(boards[symbol])
            filled += 1
        if filled:
            logger.info("industry tags: %d names classified from the 东财 boards "
                        "(no local lxg industries.csv)", filled)
        return filled

    # ------------------------------------------------------------ status
    def data_status(self) -> Dict[str, object]:
        """Coverage / freshness census for ``qf-screen data-status``."""
        prov, uni = self.provenance, self.universe
        boards = uni.meta["board"].reindex(uni.symbols).value_counts()
        loaded = uni.meta["board"].reindex(prov.index).value_counts()
        alias = {s: uni.dir_names(s) for s in prov.index}

        def _count(provider: str, filename: str) -> int:
            root = self.lxg.root if provider == "lxg" else self.ak.root
            return sum(1 for s, names in alias.items()
                       if any((root / d / provider / filename).exists() for d in names))

        traded = self.wide("close_raw").notna()
        return {
            "data_dir": str(self.data_dir),
            "window": {
                "mode": "history" if self.start is not None else f"tail({self.tail_rows} rows)",
                "start": str(self.calendar[0].date()),
                "end": str(self.last_session.date()),
                "sessions": int(len(self.calendar)),
                "live_row": str(self.live_date.date()) if self.live_date is not None else None,
            },
            "universe": {
                "symbols": int(len(uni)), "loaded": int(len(prov)),
                "by_board": {b: int(boards.get(b, 0)) for b in ALL_BOARDS},
                "loaded_by_board": {b: int(loaded.get(b, 0)) for b in ALL_BOARDS},
                "in_company_list": int(uni.meta["in_company_list"].sum()),
                "st_named": int(len(uni.st_named)),
                "names_asof": str(uni.names_asof.date()) if uni.names_asof is not None else None,
                "with_industry": int(uni.meta["industry_l1"].notna().sum())
                if "industry_l1" in uni.meta.columns else 0,
            },
            "price": {
                "lxg_daily": int(prov["lxg_daily"].sum()),
                "ak_basic": int(prov["ak_basic"].sum()),
                "traded_last_session": int(traded.iloc[-1].sum()),
                "traded_previous_session": int(traded.iloc[-2].sum()) if len(traded) > 1 else None,
                "stale": int(prov["stale"].sum()),
                "split_adjusted_events": int(prov["split_adj"].sum()),
                "dividend_adjusted_events": int(prov["dividend_adj"].sum()),
                "dividend_events_rejected": int(prov["dividend_rejected"].sum()),
            },
            "amount_source": {k: int(v) for k, v in prov["amount_source"].value_counts().items()},
            "valuation_source": {k: int(v) for k, v in prov["val_source"].value_counts().items()},
            "files_on_disk": {
                **{f"lxg_{kind}": _count("lxg", f"{kind}.csv") for kind in LXG_FILES},
                "lxg_income_quarterly": _count("lxg", "income_quarterly.csv"),
                "ak_research_reports": _count("ak", "research_reports.csv"),
            },
            "alt_freshness": self._alt_freshness(),
            "warnings": list(self.warnings),
        }

    def _alt_freshness(self) -> Dict[str, Dict[str, object]]:
        """Per alt kind: file count and the NEWEST source file mtime — the
        honest signal that an alt download (northbound, margin ...) lags
        the price files, so change features on it forward-fill stale
        values.  mtime is a good proxy here because the lxg bulk download
        rewrites each file when it extends it."""
        out: Dict[str, Dict[str, object]] = {}
        for kind in ("mutual_market", "margin", "pledge", "exec_shares_change",
                     "block_deal", "shareholders_num"):
            have = self.symbols_with_file("lxg", f"{kind}.csv")
            newest = 0.0
            for symbol in have:
                path = self.lxg.find(self.universe.dir_names(symbol), f"{kind}.csv")
                if path is not None:
                    try:
                        newest = max(newest, path.stat().st_mtime)
                    except OSError:
                        continue
            out[kind] = {
                "files": len(have),
                "newest_mtime": pd.Timestamp(newest, unit="s").strftime("%Y-%m-%d")
                if newest else None,
            }
        return out
