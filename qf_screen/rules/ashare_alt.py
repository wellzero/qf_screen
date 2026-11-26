"""A-share alternative-data features (the "smart money" family).

Each feature is built from ONE per-symbol lxg/ak file, lazily: a screen
that never mentions ``pledge_ratio`` never reads a ``pledge.csv``.  The
tables cost real I/O when first used (one file per symbol, ~2,000 lxg
names) — the first screen that uses them pays it, the panel caches.

Semantics and units (verified on the store 2026-11):

* **northbound** (``mutual_market.csv``, 沪深港通 holdings, shares):
  ``nb_hold_chg_{20,60}d`` = holdings / holdings N file-rows earlier - 1.
  NaN for names never in the program (they fail such filters
  conservatively).
* **margin** (``margin.csv``): ``margin_chg_20d`` = financing-balance
  change over 20 of the file's own rows - 1 (unit-free).  NOTE the file's
  early years are denominated in 万元 and later ones in 元 — never use the
  raw balance level, only unit-free changes.
* **holders** (``shareholders_num.csv``, quarterly): ``holder_num_chg`` =
  latest report's count / previous report's - 1, ffilled from each report
  date (a step function, as-traded).
* **pledge**: ``pledge_ratio`` = fraction of total equity pledged,
  ffilled from the event/summary series.
* **analysts** (``ak/research_reports.csv``): ``report_cnt_90d`` = count
  of reports in the trailing 90 calendar days; ``buy_report_ratio_90d`` =
  share of them rated 买入/增持 (NaN when there are none).
* **insiders** (``exec_shares_change.csv``):
  ``insider_net_180d_mc`` = net executive share purchases over the
  trailing 180 calendar days, valued at each trade's price (CNY, negative
  = net selling), divided by market cap.
* **block trades** (``block_deal.csv``): ``block_discount_180d`` = mean
  discount rate of block deals in the trailing 180 calendar days
  (discountRate is already a fraction; NaN = no deals).
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Optional, Tuple

import numpy as np
import pandas as pd

from .base import FeaturePanel, _safe_div, register_feature

logger = logging.getLogger(__name__)

_BUY_RATINGS = frozenset({"买入", "增持"})

EventExtractor = Callable[[pd.DataFrame], Tuple[Optional[np.ndarray], Optional[np.ndarray]]]

#: bounded forward-fill applied to CHANGE features (``nb_hold_chg_*``,
#: ``margin_chg_20d``): an alt file that lags the price files keeps its last
#: computed value for at most this many sessions.  Causal by construction —
#: ffill only carries PAST observations forward — and bounded so a source
#: that died months ago (northbound disclosure, say) surfaces as NaN again
#: instead of freezing forever.  Override with ``QF_SCREEN_ALT_FFILL``.
ALT_FFILL_LIMIT = max(0, int(os.environ.get("QF_SCREEN_ALT_FFILL", "20")))


# ------------------------------------------------------------ machinery
def _step_from_events(panel: FeaturePanel, loader: Callable, extract: EventExtractor,
                      have: Optional[list] = None) -> pd.DataFrame:
    """Event series -> step table: per symbol, dated (date, value) pairs
    forward-filled onto the calendar (levels that hold until superseded)."""
    calendar, symbols = panel.calendar, panel.symbols
    values = np.full((len(calendar), len(symbols)), np.nan)
    where = {s: i for i, s in enumerate(symbols)}
    for symbol in (have if have is not None else symbols):
        frame = loader(panel, symbol)
        if frame is None or frame.empty:
            continue
        dates, vals = extract(frame)
        if dates is None or len(dates) == 0:
            continue
        series = pd.Series(np.asarray(vals, dtype=float),
                           index=pd.DatetimeIndex(dates))
        series = series[series.index.notna()].groupby(level=0).last().sort_index()
        series = series[series.index <= calendar[-1]]
        if series.empty or symbol not in where:
            continue
        values[:, where[symbol]] = series.reindex(calendar, method="ffill") \
            .to_numpy(dtype=float)
    return pd.DataFrame(values, index=calendar, columns=symbols)


def _window_sum(panel: FeaturePanel, loader: Callable, extract: EventExtractor,
                window_days: int, have: Optional[list] = None
                ) -> pd.DataFrame:
    """Event series -> trailing-window SUM table: per symbol, the total of
    event values with dates in ``(t - window_days, t]`` for each row ``t``
    (0 when the file exists but no event falls inside).

    The 1-D ``days`` in the searchsorted calls matters: a ``(N, 1)`` needle
    broadcasts the cumulative sums against per-symbol arrays and corrupts
    the result (the v0.1 shape bug this replaces).
    """
    calendar, symbols = panel.calendar, panel.symbols
    values = np.full((len(calendar), len(symbols)), np.nan)
    where = {s: i for i, s in enumerate(symbols)}
    days = calendar.to_numpy().astype("datetime64[D]")
    for symbol in (have if have is not None else symbols):
        frame = loader(panel, symbol)
        if frame is None or frame.empty or symbol not in where:
            continue
        event_days, vals = extract(frame)
        if event_days is None or len(event_days) == 0:
            continue
        order = np.argsort(event_days)
        event_days = np.asarray(event_days)[order].astype("datetime64[D]")
        vals = np.nan_to_num(np.asarray(vals, dtype=float))[order]
        cumulative = np.concatenate([[0.0], np.cumsum(vals)])
        hi = np.searchsorted(event_days, days, side="right")
        lo = np.searchsorted(event_days,
                             days - np.timedelta64(window_days, "D"),
                             side="right")
        values[:, where[symbol]] = cumulative[hi] - cumulative[lo]
    return pd.DataFrame(values, index=calendar, columns=symbols)


def _change_table(panel: FeaturePanel, kind: str, column: str, rows: int,
                  ffill_limit: Optional[int] = None,
                  feature: str = "") -> pd.DataFrame:
    """``value / value N file-rows earlier - 1`` of a per-symbol dated
    series, aligned to the calendar, then BOUNDED forward-filled.

    The ffill is the staleness fix: lxg's alt downloads lag ``ak/basic.csv``
    by days to weeks, and without it a screen decided on the last complete
    session sees NaN for every name (0 members out of 5,472 — the v0.1
    failure).  Carrying the last computed change forward is strictly
    causal; the bound keeps a dead source from freezing forever.

    ``feature`` is the registered feature name the table serves; it heads
    the staleness warning, because one lagging file feeds SEVERAL features
    (``nb_hold_chg_20d`` and ``nb_hold_chg_60d`` read the same
    ``mutual_market.csv``) and two anonymous lines about one file with two
    different counts read like a bug rather than a fact.
    """
    calendar, symbols = panel.calendar, panel.symbols
    limit = ALT_FFILL_LIMIT if ffill_limit is None else int(ffill_limit)
    store = panel.store
    have = store.symbols_with_file("lxg", f"{kind}.csv")
    cache_key = f"alt_change_{kind}_{column}_{rows}"
    files = [store.lxg.find(store.universe.dir_names(s), f"{kind}.csv")
             for s in have]
    fp = _derived_fingerprint(files, calendar, extra=str(limit),
                              symbols=panel.symbols)

    def build() -> pd.DataFrame:
        values = np.full((len(calendar), len(symbols)), np.nan)
        where = {s: i for i, s in enumerate(symbols)}
        stale = 0
        newest = None
        for symbol in have:
            frame = store.alt(symbol, kind, columns=(column,))
            if frame is None or column not in frame.columns or symbol not in where:
                continue
            series = pd.Series(pd.to_numeric(frame[column], errors="coerce").to_numpy(),
                               index=pd.DatetimeIndex(frame["date"]))
            series = series[series.index.notna() & series.notna()]
            series = series[~series.index.duplicated(keep="last")].sort_index()
            if len(series) <= rows:
                continue
            with np.errstate(invalid="ignore", divide="ignore"):
                changed = series / series.shift(rows) - 1.0
            changed = changed[changed.index <= calendar[-1]]
            if changed.empty:
                continue
            filled = changed.reindex(calendar)
            if limit:
                filled = filled.ffill(limit=limit)
            values[:, where[symbol]] = filled.to_numpy(dtype=float)
            last = series.index[-1]
            if last < calendar[0]:
                stale += 1
            elif newest is None or last > newest:
                newest = last
        label = feature or f"{kind}.{column}/{rows}d"
        if have and newest is not None and newest < calendar[-1] - pd.Timedelta(days=7):
            panel.warnings.append(
                f"{label} [{kind}.csv]: freshest observation {newest.date()} precedes "
                f"the last session {calendar[-1].date()} ({stale} of {len(have)} files "
                f"carry no row inside the window) -- forward-filled (limit {limit} "
                f"sessions); the screen may be deciding on stale data")
        return pd.DataFrame(values, index=calendar, columns=symbols)

    return _cached_wide(panel, cache_key, fp, build)


def _derived_fingerprint(files: list, calendar: pd.DatetimeIndex, extra: str = "",
                         symbols: Optional[pd.Index] = None) -> str:
    """Fingerprint + calendar bounds + the SYMBOL LIST hash (see
    :func:`qf_screen.data.table_cache.derived_fingerprint` for why the
    symbol hash is load-bearing)."""
    from qf_screen.data.table_cache import derived_fingerprint, fingerprint

    if symbols is None:
        return fingerprint(files, extra=f"{len(calendar)}:{calendar[0].date()}:"
                                        f"{calendar[-1].date()}::{extra}")
    return derived_fingerprint(files, calendar, symbols, extra)


def _cached_wide(panel_or_store, key: str, fp: str, build: Callable[[], pd.DataFrame]):
    from qf_screen.data.table_cache import cached_wide_table

    panel = panel_or_store if hasattr(panel_or_store, "warnings") else None
    store = panel.store if panel is not None else panel_or_store
    return cached_wide_table(store.table_cache_dir, key, fp,
                             build, warnings_sink=panel.warnings if panel else None,
                             use_cache=store.use_cache)


# ------------------------------------------------------------- extractors
def _holder_change(frame: pd.DataFrame):
    if "num" not in frame.columns:
        return None, None
    num = pd.to_numeric(frame["num"], errors="coerce")
    dates = pd.to_datetime(frame["date"], errors="coerce")
    series = pd.Series(num.to_numpy(), index=dates).dropna()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    if len(series) < 2:
        return None, None
    change = (series / series.shift(1) - 1.0).iloc[1:]
    return change.index.to_numpy(), change.to_numpy()


def _pledge_value(frame: pd.DataFrame):
    for column in ("pledgeRatio", "accumulatedPledgePercentageOfTotalEquity"):
        if column in frame.columns and frame[column].notna().any():
            values = pd.to_numeric(frame[column], errors="coerce")
            dates = pd.to_datetime(frame["date"], errors="coerce")
            ok = values.notna() & dates.notna()
            return dates[ok].to_numpy(), values[ok].to_numpy()
    return None, None


def _report_count(frame: pd.DataFrame):
    return pd.DatetimeIndex(frame["report_date"]).to_numpy(), np.ones(len(frame))


def _report_buy_count(frame: pd.DataFrame):
    rating = frame["rating"].astype(str).str.strip() \
        if "rating" in frame.columns else pd.Series([""] * len(frame), index=frame.index)
    buy = rating.isin(_BUY_RATINGS).to_numpy().astype(float)
    return pd.DatetimeIndex(frame["report_date"]).to_numpy(), buy


def _insider_value(frame: pd.DataFrame):
    if "changedShares" not in frame.columns or "date" not in frame.columns:
        return None, None
    changed = pd.to_numeric(frame["changedShares"], errors="coerce")
    price = pd.to_numeric(frame["avgPrice"], errors="coerce") \
        if "avgPrice" in frame.columns else pd.Series(np.nan, index=frame.index)
    amount = pd.to_numeric(frame["sharesChangeAmount"], errors="coerce") \
        if "sharesChangeAmount" in frame.columns else pd.Series(np.nan, index=frame.index)
    # prefer shares*price; fall back to the file's own value column
    value = (changed * price).where((changed * price).notna(), amount)
    dates = pd.to_datetime(frame["date"], errors="coerce")
    ok = dates.notna() & value.notna()
    if not ok.any():
        return None, None
    return dates[ok].to_numpy(), value[ok].to_numpy()


def _block_discount(frame: pd.DataFrame):
    if "discountRate" not in frame.columns:
        return None, None
    values = pd.to_numeric(frame["discountRate"], errors="coerce")
    dates = pd.to_datetime(frame["date"], errors="coerce")
    ok = values.notna() & dates.notna()
    if not ok.any():
        return None, None
    return dates[ok].to_numpy(), values[ok].to_numpy()


# ------------------------------------------------------------- builders
def _register_alt_features() -> None:
    def cached_step(feature: str, provider: str, filename: str, loader, extract):
        """A step-table feature, built only over symbols that HAVE the file
        and read through the derived-table disk cache."""
        def build(panel: FeaturePanel) -> pd.DataFrame:
            store = panel.store
            have = store.symbols_with_file(provider, filename)
            files = [_provider_find(store, provider, s, filename) for s in have]
            fp = _derived_fingerprint(files, panel.calendar, symbols=panel.symbols)
            return _cached_wide(
                panel, f"alt_step_{feature}", fp,
                lambda: _step_from_events(panel, loader, extract, have=have))
        return build

    def cached_window(feature: str, provider: str, filename: str, loader, extract,
                      window_days: int):
        def build(panel: FeaturePanel) -> pd.DataFrame:
            store = panel.store
            have = store.symbols_with_file(provider, filename)
            files = [_provider_find(store, provider, s, filename) for s in have]
            fp = _derived_fingerprint(files, panel.calendar, extra=str(window_days),
                                      symbols=panel.symbols)
            return _cached_wide(
                panel, f"alt_window_{feature}", fp,
                lambda: _window_sum(panel, loader, extract, window_days, have=have))
        return build

    for rows in (20, 60):
        register_feature(
            f"nb_hold_chg_{rows}d",
            lambda p, r=rows: _change_table(p, "mutual_market", "shareholdings", r,
                                            feature=f"nb_hold_chg_{r}d"),
            f"northbound (沪深港通) holdings change over the file's trailing "
            f"{rows} rows (NaN = never in the program; bounded ffill "
            f"{ALT_FFILL_LIMIT} sessions on a lagging file)", kind="table",
            replace=True)

    register_feature(
        "margin_chg_20d",
        lambda p: _change_table(p, "margin", "financingBalance", 20,
                                feature="margin_chg_20d"),
        "margin financing-balance change over 20 rows (unit-free; the file's "
        "early years mix 万元/元 so levels are meaningless; bounded ffill on "
        "a lagging file)", kind="table")

    register_feature(
        "holder_num_chg",
        cached_step("holder_num_chg", "lxg", "shareholders_num.csv",
                    lambda p, s: p.store.alt(s, "shareholders_num", columns=("num",)),
                    _holder_change),
        "shareholder-count change, latest report vs previous (step, ffilled "
        "from report dates)", kind="table")

    register_feature(
        "pledge_ratio",
        cached_step("pledge_ratio", "lxg", "pledge.csv",
                    lambda p, s: p.store.alt(
                        s, "pledge",
                        columns=("pledgeRatio",
                                 "accumulatedPledgePercentageOfTotalEquity")),
                    _pledge_value),
        "share of total equity under pledge (step, ffilled)", kind="table")

    register_feature(
        "report_cnt_90d",
        cached_window("report_cnt_90d", "ak", "research_reports.csv",
                      lambda p, s: p.store.research_reports(s), _report_count, 90),
        "analyst reports in the trailing 90 calendar days (0 = covered but "
        "silent; NaN = no report file)", kind="table")

    def buy_ratio(panel: FeaturePanel) -> pd.DataFrame:
        buy = cached_window("buy_report_cnt_90d", "ak", "research_reports.csv",
                            lambda p, s: p.store.research_reports(s),
                            _report_buy_count, 90)(panel)
        count = panel.table("report_cnt_90d")
        ratio = _safe_div(buy, count.where(count > 0))
        return ratio                                      # NaN when no reports

    register_feature(
        "buy_report_ratio_90d", buy_ratio,
        "share of trailing-90d analyst reports rated 买入/增持 (NaN = none)",
        kind="table")

    def insider_net(panel: FeaturePanel) -> pd.DataFrame:
        value = cached_window("insider_net_180d", "lxg", "exec_shares_change.csv",
                              lambda p, s: p.store.alt(
                                  s, "exec_shares_change",
                                  columns=("changedShares", "avgPrice",
                                           "sharesChangeAmount")),
                              _insider_value, 180)(panel)
        return _safe_div(value, panel.store.wide("market_cap").astype(float))

    register_feature(
        "insider_net_180d_mc", insider_net,
        "net executive share purchases over 180d (CNY at trade price, "
        "negative = net selling) / market cap", kind="table")

    def block_discount(panel: FeaturePanel) -> pd.DataFrame:
        total = cached_window("block_total_180d", "lxg", "block_deal.csv",
                              lambda p, s: p.store.alt(
                                  s, "block_deal", columns=("discountRate",)),
                              _block_discount, 180)(panel)
        count = cached_window("block_cnt_180d", "lxg", "block_deal.csv",
                              lambda p, s: p.store.alt(
                                  s, "block_deal", columns=("discountRate",)),
                              lambda f: (_block_discount(f)[0],
                                         np.ones(len(_block_discount(f)[0]))), 180)(panel)
        return _safe_div(total, count.where(count > 0))

    register_feature(
        "block_discount_180d", block_discount,
        "mean block-deal discount rate over 180d (fraction; NaN = no deals)",
        kind="table")


def _provider_find(store, provider: str, symbol: str, filename: str):
    backend = store.lxg if provider == "lxg" else store.ak
    return backend.find(store.universe.dir_names(symbol), filename)


_register_alt_features()
