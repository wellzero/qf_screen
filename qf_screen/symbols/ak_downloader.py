"""akshare downloaders for the stock list and the 东财 section boards.

Everything here is a thin, testable adapter: ``ak`` (the module or any
stand-in exposing the four akshare functions) is injectable, imported lazily
otherwise, and the network-facing proxy bypass is the one already used by
:class:`qf_screen.data.ak_provider.LiveAkClient` (eastmoney refuses proxied
connections).

Shapes out::

    download_ak_stocks()      -> symbol, code, name                (~5,400 rows)
    download_ak_sections()    -> (name, kind, members | level, ...)
                                 (section, symbol, code, name)

The constituent walk pauses ``pause`` seconds between the per-board requests
(the concept universe alone is several hundred boards); ``section_filter``
cuts it short (a name collection or a ``name -> bool`` callable).
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Tuple, Union

import pandas as pd

from quant_free.common.symbol_format import to_canonical

from qf_screen.data.ak_provider import _bypass_proxy_for_eastmoney

from .store import SymbolError

logger = logging.getLogger(__name__)

#: the boards listing's board-name column (东财 F10 spelling)
_BOARD_NAME_COL = "板块名称"
#: the constituents' code/name columns
_CONS_MAP = {"代码": "code", "名称": "name"}

SectionFilter = Union[None, Callable[[str], bool], frozenset, set, tuple, list]


def _module(ak=None):
    if ak is not None:
        return ak
    try:
        import akshare
    except ImportError as exc:                 # pragma: no cover - env-dependent
        raise SymbolError("akshare is not installed; the sync needs "
                          "`pip install akshare` (or the [live] extra)") from exc
    return akshare


def _wanted(name: str, section_filter: SectionFilter) -> bool:
    if section_filter is None:
        return True
    if callable(section_filter):
        return bool(section_filter(name))
    return name in section_filter


def download_ak_stocks(ak=None) -> pd.DataFrame:
    """Every listed A-share as ``symbol, code, name``.

    ``stock_info_a_code_name`` is the cheap dedicated endpoint; when it is
    unavailable the real-time spot snapshot stands in (it carries every
    listed name too).
    """
    module = _module(ak)
    _bypass_proxy_for_eastmoney()
    frame = None
    try:
        frame = module.stock_info_a_code_name()
    except Exception as exc:                   # noqa: BLE001 - endpoint shapes vary
        logger.warning("stock_info_a_code_name failed (%s) -- falling back to "
                       "the spot snapshot", exc)
    if frame is None or getattr(frame, "empty", True) or "code" not in frame.columns:
        frame = module.stock_zh_a_spot_em()
    if frame is None or getattr(frame, "empty", True):
        raise SymbolError("akshare returned no stock list")
    frame = frame.copy()
    frame.columns = [str(c).strip() for c in frame.columns]
    if "code" not in frame.columns and "代码" in frame.columns:
        frame = frame.rename(columns=_CONS_MAP)
    if "code" not in frame.columns:
        raise SymbolError("the akshare stock list lacks a code column")
    codes = frame["code"].astype(str).str.strip().str.zfill(6)
    symbols = [to_canonical(code) for code in codes]
    name = frame["name"].astype(str).str.strip() if "name" in frame.columns \
        else ""
    out = pd.DataFrame({"symbol": symbols, "code": codes,
                        "name": list(name) if not isinstance(name, str) else ""})
    out = out[out["symbol"].notna()].drop_duplicates("symbol")
    if not len(out):
        raise SymbolError("the akshare stock list held no CN A-share")
    return out.reset_index(drop=True)


def download_ak_sections(kind: str = "industry", ak=None, include_members: bool = True,
                         pause: float = 0.3,
                         section_filter: SectionFilter = None,
                         ) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """The 东财 industry / concept boards.

    Returns ``(sections, members)``: sections as ``name, kind, members``
    (members = the constituent count fetched, ``''`` when
    ``include_members=False``), members as ``section, symbol, code, name``
    (empty when ``include_members=False``).  A board whose constituents call
    fails is skipped with a warning, not fatal.
    """
    if kind not in ("industry", "concept"):
        raise SymbolError(f"unknown board kind {kind!r}")
    module = _module(ak)
    _bypass_proxy_for_eastmoney()
    boards = module.stock_board_industry_name_em() if kind == "industry" \
        else module.stock_board_concept_name_em()
    if boards is None or getattr(boards, "empty", True) \
            or _BOARD_NAME_COL not in boards.columns:
        raise SymbolError(f"akshare returned no {kind} board list")
    names = [str(n).strip() for n in boards[_BOARD_NAME_COL]
             if str(n).strip() and _wanted(str(n).strip(), section_filter)]
    if not names:
        raise SymbolError(f"no {kind} board passed the filter")

    cons_getter = module.stock_board_industry_cons_em if kind == "industry" \
        else module.stock_board_concept_cons_em
    member_rows = []
    for i, name in enumerate(names):
        rows = []
        if include_members:
            try:
                cons = cons_getter(symbol=name)
            except Exception as exc:           # noqa: BLE001 - per-board failures
                logger.warning("%s board %s constituents failed (%s) -- "
                               "skipped", kind, name, exc)
                cons = None
            if cons is not None and not getattr(cons, "empty", True):
                cons = cons.rename(columns=_CONS_MAP)
                if "code" in cons.columns:
                    codes = cons["code"].astype(str).str.strip().str.zfill(6)
                    labels = cons["name"].astype(str).str.strip() \
                        if "name" in cons.columns else pd.Series([""] * len(cons))
                    rows = [{"section": name, "code": code,
                             "symbol": to_canonical(code), "name": label}
                            for code, label in zip(codes, labels)
                            if to_canonical(code) is not None]
            if pause and i < len(names) - 1:
                time.sleep(pause)
        member_rows += rows
    members = pd.DataFrame(
        member_rows, columns=["section", "symbol", "code", "name"])
    if len(members):
        members = members.drop_duplicates(["section", "symbol"]) \
            .reset_index(drop=True)
    sections = pd.DataFrame({
        "name": names, "kind": kind,
        "members": [str(int((members["section"] == n).sum())) if include_members
                    else "" for n in names],
    }, columns=["name", "kind", "members"])
    logger.info("ak %s boards: %d sections, %d memberships", kind,
                len(sections), len(members))
    return sections, members
