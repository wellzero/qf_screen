"""Fundamental features: statements, valuation, dividend yield.

Statement features come from the lxg finance bridge
(:func:`qf_factor.factory.lxg_finance.lxg_trade_finance_data_adapter`):
TTM canonical fields dated by their CONSERVATIVE STATUTORY announcement
date (CSRC deadlines: Q1 +30d, H1 +62d, Q3 +31d, annual +120d — the bridge
has no real announcement dates), stepped onto the trading calendar with
forward-fill: row ``t`` carries the latest report ANNOUNCED on or before
``t``.  That is point-in-time — a report only enters after its deadline.

Coverage honesty: only ~765 of ~5,500 symbols have lxg statement pages on
disk.  For everyone else these features are NaN, and NaN fails a filter —
a fundamental screen therefore screens the lxg-covered subset.  The
engine's funnel makes that visible (pass counts drop at the first
fundamental filter); ``data-status`` reports the file census.

Like the alt tables, every statement field is a derived WIDE table read
through the fingerprinted disk cache (:mod:`qf_screen.data.table_cache`):
a warm run pays one parquet read per field, not the per-symbol parse (the
cache miss) nor the per-process re-pivot a raw payload cache would cost.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from qf_screen.data import CNDataStore

from .base import (
    FeaturePanel, Filter, _coalesce, _safe_div, _safe_sub, register_feature,
)
from .hygiene import ExprFilter

logger = logging.getLogger(__name__)

#: statement file the finance caches fingerprint -- the lxg bulk download
#: rewrites it whenever it extends a symbol's statements (the bridge also
#: reads the balance / cash statements, rewritten in the same download)
_FINANCE_SOURCE = "income_quarterly.csv"

#: sessions in "a year earlier" for the prior-year base features
_YEAR_SESSIONS = 252

#: finance-bridge column -> registered feature name
_FINANCE_FIELDS = {
    "return_on_equity": "roe",
    "return_on_assets": "roa",
    "gross_profit_margin": "gross_margin",
    "net_profit_margin": "net_margin",
    "operating_margin": "op_margin",
    "operating_revenue_growth": "rev_growth",
    "net_profit_growth": "profit_growth",
    "operating_revenue": "revenue_ttm",
    "net_profit": "profit_ttm",
    "ocf": "ocf_ttm",
    "capex": "capex_ttm",
    "total_assets": "assets",
    "total_liabilities": "liabilities",
    "total_equity": "equity",
    "total_debt": "interest_debt",
    "cash_and_equivalents": "cash",
}


def _finance_sources(panel: FeaturePanel) -> list:
    """``(symbol, <income_quarterly.csv>)`` for every loaded symbol that
    has lxg statement pages -- the parse work list AND the file census the
    finance fingerprints are built from (memoized on the panel)."""
    if "_finance_sources" not in panel._cache:
        store = panel.store
        found = []
        for symbol in panel.symbols:
            path = store.lxg.find(store.universe.dir_names(symbol), _FINANCE_SOURCE)
            if path is not None:
                found.append((symbol, path))
        panel._cache["_finance_sources"] = found
    return panel._cache["_finance_sources"]


def _finance_payload(panel: FeaturePanel) -> dict:
    """Every symbol's finance frame, parsed once and cached (in memory on
    the panel; on disk as parquet while the store's cache is on).

    Only a statement-table cache MISS reaches the parse at all -- a warm
    run reads the per-field step tables (:func:`_finance_steps`) and never
    comes here.

    The lxg bridge costs ~0.17 s per symbol × 765 symbols with statement
    pages — ~130 s single-threaded.  The disk cache makes the parse a
    once-per-quarter event (invalidated by the newest source file's mtime);
    parsing runs through :func:`map_workers` (processes when safe, threads
    otherwise).

    The payload file is named after the LOADED symbol set (a daily snapshot
    and a validation window load different names): one shared file made the
    two modes evict each other's parse, and a validate re-run paid ~15 s
    to rebuild what the morning's ``run`` had just written.
    """
    payload = panel._cache.get("_finance_payload")
    if payload is not None:
        return payload
    from qf_screen.data._parallel import map_workers
    from qf_screen.data.table_cache import cache_dir_for

    store = panel.store
    use_cache = store.use_cache
    sources = _finance_sources(panel)
    files = [path for _symbol, path in sources]
    cache_path = cache_dir_for(store.data_dir, store.market) / (
        f"finance_payload_{CNDataStore.symbols_key(panel.symbols)}.parquet")
    fingerprint = f"{len(files)}:{max((p.stat().st_mtime for p in files), default=0):.0f}"
    payload = _read_finance_cache(cache_path, fingerprint) if use_cache else None
    if payload is None:
        items = sorted((symbol, store.universe.dir_names(symbol))
                       for symbol, _path in sources)
        results = map_workers(_load_finance_frame, items, workers=8, chunksize=16,
                              initializer=_finance_worker_init,
                              initargs=(str(store.data_dir), store.market))
        payload = {symbol: frame for symbol, frame in results if frame is not None}
        if use_cache:
            _write_finance_cache(cache_path, fingerprint, payload)
        logger.info("finance payload: parsed %d statement symbols%s",
                    len(payload), f" (cache {cache_path})" if use_cache
                    else " (cache bypassed)")
    else:
        logger.info("finance payload: %d statement symbols from cache %s",
                    len(payload), cache_path)
    panel._cache["_finance_payload"] = payload
    return payload


def _read_finance_cache(path, fingerprint: str) -> Optional[dict]:
    """Cached ``{symbol: frame}`` payload from parquet, or ``None`` when
    absent / stale / unreadable."""
    if not path.exists():
        return None
    try:
        import pyarrow  # noqa: F401 - read_parquet needs it; miss if absent
        long = pd.read_parquet(path)
    except Exception:  # noqa: BLE001 - a corrupt cache is a miss, not an error
        return None
    meta_path = path.with_suffix(".meta.txt")
    try:
        cached_fp = meta_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if cached_fp != fingerprint or not len(long):
        return None
    payload: dict = {}
    long["announce_date"] = pd.to_datetime(long["announce_date"])
    for symbol, part in long.groupby("symbol"):
        part = part.copy()
        part["value"] = pd.to_numeric(part["value"], errors="coerce")
        part = part.dropna(subset=["value"])
        # two report periods can share a statutory announce date; aggfunc
        # keeps the later one instead of failing the reshape
        frame = part.pivot_table(index="announce_date", columns="field",
                                 values="value", aggfunc="last").astype(float)
        frame.index = pd.MultiIndex.from_product([[symbol], frame.index],
                                                 names=["symbol", "date"])
        payload[str(symbol)] = frame.sort_index()
    return payload


def _write_finance_cache(path, fingerprint: str, payload: dict) -> None:
    """Persist the payload as long parquet (symbol, field, announce_date,
    value) + a fingerprint sidecar.  Best effort — an unwritable cache only
    costs speed."""
    try:
        import pyarrow  # noqa: F401
        parts = []
        for symbol, frame in payload.items():
            if frame is None or not len(frame):
                continue
            part = frame.reset_index(level=0, drop=True)
            part = part[~part.index.duplicated(keep="last")].reset_index()
            # only NUMERIC fields ride the cache: the bridge also carries
            # report_date (datetime), which would make the melted `value`
            # column non-numeric and explode the read-side cast
            numeric = part.select_dtypes(include=[np.number])
            if numeric.empty:
                continue
            part = part[["date"]].join(numeric)
            part.insert(0, "symbol", symbol)
            parts.append(part.melt(id_vars=["symbol", "date"],
                                   var_name="field", value_name="value")
                         .dropna(subset=["value"]))
        if not parts:
            return
        long = pd.concat(parts, ignore_index=True).rename(columns={"date": "announce_date"})
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        long.to_parquet(tmp, index=False)
        tmp.replace(path)
        path.with_suffix(".meta.txt").write_text(fingerprint, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("finance cache write failed (%s) -- continuing without",
                       exc)


#: worker-side store location (forkserver workers cannot pickle the store;
#: the initializer sets this before any task runs)
_FINANCE_CONTEXT = {"data_dir": "", "market": "cn"}


def _finance_worker_init(data_dir: str, market: str) -> None:
    _FINANCE_CONTEXT["data_dir"] = data_dir
    _FINANCE_CONTEXT["market"] = market


def _load_finance_frame(item):
    """Worker: one symbol's finance frame (or ``None``)."""
    symbol, _dirs = item
    from qf_factor.factory.lxg_finance import lxg_trade_finance_data_adapter

    try:
        org, _quarterly, _ttm = lxg_trade_finance_data_adapter(
            symbol, _FINANCE_CONTEXT["market"], _FINANCE_CONTEXT["data_dir"])
    except Exception:  # noqa: BLE001 - one symbol must not break the field
        return symbol, None
    return symbol, org


def _finance_steps(panel: FeaturePanel, field: str, name: Optional[str] = None
                   ) -> pd.DataFrame:
    """Bridge field ``field`` as a date x symbol step table (PIT by
    announcement date; ``NaN`` for symbols without statement pages).

    Read through the derived-table disk cache like the alt tables.  A warm
    run is ONE parquet read per field; without it every process re-pivoted
    the 764-symbol payload and re-stepped the field (~4 s for the three
    statement fields ``value_dividend`` touches), which handed back most
    of what the store cache saves.  The fingerprint covers the statement
    sources, the calendar bounds, the symbol list and the field.

    ``name`` is the FEATURE the table serves (``roe`` for the bridge's
    ``return_on_equity``) and names the cache file, so ``qf_screen_cache``
    reads like the feature registry.
    """
    from qf_screen.data.table_cache import cached_wide_table, derived_fingerprint

    store = panel.store
    files = [path for _symbol, path in _finance_sources(panel)]
    fp = derived_fingerprint(files, panel.calendar, panel.symbols, extra=field)
    return cached_wide_table(store.table_cache_dir,
                             f"finance_step_{name or field}", fp,
                             lambda: _build_finance_steps(panel, field),
                             use_cache=store.use_cache)


def _build_finance_steps(panel: FeaturePanel, field: str) -> pd.DataFrame:
    """The uncached build behind :func:`_finance_steps`: every statement
    symbol's field stepped onto the calendar, assembled into one numpy
    matrix (no per-column DataFrame insertion)."""
    calendar, symbols = panel.calendar, panel.symbols
    out = np.full((len(calendar), len(symbols)), np.nan)
    where = {s: i for i, s in enumerate(symbols)}
    filled = 0
    for symbol, frame in _finance_payload(panel).items():
        if frame is None or field not in frame.columns or symbol not in where:
            continue
        series = frame[field].droplevel(0)
        series = pd.to_numeric(series, errors="coerce")
        series = series[series.index.notna()].groupby(level=0).last().sort_index()
        series = series[series.index <= calendar[-1]]
        if series.empty:
            continue
        out[:, where[symbol]] = series.reindex(calendar, method="ffill") \
            .to_numpy(dtype=float)
        filled += 1
    if filled == 0:
        logger.debug("finance field %s: no symbol has data", field)
    return pd.DataFrame(out, index=calendar, columns=symbols)


def _div_yield_table(panel: FeaturePanel) -> pd.DataFrame:
    """Trailing-365-day cash dividend per share / unadjusted close.

    Only ``implemented`` events with an ex-date count (a
    ``board_director_plan`` pays nothing); the sum is taken over ex-dates
    strictly inside the trailing year ending at each row — as-traded, so
    the engine's one-day shift keeps it causal in schedules.  Names whose
    lxg dividend file the event builder cannot serve (no file, no usable
    event) fall back to the layout-B ``dyr`` column — lxg's own trailing
    yield, the same quantity measured by the provider.

    The 1-D ``days`` in the two ``searchsorted`` calls is load-bearing: a
    ``(N, 1)`` needle makes the cumulative sums broadcast against the
    per-symbol price and silently divides every row by the FIRST row's
    price (the v0.1 bug this replaces).
    """
    from qf_screen.data.merged_store import prepare_dividend_events
    from qf_screen.data.table_cache import cached_wide_table, derived_fingerprint

    calendar, symbols = panel.calendar, panel.symbols
    store = panel.store
    have = store.symbols_with_file("lxg", "dividend.csv")
    files = [store.lxg.find(store.universe.dir_names(s), "dividend.csv")
             for s in have]
    fp = derived_fingerprint(files, calendar, symbols)

    def build() -> pd.DataFrame:
        values = np.full((len(calendar), len(symbols)), np.nan)
        where = {s: i for i, s in enumerate(symbols)}
        close = store.wide("close_raw").astype(float)
        days = calendar.to_numpy().astype("datetime64[D]")
        for symbol in have:                      # skip the ~3,000 bare dirs
            raw = store.dividend_events(symbol)
            events = prepare_dividend_events(raw) if raw is not None else None
            if events is None or events.empty or symbol not in where:
                continue
            ex = events["ex_date"].to_numpy().astype("datetime64[D]")
            cash = events["cash"].to_numpy(dtype=float)
            order = np.argsort(ex)
            ex, cash = ex[order], cash[order]
            keep = cash > 0
            ex, cash = ex[keep], cash[keep]
            if ex.size == 0:
                continue
            cumulative = np.concatenate([[0.0], np.cumsum(cash)])
            hi = np.searchsorted(ex, days, side="right")
            lo = np.searchsorted(ex, days - np.timedelta64(365, "D"), side="right")
            trailing = cumulative[hi] - cumulative[lo]
            price = close[symbol].to_numpy(dtype=float)
            with np.errstate(invalid="ignore", divide="ignore"):
                values[:, where[symbol]] = np.where(price > 0, trailing / price, np.nan)
        return pd.DataFrame(values, index=calendar, columns=symbols)

    events_yield = cached_wide_table(
        store.table_cache_dir, "div_yield_ttm_events", fp,
        build, use_cache=store.use_cache)
    dyr = store.wide("dyr").astype(float)
    if events_yield.size and dyr.notna().any().any():
        return _coalesce(events_yield, dyr)
    return events_yield


def register_fundamental_features() -> None:
    for column, name in _FINANCE_FIELDS.items():
        register_feature(
            name, (lambda p, c=column, n=name: _finance_steps(p, c, n)),
            f"lxg TTM statement field {column!r}, PIT by statutory announce "
            "date (NaN without lxg statements)", kind="table", replace=True)
    register_feature("fcf_ttm", lambda p: _safe_sub(p.table("ocf_ttm"),
                                                    p.table("capex_ttm")),
                     "free cash flow = ocf_ttm - capex_ttm", kind="table")
    register_feature("debt_assets", lambda p: _ratio(p, "liabilities", "assets"),
                     "total liabilities / total assets", kind="table")
    register_feature("cash_assets", lambda p: _ratio(p, "cash", "assets"),
                     "cash & equivalents / total assets", kind="table")
    register_feature("fcf_yield", lambda p: _ratio(p, "fcf_ttm", "market_cap"),
                     "free cash flow / market cap", kind="table")
    register_feature("div_yield_ttm", _div_yield_table,
                     "trailing-365d cash dividends per share / unadjusted close "
                     "(lxg dyr fallback where no dividend file serves)",
                     kind="table")
    register_feature("dyr", lambda p: p.store.wide("dyr").astype(float),
                     "lxg layout-B trailing dividend yield (fraction; the "
                     "div_yield_ttm fallback source)", kind="table")
    # ---- full-market coverage features -------------------------------
    def roe_implied(panel: FeaturePanel) -> pd.DataFrame:
        """pb / pe_ttm == E/B: the ROE a valuation pair implies.  Both legs
        come from the merged wide tables, so coverage is the FULL market
        (~5,200 names) rather than the ~765 statement symbols; loss-makers
        (pe < 0) and missing legs stay NaN — a negative implied ROE must
        not pass a quality filter."""
        pb = panel.store.wide("pb").astype(float)
        pe = panel.store.wide("pe_ttm").astype(float)
        return _safe_div(pb.where(pb > 0), pe.where(pe > 0))

    register_feature("roe_implied", roe_implied,
                     "implied ROE = pb / pe_ttm (full-market coverage; "
                     "loss-makers stay NaN)", kind="table")
    register_feature(
        "roe_best",
        lambda p: _coalesce(p.table("roe"), p.table("roe_implied")),
        "statement roe with the full-market roe_implied fallback (best "
        "available ROE per name)", kind="table")
    register_feature(
        "net_margin_py",
        lambda p: _safe_div(p.table("profit_ttm").shift(_YEAR_SESSIONS),
                            p.table("revenue_ttm").shift(_YEAR_SESSIONS)),
        "net margin of the TTM report that was known a year (252 sessions) "
        "earlier -- the base the current growth is measured against; a thin "
        "prior-year margin flags growth off a near-zero base (NaN without "
        "lxg statements)", kind="table")
    # readable-unit companions (CLI formatting is nicer on these)
    register_feature("market_cap_bn",
                     lambda p: _safe_div(p.store.wide("market_cap").astype(float),
                                         1e8),
                     "total market cap (亿元 = 1e8 CNY)", kind="table")
    register_feature("float_market_cap_bn",
                     lambda p: _safe_div(p.store.wide("float_market_cap").astype(float),
                                         1e8),
                     "float market cap (亿元 = 1e8 CNY)", kind="table")
    register_feature("amount_ma20_mn",
                     lambda p: _safe_div(p.table("amount_ma20"), 1e6),
                     "trailing 20d mean traded value (百万元 = 1e6 CNY)",
                     kind="table")


def _ratio(panel: FeaturePanel, num: str, den: str) -> pd.DataFrame:
    return _safe_div(panel.table(num), panel.table(den))


# ------------------------------------------------------------ filters
def pe_range(lo: float = 0.0, hi: float = 15.0) -> Filter:
    """Positive P/E below ``hi`` (loss-makers stay out: their P/E is NaN)."""
    return ExprFilter("pe", f"pe_ttm > {lo} and pe_ttm < {hi}",
                      f"{lo} < pe_ttm < {hi}")


def pb_under(hi: float = 1.5) -> Filter:
    return ExprFilter("pb", f"pb > 0 and pb < {hi}", f"0 < pb < {hi}")


def roe_min(min_roe: float = 0.12) -> Filter:
    return ExprFilter("roe", f"roe > {min_roe}", f"TTM ROE > {min_roe:.0%}")


def growth_min(min_growth: float = 0.10) -> Filter:
    return ExprFilter("growth", f"rev_growth > {min_growth} "
                      f"and profit_growth > {min_growth}",
                      f"TTM revenue and profit growth > {min_growth:.0%}")


def debt_max(max_debt: float = 0.6) -> Filter:
    return ExprFilter("debt", f"debt_assets < {max_debt}",
                      f"liabilities / assets < {max_debt:.0%}")


def div_yield_min(min_yield: float = 0.03) -> Filter:
    return ExprFilter("div_yield", f"div_yield_ttm >= {min_yield}",
                      f"trailing dividend yield >= {min_yield:.0%}")


def fcf_yield_min(min_yield: float = 0.05) -> Filter:
    return ExprFilter("fcf", f"fcf_yield >= {min_yield}",
                      f"trailing FCF / market cap >= {min_yield:.0%}")


register_fundamental_features()
