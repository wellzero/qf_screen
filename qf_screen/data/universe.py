"""Canonical CN A-share universe: symbol metadata, boards, ST names.

One row per *stock*, keyed by the canonical xueqiu spelling (``SH600519``,
``SZ000001``, ``BJ920001`` -- :func:`quant_free.common.symbol_format.to_canonical`).
The universe is the union of two sources, because each one alone is biased:

* ``cn/lxg/company_list.csv`` -- a TODAY snapshot of the listing (name, board
  exchange, IPO date, listing status, 沪深港通 / margin eligibility).  It
  has no rows for names that delisted before the snapshot;
* the on-disk store ``cn/equity/<DIR>/`` -- which does keep delisted names
  (the survivorship evidence), but knows nothing about them beyond their
  prices.

The census is cached on disk (see :func:`build_universe`), keyed on the
exact inputs that produce it.

The store carries duplicate directory trees for the same company under
different spellings (``SH600036``, ``600036`` AND ``600036.SS``), so every
canonical symbol maps to an ordered tuple of directory names; the
canonical-named directory comes first and the data readers take the first
alias that actually has the requested file.

Boards are derived from the 6-digit code, trusting the code range over a
mislabelled exchange prefix (the same repair
:func:`quant_free.datapull.cn_delisted_backfill.canonical_equity_symbol`
applies).
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from quant_free.common.const_filename import EQUITY_DIR, PROVIDER_LXG, get_data_dir

from ._io import ColumnSpec, read_bytes, read_csv, read_csv_typed, to_dates

logger = logging.getLogger(__name__)

#: (canonical_equity_symbol, _INDEX_RE) of
#: :mod:`quant_free.datapull.cn_delisted_backfill`, bound on FIRST USE
_SIBLING: Optional[Tuple[object, object]] = None


def _quant_free_symbols():
    """The two symbol helpers this module takes from the sibling repo,
    imported on first use rather than at import time.

    That module pulls in ``quant_free.utils.us_equity_utils`` — which
    imports ``yfinance`` (and with it bs4, peewee, ...) — for two
    regex-level helpers: ~0.11 s of every ``qf-screen`` start.  A warm run
    (universe served from its cache) never needs either helper at all.
    """
    global _SIBLING
    if _SIBLING is None:
        from quant_free.datapull.cn_delisted_backfill import (
            _INDEX_RE, canonical_equity_symbol,
        )

        _SIBLING = (canonical_equity_symbol, _INDEX_RE)
    return _SIBLING

# ------------------------------------------------------------------ boards
SH_MAIN = "sh_main"      # 沪市主板  600/601/603/605
SZ_MAIN = "sz_main"      # 深市主板  000/001/002/003
CHINEXT = "chinext"      # 创业板    30xxxx
STAR = "star"            # 科创板    688/689
BJ = "bj"                # 北交所    92xxxx / 43 / 83 / 87xxxx

ALL_BOARDS: Tuple[str, ...] = (SH_MAIN, SZ_MAIN, CHINEXT, STAR, BJ)

#: board groups a spec may name instead of listing boards one by one
BOARD_GROUPS: Dict[str, Tuple[str, ...]] = {
    "main": (SH_MAIN, SZ_MAIN),
    "a_share": (SH_MAIN, SZ_MAIN, CHINEXT, STAR),
    "all": ALL_BOARDS,
}

BOARD_LABELS: Dict[str, str] = {
    SH_MAIN: "沪市主板", SZ_MAIN: "深市主板", CHINEXT: "创业板",
    STAR: "科创板", BJ: "北交所",
}
_LABEL_TO_BOARD = {label: board for board, label in BOARD_LABELS.items()}

#: daily price limit by board (fraction) -- used in explanations only
BOARD_LIMITS: Dict[str, float] = {
    SH_MAIN: 0.10, SZ_MAIN: 0.10, CHINEXT: 0.20, STAR: 0.20, BJ: 0.30,
}

#: listingStatus values of company_list.csv that mark a risk-warning name
ST_STATUSES = frozenset({"special_treatment", "delisting_risk_warning"})

#: ST-flavoured names: ST / *ST / S*ST / SST (the set qf_factor's ST
#: enrichment uses), anchored at the start of the name
_ST_NAME_RE = re.compile(r"^\*?ST|^S\*?ST|^SST")

_TRUTHY = frozenset({"true", "1", "t", "yes"})

_META_COLUMNS: Tuple[str, ...] = (
    "name", "exchange", "board", "list_date", "listing_status", "fs_type",
    "connect", "margin_eligible", "industry_l1", "industry_l2", "industry_l3",
    "in_company_list",
)


def board_of(symbol: str) -> Optional[str]:
    """A-share board of ``symbol`` in any spelling, or ``None`` when the code
    is not in an A-share range (B-shares, ETFs, bonds, index series ...)."""
    canon = _quant_free_symbols()[0](symbol)
    if canon is None:
        return None
    exchange, code = canon[:2], canon[2:]
    if exchange == "SH":
        if code[:3] in ("600", "601", "603", "605"):
            return SH_MAIN
        if code[:3] in ("688", "689"):
            return STAR
        return None
    if exchange == "SZ":
        if code[:3] in ("000", "001", "002", "003"):
            return SZ_MAIN
        if code[:2] == "30":
            return CHINEXT
        return None
    if code[:2] == "92" or code[0] in "48":
        return BJ
    return None


def is_a_share(symbol: str) -> bool:
    return board_of(symbol) is not None


def resolve_boards(names: Iterable[str]) -> Tuple[str, ...]:
    """Expand board names / groups (``main``, ``all``, Chinese labels; case,
    ``-`` and ``_`` insensitive) into a de-duplicated board tuple."""
    out: List[str] = []
    for raw in names:
        key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
        key = _LABEL_TO_BOARD.get(key, key)
        if key in BOARD_GROUPS:
            wanted = BOARD_GROUPS[key]
        elif key in ALL_BOARDS:
            wanted = (key,)
        else:
            raise ValueError(
                f"unknown board {raw!r}; known boards: {', '.join(ALL_BOARDS)}; "
                f"groups: {', '.join(BOARD_GROUPS)}")
        for board in wanted:
            if board not in out:
                out.append(board)
    if not out:
        raise ValueError("board selection is empty")
    return tuple(out)


def _text(value) -> str:
    """``str(value)`` with pandas' missing sentinels mapped to ``""``."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):    # array-like: not a scalar
        return str(value)
    return str(value).strip()


