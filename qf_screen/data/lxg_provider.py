"""Local lxg (理杏仁) readers.

Layout::

    cn/lxg/company_list.csv
    cn/equity/<SYM>/lxg/{daily,valuation,dividend,margin,...}.csv

Two ``daily.csv`` layouts coexist in the store and BOTH are read here:

* **A** (2,042 symbols): ``date,open,high,low,close,close_raw,volume,amount,
  change,turnoverrate`` with the valuation series in a separate
  ``valuation.csv``;
* **B** (407 symbols): ``date,close,close_raw,volume,amount,market_capital,
  float_market_capital,pe_ttm,pb,ps_ttm,pcf_ttm,dyr,turnoverrate,
  total_shares,float_shares`` -- no OHLC, valuation embedded, no
  ``valuation.csv``.

Every reader is column-tolerant: the per-symbol files were written by
different vintages of the downloader, so column order varies and whole
columns are absent in a minority of files (25 ``dividend.csv`` have no
``exDate``).  A requested column that is not there simply comes back
missing.

.. warning::
   lxg's ``close`` is documented as 前复权 but is NOT a dependable adjusted
   series: 1,740 of 2,446 symbols carry ``close == close_raw`` on every row
   and 1,312 of 1,802 bonus-share events are left unadjusted (measured
   2026-11).  :class:`~.merged_store.CNDataStore` therefore treats only
   ``close_raw`` as price and rebuilds the adjusted series itself.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Optional, Sequence

import numpy as np
import pandas as pd

from quant_free.common.const_filename import EQUITY_DIR, PROVIDER_LXG, get_data_dir

from ._io import (ColumnSpec, ColumnTable, _header_names, read_bytes, read_csv,
                  read_csv_typed)

logger = logging.getLogger(__name__)

#: per-symbol files this provider reads (``<name>.csv``)
LXG_FILES = ("daily", "valuation", "company", "industries", "indices", "dividend",
             "margin", "mutual_market", "volatility", "shareholders_num", "pledge",
             "block_deal", "exec_shares_change", "major_sh_change")

_DATE = ColumnSpec("date", "date")

#: layout-A OHLC + traded value, then layout-B close + embedded valuation
_DAILY_SPECS: Sequence[ColumnSpec] = (
    _DATE,
    ColumnSpec("open"), ColumnSpec("high"), ColumnSpec("low"), ColumnSpec("close"),
    ColumnSpec("close_raw"), ColumnSpec("volume"), ColumnSpec("amount"),
    ColumnSpec("turnoverrate"),
    ColumnSpec("market_capital"), ColumnSpec("float_market_capital"),
    ColumnSpec("pe_ttm"), ColumnSpec("pb"), ColumnSpec("ps_ttm"), ColumnSpec("pcf_ttm"),
    ColumnSpec("dyr"), ColumnSpec("total_shares"), ColumnSpec("float_shares"),
)
#: valuation columns a layout-B ``daily`` carries
_EMBEDDED_VALUATION = ("market_capital", "float_market_capital", "pe_ttm", "pb",
                       "ps_ttm", "pcf_ttm", "total_shares", "float_shares")

_VALUATION_SPECS: Sequence[ColumnSpec] = (
    _DATE, ColumnSpec("pe_ttm"), ColumnSpec("pb"), ColumnSpec("ps_ttm"),
    ColumnSpec("pcf_ttm"), ColumnSpec("mc", "float", "market_capital"),
)

#: dividend.csv -> canonical event columns
_DIVIDEND_SPECS: Sequence[ColumnSpec] = (
    ColumnSpec("date", "date", "announce_date"),
    ColumnSpec("fsEndDate", "date", "fs_end_date"),
    ColumnSpec("exDate", "date", "ex_date"),
    ColumnSpec("dividend", "float", "cash_per_share"),
    ColumnSpec("bonusSharesFromProfit", "float", "bonus_profit"),
    ColumnSpec("bonusSharesFromCapitalReserve", "float", "bonus_reserve"),
    ColumnSpec("dividendAmount", "float", "cash_total"),
    ColumnSpec("annualNetProfitDividendRatio", "float", "payout_ratio"),
    ColumnSpec("status", "str"), ColumnSpec("currency", "str"),
)
_DIVIDEND_DATES = ("announce_date", "ex_date", "fs_end_date")
_DIVIDEND_NUMBERS = ("cash_per_share", "bonus_profit", "bonus_reserve", "cash_total",
                     "payout_ratio")
_DIVIDEND_TEXT = ("status", "currency")

#: columns of the alt files that are free text (everything else parses as a
#: number); used when a caller does not name the columns it wants
_TEXT_COLUMNS = frozenset({
    "areaCode", "source", "name", "buyBranch", "sellBranch", "pledgor", "pledgee",
    "pledgeMatters", "shareholderName", "executiveName", "duty", "changeReason",
    "stockType", "relationBetweenES", "pledgeSharesNature", "special",
    "isPledgeRepurchaseTransactions", "pledgeDischargeExplanation",
})


class LxgProvider:
    """Readers over ``<data_dir>/<market>/equity/<DIR>/lxg/``."""

    provider = PROVIDER_LXG

    def __init__(self, data_dir=None, market: str = "cn"):
        self.data_dir = Path(data_dir or get_data_dir())
        self.market = market
        self.root = self.data_dir / market / EQUITY_DIR
        self.list_path = self.data_dir / market / PROVIDER_LXG / "company_list.csv"
        #: ``<root>/`` as a string prefix: :meth:`find` is called thousands
        #: of times per run (the file censuses behind every cache), and
        #: ``os.path.exists`` on a joined string costs a third of pathlib
        self._base = f"{self.root}{os.sep}"

    # ------------------------------------------------------------ paths
    def find(self, dirs: Sequence[str], filename: str) -> Optional[Path]:
        """First existing ``<dir>/lxg/<filename>`` over a symbol's aliases."""
        base = self._base
        for name in dirs:
            path = f"{base}{name}{os.sep}{PROVIDER_LXG}{os.sep}{filename}"
            if os.path.exists(path):
                return Path(path)
        return None

    def read(self, dirs: Sequence[str], name: str, **kw) -> Optional[pd.DataFrame]:
        """Raw (unnormalized) ``<name>.csv`` of a symbol, or ``None``."""
        return read_csv(self.find(dirs, f"{name}.csv"), **kw)

    # ------------------------------------------------------------ daily
    def daily(self, dirs: Sequence[str], start=None, end=None,
              tail_rows: Optional[int] = None) -> Optional[ColumnTable]:
        """Per-symbol ``daily.csv`` (either layout) as a dated table.

        ``tail_rows`` reads only the last rows of the file (snapshot path);
        ``start`` / ``end`` clip afterwards.  Columns present in the file
        come back under their store names (see the module docstring).
        """
        data = read_bytes(self.find(dirs, "daily.csv"), tail_rows)
        table = ColumnTable.from_typed(read_csv_typed(data, _DAILY_SPECS), _DAILY_SPECS)
        return table.clip(start, end) if table is not None else None

    def valuation(self, dirs: Sequence[str], start=None, end=None,
                  tail_rows: Optional[int] = None) -> Optional[ColumnTable]:
        """Layout-A ``valuation.csv``: ``date,pe_ttm,pb,ps_ttm,pcf_ttm,market_capital``.
        ``None`` for layout-B symbols (valuation lives in ``daily``)."""
        data = read_bytes(self.find(dirs, "valuation.csv"), tail_rows)
        table = ColumnTable.from_typed(read_csv_typed(data, _VALUATION_SPECS),
                                       _VALUATION_SPECS)
        return table.clip(start, end) if table is not None else None

    @staticmethod
    def valuation_from_daily(daily: ColumnTable) -> ColumnTable:
        """Valuation columns of a layout-B ``daily`` (empty when layout A)."""
        cols = {name: daily.cols[name] for name in _EMBEDDED_VALUATION if name in daily.cols}
        return ColumnTable(daily.days, cols)

    # ---------------------------------------------------------- dividend
    def dividend_events(self, dirs: Sequence[str]) -> Optional[pd.DataFrame]:
        """Normalized dividend records (ALL statuses) sorted by ex-date.
        ``None`` when there is no file.

        Canonical columns: ``announce_date``, ``ex_date``, ``fs_end_date``,
        ``cash_per_share`` (税前, per pre-event share, CNY), ``bonus_profit``
        + ``bonus_reserve`` (bonus shares per pre-event share), ``cash_total``
        (whole distribution, CNY), ``payout_ratio``, ``status``, ``currency``.
        A column the file does not have comes back all-NaN / all-``""``.
        """
        arrays = read_csv_typed(read_bytes(self.find(dirs, "dividend.csv")),
                                _DIVIDEND_SPECS)
        if not arrays:
            return None
        n = len(arrays["announce_date"])
        data: Dict[str, np.ndarray] = {}
        for name in _DIVIDEND_DATES:
            missing = np.full(n, np.datetime64("NaT", "ns"))
            data[name] = np.asarray(arrays.get(name, missing)).astype("datetime64[ns]")
        for name in _DIVIDEND_NUMBERS:
            data[name] = np.asarray(arrays.get(name, np.full(n, np.nan)), dtype=float)
        for name in _DIVIDEND_TEXT:
            data[name] = np.asarray(arrays.get(name, np.full(n, "", dtype=object)), dtype=object)
        out = pd.DataFrame(data)
        # exact duplicates come from re-downloads; two DIFFERENT events
        # sharing an announcement date must both survive, so no dedup on it
        out = out.drop_duplicates()
        ex = out["ex_date"].to_numpy()
        order = np.argsort(np.where(pd.isna(ex), np.datetime64("2262-01-01"), ex),
                           kind="stable")
        return out.iloc[order].reset_index(drop=True)

    # --------------------------------------------------------------- alt
    def alt(self, dirs: Sequence[str], kind: str,
            columns: Optional[Sequence[str]] = None) -> Optional[pd.DataFrame]:
        """A per-symbol alternative-data file as a frame with a parsed
        ``date`` column (tz-naive, normalized), sorted by date.

        ``kind`` is a name in :data:`LXG_FILES` (other than the three with
        dedicated readers).  ``columns`` names the columns to return -- pass
        it: only those are parsed, which matters on the 500 KB ``margin``
        files.  Numeric columns come back as float, known text columns as
        ``str``.  ``None`` when the file is absent or empty.
        """
        if kind not in LXG_FILES or kind in ("daily", "valuation", "dividend"):
            raise KeyError(f"unknown lxg file kind {kind!r}; known: {LXG_FILES[3:]}")
        data = read_bytes(self.find(dirs, f"{kind}.csv"))
        if not data:
            return None
        header = data.partition(b"\n")[0]
        names = [c for c in _header_names(header) if c != "date"]
        if columns is not None:
            names = [c for c in names if c in set(columns)]
        specs = [_DATE] + [ColumnSpec(c, "str" if c in _TEXT_COLUMNS else "float")
                           for c in names]
        arrays = read_csv_typed(data, specs)
        if "date" not in arrays:
            return None
        frame = pd.DataFrame({spec.key: arrays[spec.key] for spec in specs if spec.key in arrays})
        frame["date"] = pd.to_datetime(frame["date"].astype("datetime64[ns]"))
        return frame[frame["date"].notna()].sort_values("date").reset_index(drop=True)

    def pledge_ratio(self, dirs: Sequence[str]) -> Optional[pd.Series]:
        """Share of total equity under pledge (fraction) by date.

        The store has two ``pledge.csv`` layouts: a per-event detail table
        (``accumulatedPledgePercentageOfTotalEquity`` = running total after
        each event) and a summary table (``pledgeRatio``).  Both are already
        fractions (0.17 = 17 %).  ``None`` when neither is usable.
        """
        frame = self.alt(dirs, "pledge", columns=("pledgeRatio",
                                                  "accumulatedPledgePercentageOfTotalEquity"))
        if frame is None:
            return None
        ratio = None
        for column in ("pledgeRatio", "accumulatedPledgePercentageOfTotalEquity"):
            if column in frame.columns and frame[column].notna().any():
                ratio = frame[column]
                break
        if ratio is None:
            return None
        out = pd.Series(ratio.to_numpy(dtype=float), index=pd.DatetimeIndex(frame["date"]))
        out = out.dropna()
        return out[~out.index.duplicated(keep="last")].sort_index() if len(out) else None

    # --------------------------------------------------------------- index
    def index_close(self, code: str) -> Optional[pd.Series]:
        """Daily close of an index from ``index/<DIR>/lxg/daily.csv`` — the
        lxg provider tree of ``cn/index`` — or ``None`` when the store has
        no such file.

        ``code`` is the bare code (``"000300"``, ``"399006"``) or the
        directory name (``"SH000300"``).  A bare ``000…`` code is a 沪市
        index and ``399…`` a 深市 one, as in the store's own naming;
        ``<code>.SH`` / ``<code>.SZ`` spellings are tried as well.  Only
        ``close`` is read; this is a benchmark series, not a screen input.
        """
        code = str(code).strip().upper()
        if len(code) == 6 and code.isdigit():
            prefixed = ("SZ" if code.startswith("399") else "SH") + code
            names = [prefixed, code, f"{code}.SH", f"{code}.SZ"]
        else:
            names = [code]
        for name in names:
            path = self.data_dir / self.market / "index" / name / PROVIDER_LXG / "daily.csv"
            arrays = read_csv_typed(read_bytes(path),
                                    (_DATE, ColumnSpec("close")))
            if "date" not in arrays or "close" not in arrays:
                continue
            keep = ~pd.isna(arrays["date"]) & np.isfinite(arrays["close"])
            if not keep.any():
                continue
            series = pd.Series(arrays["close"][keep].astype(float),
                               index=pd.DatetimeIndex(arrays["date"][keep].astype("datetime64[ns]")))
            return series[~series.index.duplicated(keep="last")].sort_index()
        return None


__all__ = ["LXG_FILES", "LxgProvider"]
