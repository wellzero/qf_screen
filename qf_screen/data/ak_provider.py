"""Local ``ak`` provider readers + the optional live akshare client.

Local (offline, always available)::

    cn/equity/<SYM>/ak/basic.csv               -- ~5,500 symbols
    cn/equity/<SYM>/ak/research_reports.csv    -- ~1,000 symbols

``basic.csv`` is written by ``quant_free.datapull.ak_basic_download`` and
has two properties the screener has to respect:

* ``close`` is the **unadjusted** traded price (identical to lxg's
  ``close_raw`` on 2,184,762 of 2,189,223 common rows), NOT an adjusted
  series -- the adjusted series is rebuilt in
  :class:`~.merged_store.CNDataStore`;
* there is **no traded value** (``amount``); it is reconstructed as
  ``turnoverrate / 100 * float_market_capital`` there.

Valuation columns start around 2018 (the 东财 F10 history); a suspended day
is a row with a blank ``close``, and the file may end with a partial
"today" row that carries valuation but no close yet.

Live (opt-in) -- :class:`LiveAkClient` wraps a handful of public akshare
endpoints (real-time spot snapshot, 东财 industry / concept board
constituents) behind a disk cache with a TTL.  akshare is imported lazily,
only when ``refresh=True``; the offline path and the unit tests never touch
it (tests inject a fake module).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

import pandas as pd

from quant_free.common.const_filename import EQUITY_DIR, PROVIDER_AK, get_data_dir
from quant_free.common.symbol_format import to_canonical

from ._io import (ColumnSpec, ColumnTable, numeric, read_bytes, read_csv, read_csv_typed)

logger = logging.getLogger(__name__)

#: ``basic.csv`` columns in file order
BASIC_COLS: Tuple[str, ...] = (
    "date", "close", "pct_chg", "turnoverrate", "pe_ttm", "pe_static", "pb", "ps",
    "pcf", "peg", "market_capital", "float_market_capital", "total_shares", "float_shares",
)

#: research_reports.csv columns
REPORT_COLS: Tuple[str, ...] = ("report_date", "rating", "org", "title", "eps_fy1")

_DATE = ColumnSpec("date", "date")
#: how ``basic.csv`` is read: EM's 市销率 / 市现率 are trailing-twelve-month
#: measures -- they equal lxg's ``ps_ttm`` / ``pcf_ttm`` on common dates --
#: so they take the store names
_BASIC_SPECS: Tuple[ColumnSpec, ...] = (
    _DATE, ColumnSpec("close"), ColumnSpec("pct_chg"), ColumnSpec("turnoverrate"),
    ColumnSpec("pe_ttm"), ColumnSpec("pe_static"), ColumnSpec("pb"),
    ColumnSpec("ps", "float", "ps_ttm"), ColumnSpec("pcf", "float", "pcf_ttm"),
    ColumnSpec("peg"), ColumnSpec("market_capital"), ColumnSpec("float_market_capital"),
    ColumnSpec("total_shares"), ColumnSpec("float_shares"),
)
_REPORT_SPECS: Tuple[ColumnSpec, ...] = (
    ColumnSpec("report_date", "date"), ColumnSpec("rating", "str"),
    ColumnSpec("org", "str"), ColumnSpec("title", "str"), ColumnSpec("eps_fy1"),
)

# ------------------------------------------------------------------ live
#: akshare -> store names for the real-time spot snapshot (东财 ``stock_zh_a_spot_em``)
_SPOT_MAP = {
    "代码": "code", "名称": "name", "最新价": "close", "涨跌幅": "pct_chg",
    "成交额": "amount", "换手率": "turnoverrate", "市盈率-动态": "pe_ttm",
    "市净率": "pb", "总市值": "market_capital", "流通市值": "float_market_capital",
    "昨收": "prev_close",
}
#: board constituents (``stock_board_*_cons_em``)
_CONS_MAP = {"代码": "code", "名称": "name"}
_BOARD_NAME_COL = "板块名称"

_SPOT_COLS = ("symbol", "name", "close", "pct_chg", "amount", "turnoverrate",
              "pe_ttm", "pb", "market_capital", "float_market_capital", "prev_close")

#: eastmoney refuses connections through a proxy (the sibling downloader
#: hit this on 2026-10-26), so these hosts are appended to ``no_proxy`` in
#: the calling process only -- same list, same reason.
DIRECT_HOSTS = (
    "push2his.eastmoney.com", "push2.eastmoney.com", "datacenter.eastmoney.com",
    "data.eastmoney.com", "emweb.securities.eastmoney.com", "xueqiu.com",
    "stock.xueqiu.com",
)


class LiveDataError(RuntimeError):
    """Live refresh was requested but is off, unavailable or failed."""


def _bypass_proxy_for_eastmoney() -> None:
    for var in ("no_proxy", "NO_PROXY"):
        cur = os.environ.get(var, "")
        have = {h.strip().lower() for h in cur.split(",") if h.strip()}
        add = [h for h in DIRECT_HOSTS if h.lower() not in have]
        if add:
            os.environ[var] = ",".join(filter(None, [cur, *add]))


class AkProvider:
    """Readers over ``<data_dir>/<market>/equity/<DIR>/ak/``."""

    provider = PROVIDER_AK

    def __init__(self, data_dir=None, market: str = "cn"):
        self.data_dir = Path(data_dir or get_data_dir())
        self.market = market
        self.root = self.data_dir / market / EQUITY_DIR
        self._base = f"{self.root}{os.sep}"          # see LxgProvider.find

    def find(self, dirs: Sequence[str], filename: str) -> Optional[Path]:
        """First existing ``<dir>/ak/<filename>`` over a symbol's aliases."""
        base = self._base
        for name in dirs:
            path = f"{base}{name}{os.sep}{PROVIDER_AK}{os.sep}{filename}"
            if os.path.exists(path):
                return Path(path)
        return None

    def read(self, dirs: Sequence[str], name: str, **kw) -> Optional[pd.DataFrame]:
        return read_csv(self.find(dirs, f"{name}.csv"), **kw)

    def basic(self, dirs: Sequence[str], start=None, end=None,
              tail_rows: Optional[int] = None) -> Optional[ColumnTable]:
        """Per-symbol ``basic.csv`` as a dated table.

        Rows with a blank ``close`` are KEPT -- a blank close is how this
        file marks a suspended day.  ``tail_rows`` reads only the last rows
        of the file (snapshot path); ``start`` / ``end`` clip afterwards.
        """
        data = read_bytes(self.find(dirs, "basic.csv"), tail_rows)
        table = ColumnTable.from_typed(read_csv_typed(data, _BASIC_SPECS), _BASIC_SPECS)
        return table.clip(start, end) if table is not None else None

    def research_reports(self, dirs: Sequence[str]) -> Optional[pd.DataFrame]:
        """Analyst reports: ``report_date, rating, org, title, eps_fy1``."""
        arrays = read_csv_typed(read_bytes(self.find(dirs, "research_reports.csv")),
                                _REPORT_SPECS)
        if not arrays or "report_date" not in arrays:
            return None
        frame = pd.DataFrame({spec.key: arrays[spec.key] for spec in _REPORT_SPECS
                              if spec.key in arrays})
        frame["report_date"] = pd.to_datetime(frame["report_date"].astype("datetime64[ns]"))
        frame = frame[frame["report_date"].notna()]
        return frame.drop_duplicates().sort_values("report_date").reset_index(drop=True) \
            if not frame.empty else None