def is_st_name(name, listing_status=None) -> bool:
    """Whether a company-list row describes an ST/*ST (risk-warning) name:
    either the listing status says so or the name carries the ST prefix."""
    if _text(listing_status).lower() in ST_STATUSES:
        return True
    return bool(_ST_NAME_RE.match(_text(name).upper()))


# ------------------------------------------------------------------ store
def scan_equity_dirs(root) -> Dict[str, Tuple[str, ...]]:
    """Canonical symbol -> ordered store directory names.

    Skips index-like and non-equity names (futures, ETFs, B-shares,
    convertibles).  The canonical-named directory sorts first so a reader
    that walks the tuple prefers it; the rest follow shortest-name-first
    (``600036`` before ``600036.SS``).
    """
    if root is None or not Path(root).is_dir():
        return {}
    canonical_equity_symbol, index_re = _quant_free_symbols()
    found: Dict[str, List[str]] = {}
    with os.scandir(root) as entries:
        for entry in entries:
            if not entry.is_dir():
                continue
            name = entry.name
            if index_re.match(name.upper()):
                continue
            canon = canonical_equity_symbol(name)
            if canon is not None:
                found.setdefault(canon, []).append(name)
    return {canon: tuple(sorted(names, key=lambda n: (n != canon, len(n), n)))
            for canon, names in found.items()}


