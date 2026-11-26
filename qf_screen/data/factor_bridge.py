"""Precomputed qf_factor / qf_mine factor CSV bridge.

The qf_factor calc scripts store one CSV per symbol under
``<QUANT_DATA_DIR>/<market>/equity/<DIR>/<provider>/<Family>.csv`` (and,
for some families, directly at ``<DIR>/<Family>.csv``), with ``symbol`` +
``date`` leading columns and one column per factor:
``lxg/Alpha101.csv`` by default; ``xq/finance_Q_factor.csv``,
``xtq/Autodsl.csv`` ... only when ``QF_SCREEN_FACTOR_PROVIDERS`` names them.

This bridge turns any ``(<family>, <column>)`` pair into a screenable WIDE
``date x symbol`` feature, on first mention, through the same
fingerprinted disk cache the alt features use:

* in the DSL: ``rank(factor("Alpha101", "alpha001"))`` or
  ``factor_Alpha101__alpha001 > 0.1``;
* search order per symbol: the ``QF_SCREEN_FACTOR_PROVIDERS`` directories
  first (default ``lxg,ak`` -- the screener is an lxg + akshare product
  and reads no other provider tree unless you opt in), the legacy bare
  ``<DIR>/<family>.csv`` LAST -- it only fills the gaps (see
  :func:`_find_factor_file`);
* values are POINT-IN-TIME steps: the factor file's own dates reindexed
  onto the panel calendar with forward-fill — a quarterly factor holds
  until its next row, exactly like the statement features;
* coverage honesty: a symbol without the file (or the column) is NaN and
  fails a filter conservatively, like every other feature here.
"""
from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from ._io import ColumnSpec, read_bytes, read_csv_typed

logger = logging.getLogger(__name__)

#: provider directories searched (first hit wins per symbol).  The
#: screener reads ONLY the lxg and akshare trees (``lxg`` first), so the
#: bridge does too by default; a factor family that some other provider
#: directory carries (``xq`` / Xueqiu, ``xtq`` ...) is opt-in:
#: ``QF_SCREEN_FACTOR_PROVIDERS=xq,lxg``.
DEFAULT_PROVIDERS: Tuple[str, ...] = ("lxg", "ak")

#: a family that reaches less than this share of the loaded symbols under
#: the configured providers is reported (it usually lives in a provider
#: directory the screener does not read by default)
_THIN_COVERAGE = 0.05


def providers() -> Tuple[str, ...]:
    raw = os.environ.get("QF_SCREEN_FACTOR_PROVIDERS")
    if not raw:
        return DEFAULT_PROVIDERS
    return tuple(p.strip() for p in raw.split(",") if p.strip()) or DEFAULT_PROVIDERS


def _find_factor_file(root: Path, dirs: Tuple[str, ...], family: str,
                      wanted: Tuple[str, ...]) -> Optional[Path]:
    """First existing factor CSV for a symbol.

    Provider files (``xq``, ``lxg``, ``xtq`` ... in the configured order)
    win; the BARE ``<DIR>/<family>.csv`` is the legacy no-provider layout
    (often years stale — e.g. ``finance_Q_factor.csv`` at the dir level
    ends 2025-03 with percent-scaled values while ``xq/`` carries 2026-04
    fractions) and only fills the gaps.  Same precedence as
    :mod:`qf_factor.framework.evaluation.loader` applies.
    """
    for name in dirs:
        for provider in wanted:
            path = root / name / provider / f"{family}.csv"
            if path.exists():
                return path
    for name in dirs:
        bare = root / name / f"{family}.csv"
        if bare.exists():
            return bare
    return None


