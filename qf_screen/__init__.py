"""qf_screen -- Chinese A-share stock screener.

Standalone, local-first screener over the ``QUANT_DATA_DIR`` store (lxg +
ak providers) with two modes that share ONE definition of a screen:

* **snapshot** (:meth:`ScreenEngine.run`) -- "which names pass today?", with
  a per-rule funnel and per-symbol explanations;
* **causal wide panel** (:meth:`ScreenEngine.schedule`) -- the same screen
  as a long ``date,symbol`` point-in-time schedule that loads straight into
  the ``qf_factor`` backtest / live plumbing, plus historical
  forward-return validation.

The public names are exported lazily so ``import qf_screen`` stays cheap::

    from qf_screen import CNDataStore, ScreenEngine, ScreenSpec
    store = CNDataStore()                       # last 600 sessions
    engine = ScreenEngine(store)
    result = engine.run(ScreenSpec.from_yaml("value_dividend"))
    print(result.members)

    watch(engine, ["SH600519"], preset="value_dividend")     # risk alerts for a book
    report = engine.validate(ScreenSpec.from_yaml("momentum_trend"), "2026-01-05",
                             "2026-09-30", cost_bps=15, skip_limit_up=True)
    print(report.stats["portfolio"], report.stats["portfolio_net"])
"""
from __future__ import annotations

__version__ = "0.4.0"

# --------------------------------------------------------------- numexpr
# CRITICAL (v0.2): pandas delegates wide DataFrame binary ops (+ - * /) to
# numexpr when the frames hold > ~10k cells, and this environment's
# numexpr 2.10.2 / numpy 2.4 combination SILENTLY CORRUPTS float32 arrays
# that contain NaN -- measured 1.38 M of 3.4 M cells wrong in a 630 x 5472
# division (ma_ratio_200 came back 0.0 for thousands of names, leaving 26
# of 5,472 passing the trend filter).  Disabling numexpr routes every op
# through plain numpy, which is exact; the measured cost on store-scale
# frames is negligible next to the I/O.  The wide-arithmetic helpers in
# :mod:`qf_screen.rules.base` additionally bypass pandas arithmetic
# outright, so a host that re-enables numexpr stays correct too.
try:  # pragma: no cover - exercised implicitly by every import
    import pandas as _pd

    if _pd.options.compute.use_numexpr:
        _pd.options.compute.use_numexpr = False
except Exception:  # noqa: BLE001 - pandas always present; belt and braces
    pass

#: lazy exports: name -> submodule (imported on first attribute access)
_LAZY = {
    "CNDataStore": "qf_screen.data",
    "build_adjusted_close": "qf_screen.data",
    "prepare_dividend_events": "qf_screen.data",
    "Universe": "qf_screen.data",
    "build_universe": "qf_screen.data",
    "LiveAkClient": "qf_screen.data",
    "LiveDataError": "qf_screen.data",
    "ScreenEngine": "qf_screen.engine",
    "ScreenSpec": "qf_screen.engine",
    "ScreenResult": "qf_screen.engine",
    "UniverseSpec": "qf_screen.engine",
    "FeaturePanel": "qf_screen.engine",
    "ValidationReport": "qf_screen.engine",
    "Filter": "qf_screen.rules",
    "Ranker": "qf_screen.rules",
    "compile_expr": "qf_screen.rules",
    "DslError": "qf_screen.rules",
    "FEATURES": "qf_screen.rules",
    "load_preset": "qf_screen.presets",
    "list_presets": "qf_screen.presets",
    "PRESET_DIR": "qf_screen.presets",
    "user_preset_dir": "qf_screen.presets",
    "preset_origin": "qf_screen.presets",
    "init_preset": "qf_screen.presets",
    # the portfolio arithmetic behind validate / compare
    "simulate": "qf_screen.analytics",
    "limit_up_mask": "qf_screen.analytics",
    "perf": "qf_screen.analytics",
    "spread_stats": "qf_screen.analytics",
    # the watchlist risk monitor
    "watch": "qf_screen.ui.watch",
    "WatchReport": "qf_screen.ui.watch",
    "WatchRule": "qf_screen.ui.watch",
    "Watchlist": "qf_screen.ui.watch",
    "load_watchlist": "qf_screen.ui.watch",
    # the snapshot archive
    "save_snapshot": "qf_screen.history",
    "load_history": "qf_screen.history",
    "history_view": "qf_screen.history",
    "history_dir": "qf_screen.history",
    # webhook delivery
    "send": "qf_screen.notify",
    "Delivery": "qf_screen.notify",
    "WebhookError": "qf_screen.notify",
    # the self-contained HTML pages
    "daily_report_html": "qf_screen.ui.html_report",
    "validate_html": "qf_screen.ui.html_report",
    "compare_html": "qf_screen.ui.html_report",
    # the cache
    "cache_status": "qf_screen.cache_ops",
    "cache_warm": "qf_screen.cache_ops",
    "cache_prune": "qf_screen.cache_ops",
    # the stock & section catalogs
    "SymbolStore": "qf_screen.symbols",
    "SymbolError": "qf_screen.symbols",
    "SyncResult": "qf_screen.symbols",
    "sync_symbols": "qf_screen.symbols",
}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module), name)


__all__ = list(_LAZY)