def _parse_company_list(df: pd.DataFrame) -> pd.DataFrame:
    """Normalized company_list rows indexed by canonical symbol.

    ``stockCode`` arrives as an int (``1`` for 平安银行), so the code is
    zero-padded back to 6 digits before the exchange prefix is attached.
    Only A-shares (``market == 'a'``) in A-share code ranges are kept --
    the list also carries 77 B-shares.
    """
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in ("stockCode", "exchange") if c not in df.columns]
    if missing:
        raise ValueError(f"company_list.csv lacks column(s) {missing}")
    for col in _META_COLUMNS:
        if col not in df.columns:
            df[col] = None

    code = df["stockCode"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    code = code.str.zfill(6)
    exchange = df["exchange"].map(_text).str.lower()
    symbol = (exchange.str.upper() + code).rename("symbol")
    if "market" in df.columns:
        a_share = df["market"].map(_text).str.lower().eq("a")
    else:
        a_share = pd.Series(True, index=df.index)
    board = pd.Series([board_of(s) for s in symbol], index=df.index)

    # every column is assigned positionally (``.to_numpy()``): ``out`` is
    # indexed by symbol while ``df`` still carries a RangeIndex, and a
    # Series assignment would align the two indexes and blank the column
    out = pd.DataFrame(index=symbol)
    out["name"] = df["name"].map(_text).to_numpy()
    out["exchange"] = exchange.to_numpy()
    out["board"] = board.to_numpy()
    out["list_date"] = to_dates(df["ipoDate"]).to_numpy()
    out["listing_status"] = df["listingStatus"].map(_text).to_numpy()
    out["fs_type"] = df["fsTableType"].map(_text).to_numpy()
    # mutualMarketFlag = 沪深港通 (northbound) eligibility
    out["connect"] = df["mutualMarketFlag"].map(_text).str.lower().isin(_TRUTHY).to_numpy()
    out["margin_eligible"] = df["marginTradingAndSecuritiesLendingFlag"].map(_text) \
        .str.lower().isin(_TRUTHY).to_numpy()
    out["in_company_list"] = True
    # the company list carries no industry classification (that lives in the
    # per-symbol lxg industries.csv, filled in by build_universe)
    for col in ("industry_l1", "industry_l2", "industry_l3"):
        out[col] = [None] * len(out)
    keep = (a_share & board.notna()).to_numpy()
    out = out[keep]
    return out[~out.index.duplicated(keep="last")][list(_META_COLUMNS)]


#: industries.csv is three short text columns
_INDUSTRY_SPECS = (ColumnSpec("areaCode", "str"), ColumnSpec("source", "str"),
                   ColumnSpec("name", "str"))


def _sw_levels(arrays: Optional[Dict[str, np.ndarray]]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(L1, L2, L3) 申万 industry names from a per-symbol ``industries.csv``.

    ``sw_2021`` rows come in ordered triples (一级行业, 二级, 三级); a
    company classified twice carries two triples and the first wins.  Falls
    back to the older ``sw`` classification, then to none.
    """
    if not arrays or "source" not in arrays or "name" not in arrays:
        return None, None, None
    sources, names = arrays["source"], arrays["name"]
    for source in ("sw_2021", "sw"):
        rows = [i for i, value in enumerate(sources) if str(value).strip() == source]
        if len(rows) >= 3:
            return tuple((_text(names[rows[k]]) or None) for k in range(3))
    return None, None, None


class Universe:
    """Per-stock metadata + store directory resolution."""

    def __init__(self, meta: pd.DataFrame, dirs: Dict[str, Tuple[str, ...]],
                 root: Optional[Path], names_asof: Optional[pd.Timestamp]):
        self.meta = meta
        self.dirs = dirs
        self.root = Path(root) if root is not None else None
        #: date the company-list snapshot (names / listing status) speaks for
        self.names_asof = names_asof
        self._st_named: Optional[frozenset] = None

    # ----------------------------------------------------------- lookups
    def __len__(self) -> int:
        return len(self.meta)

    def __contains__(self, symbol) -> bool:
        return symbol in self.meta.index

    def __repr__(self) -> str:
        return f"<Universe {len(self.meta)} symbols, names as of {self.names_asof}>"

    @property
    def symbols(self) -> pd.Index:
        return self.meta.index

    def dir_names(self, symbol: str) -> Tuple[str, ...]:
        return self.dirs.get(symbol, ())

    def resolve(self, symbol: str, *parts: str) -> Optional[Path]:
        """First existing ``<root>/<dir>/<parts>`` over the symbol's aliases."""
        if self.root is None:
            return None
        for name in self.dirs.get(symbol, ()):
            path = self.root.joinpath(name, *parts)
            if path.exists():
                return path
        return None

    def get(self, symbol: str, column: str, default=None):
        """One metadata value; missing / NaN / NaT all yield ``default``."""
        try:
            value = self.meta.at[symbol, column]
        except (KeyError, TypeError):
            return default
        return default if value is None or value is pd.NaT or _is_nan(value) else value

    def frame(self) -> pd.DataFrame:
        return self.meta.copy()

    def select_boards(self, boards: Iterable[str]) -> List[str]:
        """Symbols on the given boards, in index order."""
        wanted = frozenset(resolve_boards(boards))
        board = self.meta["board"]
        return [s for s in self.meta.index if board.get(s) in wanted]

    @property
    def st_named(self) -> frozenset:
        """Symbols the company-list snapshot marks as ST/*ST.

        A TODAY snapshot -- valid as an exclusion only from ``names_asof``
        forward (see :mod:`qf_screen.rules.hygiene`).
        """
        if self._st_named is None:
            if "name" in self.meta.columns:
                mask = [is_st_name(n, s) for n, s in
                        zip(self.meta["name"], self.meta.get("listing_status"))]
            else:
                mask = []
            self._st_named = frozenset(self.meta.index[mask])
        return self._st_named


def _is_nan(value) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def build_universe(data_dir=None, market: str = "cn", with_industries: bool = True,
                   max_workers: int = 16, use_cache: bool = True,
                   symbol_store=None) -> Universe:
    """Universe = company-list snapshot ∪ on-disk stock directories.

    The census is a pure function of three things on disk — the set of
    equity directories, ``company_list.csv`` and every
    ``<dir>/lxg/industries.csv`` — so it is cached under
    ``qf_screen_cache/universe_<market>.*`` behind
    :func:`universe_fingerprint` of exactly those.  Building it costs ~1 s
    on the real store (2,042 ``industries.csv`` parses plus a scan of
    ~9,800 directories) and every command needs it; reading it back costs
    a few tens of ms.  ``use_cache=False`` (``--no-cache``) builds from
    source and touches nothing.

    ``symbol_store`` (a :class:`qf_screen.symbols.SymbolStore`) fills the
    GAPS the passive files leave — a name for a stock the company list has
    no row for, an ``industry_l1`` for a name without ``industries.csv`` —
    and only fills: a local value always wins.  Its catalog files join the
    fingerprint, so a ``symbol sync`` invalidates the cache honestly; a
    store with no files on disk contributes nothing and every cache built
    before this parameter existed still hits.
    """
    data_dir = Path(data_dir or get_data_dir())
    root = data_dir / market / EQUITY_DIR
    list_path = data_dir / market / PROVIDER_LXG / "company_list.csv"
    fills = _symbol_fills(symbol_store)
    cacheable = bool(use_cache and with_industries)
    symbol_root = symbol_store.root \
        if symbol_store is not None and symbol_store.present else None
    fingerprint = universe_fingerprint(
        root, list_path, symbol_root=symbol_root) if cacheable else None
    if cacheable:
        cached = _read_universe_cache(data_dir, market, root, fingerprint)
        if cached is not None:
            logger.info("universe: %d symbols (names as of %s) FROM CACHE",
                        len(cached), cached.names_asof)
            return cached

    raw = read_csv(list_path, dtype=str)
    listed = _parse_company_list(raw) if raw is not None \
        else pd.DataFrame(columns=list(_META_COLUMNS))
    names_asof = pd.Timestamp(list_path.stat().st_mtime, unit="s").normalize() \
        if list_path.exists() else None

    dirs = scan_equity_dirs(root)
    symbols = sorted(set(dirs) | set(listed.index))
    if not symbols:
        raise ValueError(f"no A-share symbols found under {data_dir} "
                         f"({list_path.name} + {root})")
    meta = listed.reindex(symbols)
    listed_flag = meta["in_company_list"]
    meta["in_company_list"] = listed_flag.where(listed_flag.notna(), False).astype(bool)
    meta["board"] = [board_of(s) for s in symbols]

    # industries live only in the per-symbol lxg file (the company list has
    # none); names that are not on disk simply have no classification
    if with_industries and root.is_dir():
        todo = [(s, root / dirs[s][0] / PROVIDER_LXG / "industries.csv")
                for s in symbols if s in dirs]
        todo = [(s, p) for s, p in todo if p.exists()]

        def _levels(item):
            return item[0], _sw_levels(read_csv_typed(read_bytes(item[1]), _INDUSTRY_SPECS))

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            levels = dict(ex.map(_levels, todo))
        for col, idx in (("industry_l1", 0), ("industry_l2", 1), ("industry_l3", 2)):
            values = pd.Series({s: levels[s][idx] for s in levels}, dtype=object)
            meta[col] = values.reindex(meta.index).to_numpy()

    if fills is not None:
        _apply_symbol_fills(meta, fills)
    universe = Universe(meta=meta, dirs=dirs, root=root, names_asof=names_asof)
    logger.info("universe: %d symbols (%d in company list, %d on disk, "
                "names as of %s)", len(meta), len(listed), len(dirs), names_asof)
    if cacheable:
        _write_universe_cache(data_dir, market, universe, fingerprint)
    return universe


# ------------------------------------------------------------ universe cache
#: cache file stem under ``qf_screen_cache/`` and the format version kept
#: in its sidecar (bump to invalidate)
_UNIVERSE_VERSION = "v1"


def universe_fingerprint(root, list_path, symbol_root=None) -> str:
    """``<equity dir count>:<industries file count>:<mtime sum>`` over the
    equity tree, ``company_list.csv`` and every ``<dir>/lxg/industries.csv``
    — exactly the inputs of :func:`build_universe` — plus, when a symbol
    catalog is on disk, ``:<file count>:<mtime sum>`` of it.

    The directory COUNT alone catches a listing or delisting; the
    industries census catches a new lxg download that starts classifying
    names whose directory already existed, and a rewrite of any one of
    them (the download bumps the mtime); the mtime SUM catches a rewrite
    that leaves a file older than the newest input.  ~30 ms on the real
    store's ~9,800 directories.

    ``symbol_root`` joins in ONLY when the catalog exists, so a store
    without one (every cache built before the symbols module) keeps its
    fingerprint and still hits.
    """
    count = industries = 0
    total = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                count += 1
                try:
                    total += os.stat(os.path.join(entry.path, PROVIDER_LXG,
                                                  "industries.csv")).st_mtime_ns
                except OSError:
                    continue
                industries += 1
    except OSError:
        pass
    try:
        total += os.stat(list_path).st_mtime_ns
    except OSError:
        pass
    out = f"{count}:{industries}:{total}"
    if symbol_root is not None and Path(symbol_root).is_dir():
        files = [p for p in Path(symbol_root).rglob("*.csv")]
        meta = Path(symbol_root) / "meta.json"
        if meta.exists():
            files.append(meta)
        out += f":{len(files)}:{sum(os.stat(p).st_mtime_ns for p in files)}"
    return out


def _symbol_fills(symbol_store):
    """The gap-filling maps a symbol store can contribute, or ``None`` when
    it has nothing (absent / empty catalog): ``{"names": …, "industry_l1": …}``."""
    if symbol_store is None or not symbol_store.present:
        return None
    fills = {"names": symbol_store.stock_names(),
             "industry_l1": symbol_store.industry_l1()}
    return fills if any(fills.values()) else None


def _apply_symbol_fills(meta: pd.DataFrame, fills: dict) -> None:
    """Fill NAME and ``industry_l1`` gaps in the census from the symbol
    catalog -- fills only, never overwrites (a local file is the authority
    wherever it speaks)."""
    names, industries = fills["names"], fills["industry_l1"]
    filled_names = filled_industries = 0
    for symbol in meta.index:
        if not _text(meta.at[symbol, "name"]):
            value = names.get(symbol, "")
            if value:
                meta.at[symbol, "name"] = value
                filled_names += 1
        if "industry_l1" in meta.columns \
                and not _text(meta.at[symbol, "industry_l1"]):
            value = industries.get(symbol, "")
            if value:
                meta.at[symbol, "industry_l1"] = value
                filled_industries += 1
    if filled_names or filled_industries:
        logger.info("symbol catalog filled %d name(s) and %d industry_l1 "
                    "gap(s) the passive files leave", filled_names,
                    filled_industries)


def _universe_cache_base(data_dir, market: str) -> Path:
    from .table_cache import cache_dir_for

    return cache_dir_for(data_dir, market) / f"universe_{market}"


def _read_universe_cache(data_dir, market: str, root: Path,
                         fingerprint: str) -> Optional[Universe]:
    """The cached universe when its fingerprint matches, else ``None``."""
    base = _universe_cache_base(data_dir, market)
    try:
        import pyarrow  # noqa: F401 - parquet engine; miss if absent

        doc = json.loads(base.with_name(base.name + ".meta.json").read_text(encoding="utf-8"))
        if doc.get("version") != _UNIVERSE_VERSION or doc.get("fingerprint") != fingerprint:
            return None
        meta = pd.read_parquet(base.with_name(base.name + ".parquet"))
        dirs = {symbol: tuple(names) for symbol, names in doc["dirs"].items()}
        # the metadata and the directory map must agree: every mapped
        # directory is a metadata row (the list-only names have no directory)
        if meta.index.has_duplicates or not set(dirs) <= set(meta.index) \
                or list(meta.columns) != list(_META_COLUMNS):
            return None
    except Exception:  # noqa: BLE001 - any cache problem is a miss
        return None
    names_asof = pd.Timestamp(doc["names_asof"]) if doc.get("names_asof") else None
    # parquet hands nulls back as None; a freshly built census carries NaN
    # in its text columns (the company-list reindex), so restore that
    for column in meta.columns:
        if meta[column].dtype == object:
            values = meta[column].to_numpy()
            holes = np.array([value is None for value in values], dtype=bool)
            if holes.any():
                meta[column] = np.where(holes, np.nan, values)
    return Universe(meta=meta, dirs=dirs, root=root, names_asof=names_asof)


def _write_universe_cache(data_dir, market: str, universe: Universe,
                          fingerprint: str) -> None:
    """Persist the universe (best effort).  The metadata frame is replaced
    before the sidecar that names its fingerprint, so an interrupted write
    is a miss on the next run, never a stale hit."""
    base = _universe_cache_base(data_dir, market)
    try:
        import pyarrow  # noqa: F401

        base.parent.mkdir(parents=True, exist_ok=True)
        tmp = base.with_name(base.name + ".parquet.tmp")
        universe.meta.to_parquet(tmp)
        tmp.replace(base.with_name(base.name + ".parquet"))
        doc = {
            "version": _UNIVERSE_VERSION, "fingerprint": fingerprint,
            "names_asof": universe.names_asof.isoformat()
            if universe.names_asof is not None else None,
            "dirs": {symbol: list(names) for symbol, names in universe.dirs.items()},
        }
        sidecar = base.with_name(base.name + ".meta.json")
        tmp = base.with_name(base.name + ".meta.json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        tmp.replace(sidecar)
    except Exception as exc:  # noqa: BLE001
        logger.warning("universe cache write failed (%s) -- continuing without", exc)