def _family_elsewhere(root: Path, dirs, family: str, wanted: Tuple[str, ...]) -> str:
    """A hint for the all-NaN warning: which OTHER provider directory
    carries this family (sampled over the first few hundred symbols)."""
    for _symbol, names in list(dirs.items())[:400]:
        for name in names:
            try:
                providers = os.listdir(root / name)
            except OSError:
                continue
            found = sorted(p for p in providers if p not in wanted
                           and (root / name / p / f"{family}.csv").exists())
            if found:
                return (f"; {family}.csv also sits under {', '.join(found)} -- opt in "
                        "with QF_SCREEN_FACTOR_PROVIDERS="
                        f"{','.join(found)},{','.join(wanted)}")
    return ""


def _load_factor_column(item):
    """One symbol's (date, value) arrays from a factor CSV (or None)."""
    symbol, path, column = item
    specs = (ColumnSpec("date", "date"), ColumnSpec(column, "float"))
    arrays = read_csv_typed(read_bytes(path, tail_rows=None), specs)
    if "date" not in arrays or column not in arrays:
        return symbol, None
    dates = arrays["date"]
    values = np.asarray(arrays[column], dtype=float)
    ok = ~pd.isna(dates)
    if not ok.any():
        return symbol, None
    dates, values = dates[ok], values[ok]
    series = pd.Series(values, index=pd.DatetimeIndex(pd.to_datetime(dates)))
    series = series[series.index.notna()].groupby(level=0).last().sort_index()
    return symbol, (series if len(series) else None)


def factor_feature_table(panel, family: str, column: str) -> pd.DataFrame:
    """Wide ``date x symbol`` table of one precomputed factor column.

    Built once per (family, column) and cached to parquet under
    ``qf_screen_cache/``; the fingerprint covers the census (count + max
    mtime) of every source file the table reads plus the calendar bounds,
    so a re-run of the qf_factor calc scripts invalidates it.
    """
    from qf_screen.data.table_cache import cached_wide_table, fingerprint

    store = panel.store
    calendar, symbols = panel.calendar, panel.symbols
    root = store.lxg.root                      # == equity root for all providers
    wanted = providers()
    dirs = store.universe.dirs
    paths: List[Optional[Path]] = [
        _find_factor_file(root, dirs.get(s, ()), family, wanted) for s in symbols]
    items = [(s, p, column) for s, p in zip(symbols, paths) if p is not None]
    files = [p for p in paths if p is not None]
    hint = _family_elsewhere(root, dirs, family, wanted)
    if not files:
        logger.warning(
            "factor bridge: no symbol carries %s.csv under the providers %s -- "
            "feature factor_%s__%s is all-NaN%s", family, ",".join(wanted), family,
            column, hint)
    elif len(files) < _THIN_COVERAGE * len(symbols):
        logger.warning(
            "factor bridge: %s.csv serves only %d of %d symbols under the "
            "providers %s -- feature factor_%s__%s is NaN for everyone else%s",
            family, len(files), len(symbols), ",".join(wanted), family, column, hint)
    fp = fingerprint(files, extra=f"{column}:{len(calendar)}:{calendar[0].date()}:"
                                  f"{calendar[-1].date()}:{','.join(wanted)}:"
                                  + hashlib.sha1("|".join(symbols).encode())
                                  .hexdigest()[:10])

    def build() -> pd.DataFrame:
        values = np.full((len(calendar), len(symbols)), np.nan)
        where = {s: i for i, s in enumerate(symbols)}
        with ThreadPoolExecutor(max_workers=16) as pool:
            for symbol, series in pool.map(_load_factor_column, items):
                if series is None or symbol not in where:
                    continue
                series = series[series.index <= calendar[-1]]
                if series.empty:
                    continue
                values[:, where[symbol]] = series.reindex(
                    calendar, method="ffill").to_numpy(dtype=float)
        covered = int(np.isfinite(values).any(axis=0).sum())
        if items:
            logger.info("factor bridge: %s.%s -> %d/%d symbols", family, column,
                        covered, len(symbols))
        return pd.DataFrame(values, index=calendar, columns=symbols)

    return cached_wide_table(store.table_cache_dir,
                             f"factor_{family}__{column}", fp, build,
                             use_cache=store.use_cache)
