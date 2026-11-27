"""``sync_symbols`` -- pull the catalogs into the on-disk store.

One call, every source, structured result::

    from qf_screen.symbols import SymbolStore, sync_symbols

    result = sync_symbols(data_dir="…", source="all")
    print(result.stocks, result.sections, result.errors)

``source`` picks ``"ak"`` (东财 boards via akshare), ``"lxg``" (company
list + 申万 industries via GeoScope), ``"local"`` (the offline import from
what is already under ``cn/lxg/`` -- no network, no key) or ``"all"``
(= lxg + ak).  A failing source records its error and the others still
land; nothing is written unless its download succeeded.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .ak_downloader import SectionFilter, download_ak_sections, download_ak_stocks
from .lxg_downloader import download_lxg_sections, download_lxg_stocks
from .store import SymbolStore

logger = logging.getLogger(__name__)

_SOURCES = ("ak", "lxg", "local", "all")


@dataclass
class SyncResult:
    """What a sync landed, per source -- counts, notes, errors."""

    data_dir: str
    market: str
    sources: List[str]
    started: str
    finished: str = ""
    #: source -> rows of the stock tables
    stocks: Dict[str, int] = field(default_factory=dict)
    #: "<source>:<kind>" -> rows of the section tables
    sections: Dict[str, int] = field(default_factory=dict)
    #: "<source>:<kind>" -> rows of the member tables
    members: Dict[str, int] = field(default_factory=dict)
    #: non-fatal notes (empty upstream endpoints, mirrors written ...)
    notes: List[str] = field(default_factory=list)
    #: one entry per FAILED source; empty means every source landed
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> Dict[str, object]:
        return {"data_dir": self.data_dir, "market": self.market,
                "sources": self.sources, "started": self.started,
                "finished": self.finished, "stocks": self.stocks,
                "sections": self.sections, "members": self.members,
                "notes": self.notes, "errors": self.errors, "ok": self.ok}


def sync_symbols(store: Optional[SymbolStore] = None, data_dir=None,
                 market: str = "cn", source: str = "all",
                 what: Sequence[str] = ("stocks", "sections"),
                 ak=None, client=None, api_key: Optional[str] = None,
                 include_members: bool = True, pause: float = 0.3,
                 section_filter: SectionFilter = None,
                 lxg_source: str = "sw_2021",
                 mirror_company_list: bool = True) -> SyncResult:
    """Refresh the symbol catalogs (see the module docstring).

    ``what`` picks which tables to sync (``stocks`` and/or ``sections``);
    ``ak`` / ``client`` inject the akshare module / GeoScope client (tests);
    the rest of the knobs pass through to the downloaders.
    """
    if source not in _SOURCES:
        raise ValueError(f"unknown source {source!r}; known: {', '.join(_SOURCES)}")
    todo = list(dict.fromkeys(
        ("lxg", "ak") if source == "all" else (source,)))
    wants = {part.strip().lower() for part in what}
    unknown = wants - {"stocks", "sections"}
    if unknown:
        raise ValueError(f"unknown --what {sorted(unknown)}; known: stocks, sections")
    store = store if store is not None else SymbolStore(data_dir, market)
    result = SyncResult(data_dir=str(store.data_dir), market=store.market,
                        sources=todo, started=time.strftime("%Y-%m-%d %H:%M:%S"))

    if "lxg" in todo:
        _sync_lxg(store, result, wants, api_key, client, include_members,
                  lxg_source, mirror_company_list)
    if "ak" in todo:
        _sync_ak(store, result, wants, ak, include_members, pause,
                 section_filter)
    if "local" in todo:
        counts = store.import_local_lxg()
        result.stocks["local"] = counts["stocks"]
        result.sections["local:industry"] = counts["sections"]
        result.members["local:industry"] = counts["members"]

    result.finished = time.strftime("%Y-%m-%d %H:%M:%S")
    landed = sum(result.stocks.values()) + sum(result.sections.values()) \
        + sum(result.members.values())
    logger.info("symbol sync %s: %d rows landed, %d error(s)",
                "+".join(todo), landed, len(result.errors))
    return result


def _fail(result: SyncResult, label: str, exc: Exception) -> None:
    logger.warning("symbol sync: %s failed (%s)", label, exc)
    result.errors.append(f"{label}: {exc}")


def _sync_lxg(store: SymbolStore, result: SyncResult, wants: set,
              api_key, client, include_members: bool, lxg_source: str,
              mirror: bool) -> None:
    if "stocks" in wants:
        try:
            raw = download_lxg_stocks(api_key=api_key, client=client)
            result.stocks["lxg"] = store.replace_stocks("lxg", raw)
            if mirror:
                path = store.mirror_company_list(raw)
                result.notes.append(f"company_list mirror: {path}")
        except Exception as exc:               # noqa: BLE001 - report, continue
            _fail(result, "lxg stocks", exc)
    if "sections" in wants:
        try:
            sections, members = download_lxg_sections(
                source=lxg_source, api_key=api_key,
                include_members=include_members, client=client)
            counts = store.replace_sections("lxg", "industry", sections, members)
            result.sections["lxg:industry"] = counts["sections"]
            result.members["lxg:industry"] = counts["members"]
            if not len(members):
                result.notes.append(
                    f"lxg {lxg_source} constituents came back empty upstream "
                    "-- sections landed without members")
        except Exception as exc:               # noqa: BLE001
            _fail(result, f"lxg {lxg_source} sections", exc)


def _sync_ak(store: SymbolStore, result: SyncResult, wants: set, ak,
             include_members: bool, pause: float,
             section_filter: SectionFilter) -> None:
    if "stocks" in wants:
        try:
            result.stocks["ak"] = store.replace_stocks(
                "ak", download_ak_stocks(ak=ak))
        except Exception as exc:               # noqa: BLE001
            _fail(result, "ak stocks", exc)
    if "sections" in wants:
        for kind in ("industry", "concept"):
            try:
                sections, members = download_ak_sections(
                    kind=kind, ak=ak, include_members=include_members,
                    pause=pause, section_filter=section_filter)
                counts = store.replace_sections("ak", kind, sections, members)
                result.sections[f"ak:{kind}"] = counts["sections"]
                result.members[f"ak:{kind}"] = counts["members"]
            except Exception as exc:           # noqa: BLE001
                _fail(result, f"ak {kind} boards", exc)