# ------------------------------------------------------------------ live
class LiveAkClient:
    """Opt-in live akshare access with a disk TTL cache.

    ``refresh=False`` (the default everywhere) makes every method raise
    :class:`LiveDataError` without importing akshare: the offline path never
    touches the network.  With ``refresh=True`` a cache entry younger than
    its TTL is reused, otherwise akshare is called; if the call fails and an
    older cache exists it is served with a warning (``fetched_at`` in the
    returned metadata tells the caller how old it is).

    Parameters
    ----------
    cache_dir : where ``<key>.csv`` + ``<key>.meta.json`` live.
    ttl_spot, ttl_boards : cache ages in seconds.
    ak : an object exposing the akshare functions (tests inject a fake);
        ``None`` imports :mod:`akshare` on first use.
    pause : seconds between the per-board constituent requests.
    """

    SPOT = "spot"
    INDUSTRY = "industry_members"
    CONCEPT = "concept_members"

    def __init__(self, cache_dir, *, refresh: bool = False, ttl_spot: float = 1800.0,
                 ttl_boards: float = 86400.0, ak=None, pause: float = 0.3):
        self.cache_dir = Path(cache_dir)
        self.refresh = bool(refresh)
        self.ttl = {self.SPOT: ttl_spot, self.INDUSTRY: ttl_boards,
                    self.CONCEPT: ttl_boards}
        self._ak = ak
        self.pause = pause
        self.calls = 0          # network calls made (provenance / tests)

    # ------------------------------------------------------------ plumbing
    def _module(self):
        if self._ak is None:
            try:
                import akshare  # noqa: F401  (lazy: only on a live request)
            except ImportError as exc:
                raise LiveDataError("akshare is not installed; the live refresh "
                                    "needs `pip install akshare`") from exc
            self._ak = akshare
        return self._ak

    def _fetch(self, key: str, load: Callable[[], pd.DataFrame]):
        if not self.refresh:
            raise LiveDataError(
                f"live data ({key}) is off -- pass --live / refresh=True to "
                "call akshare")
        ttl = self.ttl[key]
        path, meta_path = self.cache_dir / f"{key}.csv", self.cache_dir / f"{key}.meta.json"
        cached = self._read_cache(path, meta_path)
        if cached is not None:
            df, meta = cached
            if time.time() - meta["fetched_ts"] <= ttl:
                return df, {**meta, "fresh": False, "from_cache": True}
        try:
            _bypass_proxy_for_eastmoney()
            self.calls += 1
            df = load()
            if df is None or df.empty:
                raise LiveDataError(f"akshare returned no rows for {key}")
        except Exception as exc:  # noqa: BLE001 - network/parse errors vary
            if cached is not None:
                logger.warning("live %s failed (%s) -- serving the cache from %s",
                               key, exc, cached[1]["fetched_at"])
                return cached[0], {**cached[1], "fresh": False, "from_cache": True,
                                   "error": str(exc)}
            raise LiveDataError(f"live {key} failed: {exc}") from exc
        now = time.time()
        meta = {"fetched_at": pd.Timestamp(now, unit="s").strftime("%Y-%m-%d %H:%M:%S"),
                "fetched_ts": now, "source": f"akshare.{getattr(load, 'source', key)}",
                "rows": int(len(df)), "fresh": True, "from_cache": False}
        self._write_cache(path, meta_path, df, meta)
        return df, meta

    @staticmethod
    def _read_cache(path: Path, meta_path: Path):
        if not (path.exists() and meta_path.exists()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            df = pd.read_csv(path, dtype={"code": str, "symbol": str})
            return df, meta
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_cache(path: Path, meta_path: Path, df: pd.DataFrame, meta: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".csv.tmp")
        df.to_csv(tmp, index=False)
        tmp.replace(path)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _source(fn, name: str):
        fn.source = name
        return fn

    # -------------------------------------------------------------- spot
    def spot(self) -> Tuple[pd.DataFrame, dict]:
        """Real-time snapshot of every A-share: ``symbol, name, close,
        pct_chg, amount, turnoverrate, pe_ttm, pb, market_capital,
        float_market_capital, prev_close`` + cache metadata."""
        def load():
            raw = self._module().stock_zh_a_spot_em()
            return normalize_spot(raw)
        return self._fetch(self.SPOT, self._source(load, "stock_zh_a_spot_em"))

    # ------------------------------------------------------------ boards
    def industry_members(self) -> Tuple[pd.DataFrame, dict]:
        """(board, code, symbol, name) rows of the 东财 industry boards."""
        def load():
            ak = self._module()
            boards = ak.stock_board_industry_name_em()
            return self._concat_constituents(
                boards[_BOARD_NAME_COL].tolist(), ak.stock_board_industry_cons_em)
        return self._fetch(self.INDUSTRY, self._source(load, "stock_board_industry_cons_em"))

    def concept_members(self) -> Tuple[pd.DataFrame, dict]:
        """(board, code, symbol, name) rows of the 东财 concept boards
        (several hundred boards -- one request each, hence the day-long TTL)."""
        def load():
            ak = self._module()
            boards = ak.stock_board_concept_name_em()
            return self._concat_constituents(
                boards[_BOARD_NAME_COL].tolist(), ak.stock_board_concept_cons_em)
        return self._fetch(self.CONCEPT, self._source(load, "stock_board_concept_cons_em"))

    def _concat_constituents(self, names: Sequence[str], getter) -> pd.DataFrame:
        parts = []
        for i, name in enumerate(names):
            self.calls += 1
            cons = getter(symbol=name)
            if cons is None or cons.empty:
                continue
            cons = cons.rename(columns=_CONS_MAP)
            if "code" not in cons.columns:
                continue
            parts.append(pd.DataFrame({"board": name, "code": cons["code"].astype(str),
                                       "name": cons["name"].astype(str)
                                       if "name" in cons.columns else ""}))
            if self.pause and i < len(names) - 1:
                time.sleep(self.pause)
        if not parts:
            raise LiveDataError("no board constituents returned")
        out = pd.concat(parts, ignore_index=True)
        out["symbol"] = [to_canonical(c) for c in out["code"].str.zfill(6)]
        out = out[out["symbol"].notna()].drop_duplicates(["board", "symbol"])
        return out[["board", "code", "symbol", "name"]].reset_index(drop=True)


#: spot columns that are numeric (symbol/name must stay strings -- running
#: them through numeric() would coerce to all-NaN and empty the table)
_SPOT_NUMERIC = ("close", "pct_chg", "amount", "turnoverrate", "pe_ttm", "pb",
                 "market_capital", "float_market_capital", "prev_close")


def normalize_spot(raw: pd.DataFrame) -> pd.DataFrame:
    """Canonical spot table from a raw ``stock_zh_a_spot_em`` frame."""
    if raw is None or raw.empty:
        raise LiveDataError("spot snapshot is empty")
    df = raw.rename(columns={k: v for k, v in _SPOT_MAP.items() if k in raw.columns})
    if "code" not in df.columns:
        raise LiveDataError("spot snapshot lacks a code column")
    df = numeric(df, [c for c in _SPOT_NUMERIC if c in df.columns])
    df["symbol"] = [to_canonical(str(c).strip().zfill(6)) for c in df["code"]]
    df = df[df["symbol"].notna()].drop_duplicates("symbol")
    for col in _SPOT_COLS:
        if col not in df.columns:
            df[col] = None
    return df[list(_SPOT_COLS)].reset_index(drop=True)
