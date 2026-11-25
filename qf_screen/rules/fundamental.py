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
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from .base import FeaturePanel, Filter, register_feature
from .hygiene import ExprFilter

logger = logging.getLogger(__name__)

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


def _finance_payload(panel: FeaturePanel) -> dict:
    """Every symbol's finance frame, parsed once and cached (in memory on
    the panel AND on disk as parquet).

    The lxg bridge costs ~0.17 s per symbol × 765 symbols with statement
    pages — ~130 s single-threaded, which a screen touching two finance
    fields would otherwise pay twice per process.  The disk cache makes
    the parse a once-per-quarter event (invalidated by the newest source
    file's mtime); parsing runs through :func:`map_workers` (processes
    when safe, threads otherwise).
    """
    payload = panel._cache.get("_finance_payload")
    if payload is not None:
        return payload
    from qf_screen.data._parallel import map_workers

    store = panel.store
    cache_dir = store.data_dir / store.market / "qf_screen_cache"
    cache_path = cache_dir / "finance_payload.parquet"
    dirs = {s: store.universe.dir_names(s) for s in panel.symbols}
    sources = [store.lxg.find(d, "income_quarterly.csv")
               for d in dirs.values()]
    sources = [p for p in sources if p is not None]
    fingerprint = f"{len(sources)}:{max((p.stat().st_mtime for p in sources), default=0):.0f}"
    payload = _read_finance_cache(cache_path, fingerprint)
    if payload is None:
        items = sorted((s, d) for s, d in dirs.items()
                       if store.lxg.find(d, "income_quarterly.csv") is not None)
        results = map_workers(_load_finance_frame, items, workers=8, chunksize=16,
                              initializer=_finance_worker_init,
                              initargs=(str(store.data_dir), store.market))
        payload = {symbol: frame for symbol, frame in results if frame is not None}
        _write_finance_cache(cache_path, fingerprint, payload)
        logger.info("finance payload: parsed %d statement symbols (cache %s)",
                    len(payload), cache_path)
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


def _finance_steps(panel: FeaturePanel, field: str) -> pd.DataFrame:
    """One finance-bridge field as a date x symbol step table (PIT by
    announcement date; ``NaN`` for symbols without statement pages)."""
    calendar, symbols = panel.calendar, panel.symbols
    out = pd.DataFrame(np.nan, index=calendar, columns=symbols, dtype=float)
    filled = 0
    for symbol, frame in _finance_payload(panel).items():
        if frame is None or field not in frame.columns:
            continue
        series = frame[field].droplevel(0)
        series = pd.to_numeric(series, errors="coerce")
        series = series[series.index.notna()].groupby(level=0).last().sort_index()
        series = series[series.index <= calendar[-1]]
        if series.empty:
            continue
        out[symbol] = series.reindex(calendar, method="ffill")
        filled += 1
    if filled == 0:
        logger.debug("finance field %s: no symbol has data", field)
    return out


def _div_yield_table(panel: FeaturePanel) -> pd.DataFrame:
    """Trailing-365-day cash dividend per share / unadjusted close.

    Only ``implemented`` events with an ex-date count (a
    ``board_director_plan`` pays nothing); the sum is taken over ex-dates
    strictly inside the trailing year ending at each row — as-traded, so
    the engine's one-day shift keeps it causal in schedules.
    """
    from qf_screen.data.merged_store import prepare_dividend_events

    calendar, symbols = panel.calendar, panel.symbols
    out = pd.DataFrame(np.nan, index=calendar, columns=symbols, dtype=float)
    close = panel.store.wide("close_raw").astype(float)
    for symbol in symbols:
        raw = panel.store.dividend_events(symbol)
        events = prepare_dividend_events(raw) if raw is not None else None
        if events is None or events.empty:
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
        days = calendar.to_numpy().astype("datetime64[D]")
        hi = np.searchsorted(ex, days[:, None], side="right")
        lo = np.searchsorted(ex, (days[:, None] - np.timedelta64(365, "D")),
                             side="right")
        trailing = cumulative[hi] - cumulative[lo]
        price = close[symbol].to_numpy(dtype=float)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[symbol] = np.where(price > 0, trailing / price, np.nan)
    return out


def register_fundamental_features() -> None:
    for column, name in _FINANCE_FIELDS.items():
        register_feature(
            name, (lambda p, c=column: _finance_steps(p, c)),
            f"lxg TTM statement field {column!r}, PIT by statutory announce "
            "date (NaN without lxg statements)", kind="table", replace=True)
    register_feature("fcf_ttm", lambda p: p.table("ocf_ttm") - p.table("capex_ttm"),
                     "free cash flow = ocf_ttm - capex_ttm", kind="table")
    register_feature("debt_assets", lambda p: _ratio(p, "liabilities", "assets"),
                     "total liabilities / total assets", kind="table")
    register_feature("cash_assets", lambda p: _ratio(p, "cash", "assets"),
                     "cash & equivalents / total assets", kind="table")
    register_feature("fcf_yield", lambda p: _ratio(p, "fcf_ttm", "market_cap"),
                     "free cash flow / market cap", kind="table")
    register_feature("div_yield_ttm", _div_yield_table,
                     "trailing-365d cash dividends per share / unadjusted close",
                     kind="table")


def _ratio(panel: FeaturePanel, num: str, den: str) -> pd.DataFrame:
    with np.errstate(invalid="ignore", divide="ignore"):
        return panel.table(num) / panel.table(den)


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
