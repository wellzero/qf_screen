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
"""
from __future__ import annotations

__version__ = "0.3.0"

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
    "ExprRule": "qf_screen.rules",
    "compile_expr": "qf_screen.rules",
    "DslError": "qf_screen.rules",
    "FEATURES": "qf_screen.rules",
    "load_preset": "qf_screen.presets",
    "list_presets": "qf_screen.presets",
    "PRESET_DIR": "qf_screen.presets",
}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module), name)


__all__ = list(_LAZY)
