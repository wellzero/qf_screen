"""``SymbolStore`` -- the on-disk stock & section (industry/concept/custom)
catalogs under ``<data_dir>/<market>/symbol/``.

Three source trees, one schema family::

    symbol/ak/stocks.csv                 symbol, code, name
    symbol/ak/industry_sections.csv      name, kind, members
    symbol/ak/industry_members.csv       section, symbol, code, name
    symbol/ak/concept_sections.csv       (same pair for the 东财 concept boards)
    symbol/ak/concept_members.csv
    symbol/lxg/stocks.csv                + exchange, list_date, listing_status,
                                          fs_type, connect, margin_eligible
    symbol/lxg/industry_sections.csv     name, kind, level, members
    symbol/lxg/industry_members.csv      section, symbol, code, name
    symbol/custom/stocks.csv             + note, added_at
    symbol/custom/sections.csv           name, kind, note, created_at, updated_at
    symbol/custom/members.csv            + added_at
    symbol/meta.json                     per-source row counts + sync times

``ak`` / ``lxg`` are written wholesale by :mod:`.sync` (a sync replaces the
snapshot); ``custom`` is the user's own layer (CRUD here, never touched by a
sync).  A *section* is identified by its name within a source; the same name
may exist in several sources and ``section_members`` then unions them.

Stock codes are normalized to the canonical ``SH600519`` spelling on every
write (:func:`quant_free.common.symbol_format.to_canonical`); ``code`` keeps
the bare 6-digit form for the downloaders' benefit.

Writes are atomic (tmp + replace), so a sync killed mid-write leaves the
previous catalog.  The store also knows how to *seed* itself offline via
:func:`SymbolStore.import_local_lxg` from the files that already exist
(``cn/lxg/company_list.csv`` and every ``cn/equity/<dir>/lxg/industries.csv``)
and to mirror an lxg company download back to ``cn/lxg/company_list.csv``
(exact original columns -- :func:`qf_screen.data.universe.build_universe`
keeps working and its mtime fingerprint invalidates itself).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from quant_free.common.const_filename import (
    EQUITY_DIR, PROVIDER_LXG, get_data_dir,
)
from quant_free.common.symbol_format import to_canonical

logger = logging.getLogger(__name__)

#: columns of the member tables (the section<->stock mapping), every source
_MEMBER_COLUMNS: Tuple[str, ...] = ("section", "symbol", "code", "name")
#: columns of the member tables that carry provenance (custom)
_MEMBER_STAMPED: Tuple[str, ...] = _MEMBER_COLUMNS + ("added_at",)
#: columns of the ak section tables
_AK_SECTION_COLUMNS: Tuple[str, ...] = ("name", "kind", "members")
#: columns of the lxg section tables (``level``: 1/2/3 of 申万)
_LXG_SECTION_COLUMNS: Tuple[str, ...] = ("name", "kind", "level", "members")
#: columns of the custom section table
_CUSTOM_SECTION_COLUMNS: Tuple[str, ...] = ("name", "kind", "note",
                                            "created_at", "updated_at")
#: minimal stock columns every source carries
_STOCK_COLUMNS: Tuple[str, ...] = ("symbol", "code", "name")
#: the lxg stock extras (the company-list fields worth keeping)
_LXG_STOCK_COLUMNS: Tuple[str, ...] = _STOCK_COLUMNS + (
    "exchange", "list_date", "listing_status", "fs_type", "connect",
    "margin_eligible",
)
#: the custom stock extras
_CUSTOM_STOCK_COLUMNS: Tuple[str, ...] = _STOCK_COLUMNS + ("note", "added_at")

#: kinds a custom section may take (an ak/lxg section is always one of the
#: first two; ``custom`` marks a plain basket)
SECTION_KINDS: Tuple[str, ...] = ("industry", "concept", "custom")

#: exact column order of ``cn/lxg/company_list.csv`` (the mirror contract of
#: ``universe._parse_company_list``)
_COMPANY_LIST_COLUMNS: Tuple[str, ...] = (
    "name", "market", "exchange", "areaCode", "stockCode", "fsTableType",
    "listingStatus", "ipoDate", "marginTradingAndSecuritiesLendingFlag",
    "mutualMarketFlag", "mutualMarkets",
)

_TRUTHY = frozenset({"true", "1", "t", "yes"})


class SymbolError(RuntimeError):
    """A symbol-catalog operation failed (bad symbol, unknown section,
    unavailable source ...)."""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _text(value) -> str:
    """``str(value)`` with pandas' missing sentinels mapped to ``''``."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):        # array-like: not a scalar
        return str(value)
    return str(value).strip()


def _truthy(value) -> bool:
    return _text(value).lower() in _TRUTHY


def _col(frame: pd.DataFrame, name: str) -> pd.Series:
    """Column ``name`` as-is, or a Series of ``''`` when the frame lacks it."""
    if name in frame.columns:
        return frame[name]
    return pd.Series([""] * len(frame), index=frame.index, dtype=object)


class SymbolStore:
    """Query + manage the stock / section catalogs of one market."""

    VERSION = 1

    def __init__(self, data_dir=None, market: str = "cn"):
        self.data_dir = Path(data_dir or get_data_dir())
        self.market = market
        self.root = self.data_dir / market / "symbol"

    def __repr__(self) -> str:
        return f"<SymbolStore {self.root}>"

    # ------------------------------------------------------------ plumbing
    @property
    def present(self) -> bool:
        """Whether any catalog file exists (drives cache fingerprints)."""
        if not self.root.is_dir():
            return False
        return any(self.root.rglob("*.csv")) or (self.root / "meta.json").exists()

    def fingerprint(self) -> str:
        """``<file count>:<mtime sum>`` over every catalog file -- cheap
        change detection for the universe cache."""
        total = count = 0
        if self.root.is_dir():
            paths = list(self.root.rglob("*.csv"))
            meta = self.root / "meta.json"
            if meta.exists():
                paths.append(meta)
            for path in paths:
                try:
                    total += os.stat(path).st_mtime_ns
                    count += 1
                except OSError:
                    continue
        return f"{count}:{total}"

    def _read(self, relative: str, columns: Tuple[str, ...]) -> pd.DataFrame:
        path = self.root / relative
        if not path.exists():
            return pd.DataFrame(columns=list(columns))
        try:
            frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
        except (OSError, ValueError) as exc:
            logger.warning("symbol catalog %s unreadable (%s) -- treated as "
                           "empty", path, exc)
            return pd.DataFrame(columns=list(columns))
        for column in columns:
            if column not in frame.columns:
                frame[column] = ""
        return frame[list(columns)].fillna("")

    def _write(self, relative: str, frame: pd.DataFrame) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        frame.to_csv(tmp, index=False, encoding="utf-8")
        tmp.replace(path)

    def _meta(self) -> Dict:
        try:
            doc = json.loads((self.root / "meta.json").read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                return doc
        except (OSError, ValueError):
            pass
        return {"version": self.VERSION, "sources": {}}

    def _note(self, source: str, what: str, rows: int, **extra) -> None:
        """Record a sync in ``meta.json`` (row count + timestamp)."""
        doc = self._meta()
        doc["version"] = self.VERSION
        entry = doc.setdefault("sources", {}).setdefault(source, {})
        entry[what] = {"rows": int(rows), "synced_at": _now(), **extra}
        path = self.root / "meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _canon(raw) -> str:
        canon = to_canonical(_text(raw))
        if canon is None:
            raise SymbolError(f"{raw!r} is not a CN A-share symbol")
        return canon

    # -------------------------------------------------------------- stocks
    def stocks(self, source: Optional[str] = None,
               query: Optional[str] = None) -> pd.DataFrame:
        """Stock rows (``symbol, code, name[, ...extras], source``).

        ``source`` picks one of ``ak`` / ``lxg`` / ``custom`` (default: all);
        ``query`` is a case-insensitive substring of the symbol, code or
        name.
        """
        schemas = (("ak", _STOCK_COLUMNS, "ak/stocks.csv"),
                   ("lxg", _LXG_STOCK_COLUMNS, "lxg/stocks.csv"),
                   ("custom", _CUSTOM_STOCK_COLUMNS, "custom/stocks.csv"))
        frames = []
        for name, columns, relative in schemas:
            if source is None or source == name:
                frames.append(self._read(relative, columns).assign(source=name))
        if not frames:
            raise SymbolError(f"unknown stock source {source!r}")
        out = pd.concat(frames, ignore_index=True).fillna("")
        if query:
            needle = str(query).strip().lower()
            haystack = (out["symbol"].str.lower() + " " + out["code"]
                        .str.lower() + " " + out["name"].str.lower())
            out = out[haystack.str.contains(needle, regex=False)]
        return out.reset_index(drop=True)

    def get_stock(self, symbol: str,
                  source: Optional[str] = None) -> Optional[Dict]:
        """One stock row (the last source that has it), or ``None``."""
        canon = self._canon(symbol)
        hit = self.stocks(source=source)
        hit = hit[hit["symbol"] == canon]
        return hit.iloc[-1].to_dict() if len(hit) else None

    def stock_names(self) -> Dict[str, str]:
        """symbol -> name, the freshest known (lxg wins, then ak, then
        custom); fills universe name gaps."""
        names: Dict[str, str] = {}
        for relative in ("ak/stocks.csv", "custom/stocks.csv",
                         "lxg/stocks.csv"):                 # lxg last: wins
            frame = self._read(relative, _STOCK_COLUMNS)
            for symbol, name in zip(frame["symbol"], frame["name"]):
                if symbol and name:
                    names[symbol] = name
        return names

    # ----------------------------------------------------- custom stocks
    def upsert_stock(self, symbol: str, name: str = "", note: str = "") -> Dict:
        """Add / update a stock in the ``custom`` layer."""
        canon = self._canon(symbol)
        frame = self._read("custom/stocks.csv", _CUSTOM_STOCK_COLUMNS)
        hit = frame.index[frame["symbol"] == canon]
        row = {"symbol": canon, "code": canon[2:], "name": _text(name),
               "note": _text(note),
               "added_at": frame.at[hit[0], "added_at"] if len(hit) else _now()}
        out = pd.concat([frame[frame["symbol"] != canon], pd.DataFrame(
            [row], columns=list(_CUSTOM_STOCK_COLUMNS))], ignore_index=True)
        out = out.sort_values("symbol", kind="stable").reset_index(drop=True)
        self._write("custom/stocks.csv", out)
        self._note("custom", "stocks", len(out))
        return row

    def remove_stock(self, symbol: str) -> int:
        """Drop a stock from the ``custom`` layer; returns 1 when it existed."""
        canon = self._canon(symbol)
        frame = self._read("custom/stocks.csv", _CUSTOM_STOCK_COLUMNS)
        keep = frame["symbol"] != canon
        if bool(keep.all()):
            return 0
        out = frame[keep].reset_index(drop=True)
        self._write("custom/stocks.csv", out)
        self._note("custom", "stocks", len(out))
        return 1

    # ------------------------------------------------------------ sections
    def _section_frames(self) -> List[Tuple[str, pd.DataFrame]]:
        """(label, sections frame) for every source, in a stable order."""
        out = []
        for kind in ("industry", "concept"):
            out.append((f"ak:{kind}", self._read(f"ak/{kind}_sections.csv",
                                                 _AK_SECTION_COLUMNS)))
        out.append(("lxg:industry", self._read("lxg/industry_sections.csv",
                                               _LXG_SECTION_COLUMNS)))
        out.append(("custom", self._read("custom/sections.csv",
                                         _CUSTOM_SECTION_COLUMNS)))
        return out

    def sections(self, kind: Optional[str] = None,
                 source: Optional[str] = None,
                 query: Optional[str] = None) -> pd.DataFrame:
        """Section rows (``name, kind, source, level, members, note``).

        ``kind`` filters industry/concept/custom; ``source`` picks
        ``ak`` / ``lxg`` / ``custom`` (default: all); ``query`` is a
        case-insensitive substring of the section name.  ``members`` is the
        constituent count at sync time (custom sections: counted live).
        """
        custom_counts = self._read("custom/members.csv",
                                   _MEMBER_COLUMNS)["section"].value_counts()
        frames = []
        for label, frame in self._section_frames():
            src = label.split(":")[0]
            if source is not None and src != source:
                continue
            piece = pd.DataFrame({
                "name": frame["name"], "kind": _col(frame, "kind"),
                "source": src, "level": _col(frame, "level"),
                "members": _col(frame, "members"), "note": _col(frame, "note"),
            })
            if src == "custom":
                piece["members"] = [int(custom_counts.get(n, 0))
                                    for n in piece["name"]]
            frames.append(piece)
        if not frames:
            raise SymbolError(f"unknown section source {source!r}")
        out = pd.concat(frames, ignore_index=True)
        if kind is not None:
            out = out[out["kind"] == kind]
        if query:
            needle = str(query).strip().lower()
            out = out[out["name"].str.lower().str.contains(needle, regex=False)]
        return out.reset_index(drop=True)

    def get_section(self, name: str,
                    source: Optional[str] = None) -> Optional[Dict]:
        """One section row (exact name), or ``None``."""
        hit = self.sections(source=source)
        hit = hit[hit["name"] == _text(name)]
        return hit.iloc[0].to_dict() if len(hit) else None

    def _member_frames(self) -> List[Tuple[str, pd.DataFrame, pd.Series]]:
        """(label, members frame, section name -> kind) per source."""
        out = []
        for kind in ("industry", "concept"):
            members = self._read(f"ak/{kind}_members.csv", _MEMBER_COLUMNS)
            sections = self._read(f"ak/{kind}_sections.csv", _AK_SECTION_COLUMNS)
            out.append((f"ak:{kind}", members, sections.set_index("name")["kind"]))
        members = self._read("lxg/industry_members.csv", _MEMBER_COLUMNS)
        sections = self._read("lxg/industry_sections.csv", _LXG_SECTION_COLUMNS)
        out.append(("lxg:industry", members,
                    sections.set_index("name")["kind"]))
        members = self._read("custom/members.csv", _MEMBER_STAMPED)
        sections = self._read("custom/sections.csv", _CUSTOM_SECTION_COLUMNS)
        out.append(("custom", members, sections.set_index("name")["kind"]))
        return out

    def section_members(self, name: str,
                        source: Optional[str] = None) -> List[str]:
        """Canonical symbols of every section named ``name`` (union across
        the sources; ``source`` restricts to ``ak`` / ``lxg`` / ``custom``)."""
        wanted = _text(name)
        symbols: List[str] = []
        for label, members, _kinds in self._member_frames():
            if source is not None and label.split(":")[0] != source:
                continue
            for symbol in members.loc[members["section"] == wanted, "symbol"]:
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
        return symbols

    def stock_sections(self, symbol: str,
                       source: Optional[str] = None) -> pd.DataFrame:
        """Every section ``symbol`` is a member of
        (``section, kind, source, code, name``)."""
        canon = self._canon(symbol)
        frames = []
        for label, members, kinds in self._member_frames():
            if source is not None and label.split(":")[0] != source:
                continue
            hit = members[members["symbol"] == canon]
            if not len(hit):
                continue
            frames.append(pd.DataFrame({
                "section": hit["section"],
                "kind": [_text(kinds.get(section, "")) for section in hit["section"]],
                "source": label.split(":")[0], "code": hit["code"],
                "name": hit["name"],
            }))
        if not frames:
            return pd.DataFrame(
                columns=["section", "kind", "source", "code", "name"])
        return pd.concat(frames, ignore_index=True)

    def membership(self, kind: Optional[str] = None) -> Dict[str, List[str]]:
        """section name -> member symbols in one pass over the catalogs
        (``kind`` filters by the section's kind)."""
        out: Dict[str, List[str]] = {}
        for label, members, kinds in self._member_frames():
            if kind is not None:
                wanted = set(kinds.index[kinds == kind])
                members = members[members["section"].isin(wanted)]
            for section, symbol in zip(members["section"], members["symbol"]):
                if not section or not symbol:
                    continue
                bucket = out.setdefault(section, [])
                if symbol not in bucket:
                    bucket.append(symbol)
        return out

    def board_frame(self, kind: str = "concept") -> pd.DataFrame:
        """Members of every ``kind`` section as the live client's shape
        (``board, code, symbol, name``) -- what
        :meth:`qf_screen.data.CNDataStore.board_members` serves offline.
        The lxg 申万 levels are NOT 东财 boards and stay out."""
        rows = []
        for label, members, kinds in self._member_frames():
            if label.split(":")[0] == "lxg":
                continue
            if kind is not None:
                wanted = set(kinds.index[kinds == kind])
                members = members[members["section"].isin(wanted)]
            rows += [{"board": section, "code": code, "symbol": symbol,
                      "name": name}
                     for section, code, symbol, name in zip(
                         members["section"], members["code"], members["symbol"],
                         members["name"])]
        return pd.DataFrame(rows, columns=["board", "code", "symbol", "name"])

    def industry_l1(self) -> Dict[str, str]:
        """symbol -> its 申万 L1 industry name from the lxg catalog
        (sections marked ``level == 1``); empty when the catalog carries no
        levels.  Fills universe ``industry_l1`` gaps without ``--live``."""
        sections = self._read("lxg/industry_sections.csv", _LXG_SECTION_COLUMNS)
        if not len(sections) or not (sections["level"] == "1").any():
            return {}
        wanted = set(sections.loc[sections["level"] == "1", "name"])
        members = self._read("lxg/industry_members.csv", _MEMBER_COLUMNS)
        hit = members[members["section"].isin(wanted)]
        out: Dict[str, str] = {}
        for symbol, section in zip(hit["symbol"], hit["section"]):
            out.setdefault(symbol, section)
        return out

    # ------------------------------------------------- custom section CRUD
    def upsert_section(self, name: str, kind: str = "custom",
                       note: str = "") -> Dict:
        """Create / update a CUSTOM section (``kind`` may be ``industry`` or
        ``concept`` to extend those pools offline)."""
        wanted = _text(name)
        if not wanted:
            raise SymbolError("a section needs a name")
        if kind not in SECTION_KINDS:
            raise SymbolError(f"unknown section kind {kind!r}; "
                              f"known: {', '.join(SECTION_KINDS)}")
        frame = self._read("custom/sections.csv", _CUSTOM_SECTION_COLUMNS)
        hit = frame.index[frame["name"] == wanted]
        row = {"name": wanted, "kind": kind, "note": _text(note),
               "created_at": frame.at[hit[0], "created_at"] if len(hit) else _now(),
               "updated_at": _now()}
        out = pd.concat([frame[frame["name"] != wanted], pd.DataFrame(
            [row], columns=list(_CUSTOM_SECTION_COLUMNS))], ignore_index=True)
        self._write("custom/sections.csv", out)
        self._note("custom", "sections", len(out))
        return row

    def delete_section(self, name: str) -> int:
        """Delete a CUSTOM section and its members (sync-managed ak/lxg
        sections are replaced by the next sync, not deleted by hand)."""
        wanted = _text(name)
        frame = self._read("custom/sections.csv", _CUSTOM_SECTION_COLUMNS)
        if not len(frame[frame["name"] == wanted]):
            managed = self.get_section(wanted)
            if managed is not None:
                raise SymbolError(
                    f"{wanted!r} is a {managed['source']} section managed by "
                    "sync -- re-sync to replace it, or pick another name")
            raise SymbolError(f"no custom section named {wanted!r}")
        sections = frame[frame["name"] != wanted].reset_index(drop=True)
        self._write("custom/sections.csv", sections)
        members = self._read("custom/members.csv", _MEMBER_STAMPED)
        gone = members["section"] == wanted
        self._write("custom/members.csv", members[~gone].reset_index(drop=True))
        self._note("custom", "sections", len(sections))
        return int(gone.sum())

    def add_section_members(self, name: str, symbols: Sequence[str]) -> int:
        """Add members to an EXISTING custom section; returns how many were
        new.  Codes that are not CN A-share symbols are rejected (a silent
        bad row would poison every later screen)."""
        wanted = _text(name)
        sections = self._read("custom/sections.csv", _CUSTOM_SECTION_COLUMNS)
        if not len(sections[sections["name"] == wanted]):
            managed = self.get_section(wanted)
            hint = (f" (the {managed['source']} section of that name is "
                    "sync-managed; members union across sources, but the "
                    "custom section needs its own name)"
                    if managed is not None else
                    " -- create it first (qf-screen symbol add-section)")
            raise SymbolError(f"no custom section named {wanted!r}{hint}")
        members = self._read("custom/members.csv", _MEMBER_STAMPED)
        have = set(members.loc[members["section"] == wanted, "symbol"])
        known = self.stock_names()
        rows, seen = [], set()
        for raw in symbols:
            canon = self._canon(raw)
            if canon in have or canon in seen:
                continue
            seen.add(canon)
            rows.append({"section": wanted, "symbol": canon, "code": canon[2:],
                         "name": known.get(canon, ""), "added_at": _now()})
        if rows:
            out = pd.concat([members, pd.DataFrame(
                rows, columns=list(_MEMBER_STAMPED))], ignore_index=True)
            self._write("custom/members.csv", out)
            self._note("custom", "members", len(out))
        return len(rows)

    def remove_section_members(self, name: str,
                               symbols: Sequence[str]) -> int:
        """Remove members from a custom section; returns how many went."""
        wanted = _text(name)
        members = self._read("custom/members.csv", _MEMBER_STAMPED)
        gone = set(self._canon(s) for s in symbols)
        drop = (members["section"] == wanted) & members["symbol"].isin(gone)
        if bool(drop.any()):
            self._write("custom/members.csv",
                        members[~drop].reset_index(drop=True))
            self._note("custom", "members", int((~drop).sum()))
        return int(drop.sum())

    # ------------------------------------------------------------ syncing
    def replace_stocks(self, source: str, frame: pd.DataFrame) -> int:
        """Swap in a fresh stock snapshot (``ak``: ``symbol, code, name``;
        ``lxg``: the raw company rows, normalized here).  Returns rows."""
        if source == "ak":
            rows = _normalize_stocks(frame)
            self._write("ak/stocks.csv", rows)
        elif source == "lxg":
            rows = _normalize_lxg_stocks(frame)
            self._write("lxg/stocks.csv", rows)
        else:
            raise SymbolError(f"cannot bulk-replace stocks of {source!r}")
        self._note(source, "stocks", len(rows))
        return len(rows)

    def replace_sections(self, source: str, kind: str, sections: pd.DataFrame,
                         members: pd.DataFrame) -> Dict[str, int]:
        """Swap in a fresh section snapshot (``ak`` industry/concept boards,
        ``lxg`` industries); returns ``{"sections": n, "members": n}``.
        ``sections`` needs a ``name`` column, ``members`` the
        ``section, symbol, code, name`` shape the downloaders produce."""
        if source == "ak" and kind in ("industry", "concept"):
            columns, tagged = _AK_SECTION_COLUMNS, False
        elif source == "lxg" and kind == "industry":
            columns, tagged = _LXG_SECTION_COLUMNS, True
        else:
            raise SymbolError(f"cannot bulk-replace {source!r} {kind!r} "
                              "sections (custom sections are managed by hand)")
        if "name" not in sections.columns:
            raise SymbolError("sections table lacks a name column")
        sec = pd.DataFrame({
            "name": sections["name"].map(_text), "kind": kind,
            **({"level": sections["level"].map(_text)} if tagged else {}),
            "members": _col(sections, "members").map(_text),
        }, columns=list(columns))
        for column in _MEMBER_COLUMNS:
            if column not in members.columns:
                raise SymbolError(f"members table lacks a {column!r} column")
        mem = members[list(_MEMBER_COLUMNS)].copy()
        mem["section"] = mem["section"].map(_text)
        mem = mem.drop_duplicates(["section", "symbol"]).reset_index(drop=True)
        sec["members"] = [str(int((mem["section"] == n).sum()))
                          for n in sec["name"]]
        where = (f"ak/{kind}" if source == "ak" else "lxg/industry")
        self._write(f"{where}_sections.csv", sec)
        self._write(f"{where}_members.csv", mem)
        self._note(source, f"{kind}_sections", len(sec))
        self._note(source, f"{kind}_members", len(mem))
        return {"sections": len(sec), "members": len(mem)}

    def mirror_company_list(self, frame: pd.DataFrame,
                            path: Optional[Path] = None) -> Path:
        """Write the raw lxg company rows back to ``cn/lxg/company_list.csv``
        (exact original columns; the universe cache invalidates itself via
        the file's new mtime)."""
        target = Path(path) if path is not None \
            else self.data_dir / self.market / PROVIDER_LXG / "company_list.csv"
        raw = frame.copy()
        for column in _COMPANY_LIST_COLUMNS:
            if column not in raw.columns:
                raw[column] = ""
        target.parent.mkdir(parents=True, exist_ok=True)
        raw[list(_COMPANY_LIST_COLUMNS)].to_csv(
            target, index=False, encoding="utf-8-sig")
        return target

    def import_local_lxg(self) -> Dict[str, int]:
        """Seed the lxg catalogs OFFLINE from what is already on disk:
        ``cn/lxg/company_list.csv`` -> ``lxg/stocks.csv``, and every
        ``cn/equity/<dir>/lxg/industries.csv`` -> the 申万 section/member
        tables.  No network, no key; returns what was imported."""
        list_path = self.data_dir / self.market / PROVIDER_LXG / "company_list.csv"
        raw = self._read_any(list_path)
        stocks = _normalize_lxg_stocks(raw) if raw is not None \
            else pd.DataFrame(columns=list(_LXG_STOCK_COLUMNS))
        self._write("lxg/stocks.csv", stocks)
        self._note("lxg", "stocks", len(stocks), via="local")

        sections: Dict[str, str] = {}          # name -> level
        rows = []
        equity_root = self.data_dir / self.market / EQUITY_DIR
        if equity_root.is_dir():
            with os.scandir(equity_root) as entries:
                dir_names = sorted(e.name for e in entries if e.is_dir())
            for dir_name in dir_names:
                canon = to_canonical(dir_name)
                if canon is None:
                    continue
                frame = self._read_any(
                    equity_root / dir_name / PROVIDER_LXG / "industries.csv")
                if frame is None:
                    continue
                levels = _sw_levels(frame)
                if levels is None:
                    continue
                name = _stock_name_of(raw, canon)
                for level, section in zip(("1", "2", "3"), levels):
                    if not section:
                        continue
                    sections.setdefault(section, level)
                    rows.append({"section": section, "symbol": canon,
                                 "code": canon[2:], "name": name})
        members = pd.DataFrame(rows, columns=list(_MEMBER_COLUMNS)) \
            .drop_duplicates(["section", "symbol"]) if rows \
            else pd.DataFrame(columns=list(_MEMBER_COLUMNS))
        names = sorted(sections)
        sections_frame = pd.DataFrame({
            "name": names, "kind": "industry",
            "level": [sections[n] for n in names],
            "members": [str(int((members["section"] == n).sum()))
                        for n in names],
        }, columns=list(_LXG_SECTION_COLUMNS))
        self._write("lxg/industry_sections.csv", sections_frame)
        self._write("lxg/industry_members.csv", members.reset_index(drop=True))
        self._note("lxg", "industry_sections", len(sections_frame), via="local")
        self._note("lxg", "industry_members", len(members), via="local")
        logger.info("local lxg import: %d stocks, %d 申万 sections, "
                    "%d memberships", len(stocks), len(sections_frame),
                    len(members))
        return {"stocks": len(stocks), "sections": len(sections_frame),
                "members": len(members)}

    @staticmethod
    def _read_any(path: Path) -> Optional[pd.DataFrame]:
        """A CSV anywhere in the data dir (dtype=str), or ``None``."""
        if not path.exists():
            return None
        try:
            return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
        except (OSError, ValueError) as exc:
            logger.warning("%s unreadable (%s) -- skipped", path, exc)
            return None

    # -------------------------------------------------------------- status
    def status(self) -> Dict[str, object]:
        """Coverage census for ``qf-screen symbol status``."""
        pieces = {
            "ak": (("stocks", "ak/stocks.csv", _STOCK_COLUMNS),
                   ("industry_sections", "ak/industry_sections.csv",
                    _AK_SECTION_COLUMNS),
                   ("industry_members", "ak/industry_members.csv",
                    _MEMBER_COLUMNS),
                   ("concept_sections", "ak/concept_sections.csv",
                    _AK_SECTION_COLUMNS),
                   ("concept_members", "ak/concept_members.csv",
                    _MEMBER_COLUMNS)),
            "lxg": (("stocks", "lxg/stocks.csv", _LXG_STOCK_COLUMNS),
                    ("industry_sections", "lxg/industry_sections.csv",
                     _LXG_SECTION_COLUMNS),
                    ("industry_members", "lxg/industry_members.csv",
                     _MEMBER_COLUMNS)),
            "custom": (("stocks", "custom/stocks.csv", _CUSTOM_STOCK_COLUMNS),
                       ("sections", "custom/sections.csv",
                        _CUSTOM_SECTION_COLUMNS),
                       ("members", "custom/members.csv", _MEMBER_STAMPED)),
        }
        notes = self._meta()["sources"]
        out: Dict[str, object] = {"root": str(self.root),
                                  "present": self.present,
                                  "fingerprint": self.fingerprint(),
                                  "sources": {}}
        for source, items in pieces.items():
            entry = {}
            for what, relative, columns in items:
                note = notes.get(source, {}).get(what) or {}
                entry[what] = {"rows": len(self._read(relative, columns)),
                               "synced_at": note.get("synced_at")}
            out["sources"][source] = entry                # type: ignore[index]
        return out


# ----------------------------------------------------------------- helpers
def _normalize_stocks(frame: pd.DataFrame) -> pd.DataFrame:
    """Any (symbol|code), name table -> the canonical ``symbol, code, name``."""
    source = frame.copy()
    source.columns = [str(c).strip() for c in source.columns]
    if "symbol" in source.columns:
        symbols = [to_canonical(s) for s in source["symbol"].map(_text)]
    elif "code" in source.columns:
        symbols = [to_canonical(c.zfill(6)) for c in source["code"].map(_text)]
    else:
        raise SymbolError("stock table lacks both symbol and code columns")
    rows = pd.DataFrame({
        "symbol": symbols,
        "code": [s[2:] if s else "" for s in symbols],
        "name": _col(source, "name").map(_text).tolist(),
    }, columns=list(_STOCK_COLUMNS))
    return rows[rows["symbol"].notna()].drop_duplicates(
        "symbol").reset_index(drop=True)


def _normalize_lxg_stocks(frame: pd.DataFrame) -> pd.DataFrame:
    """Raw lxg company rows -> the ``lxg/stocks.csv`` schema (A-shares only,
    canonical symbols, ``ipoDate`` -> ``list_date`` date part)."""
    if frame is None or not len(frame):
        return pd.DataFrame(columns=list(_LXG_STOCK_COLUMNS))
    raw = frame.reset_index(drop=True)
    raw.columns = [str(c).strip() for c in raw.columns]
    if "stockCode" not in raw.columns:
        raise SymbolError("lxg company rows lack stockCode")
    market = _col(raw, "market").str.lower()
    codes = _col(raw, "stockCode").astype(str).str.strip().str.replace(
        r"\.0$", "", regex=True).str.zfill(6)
    exchange = _col(raw, "exchange").str.lower()
    symbols = [to_canonical(f"{ex}{code}") if ex else to_canonical(code)
               for ex, code in zip(exchange, codes)]
    out = pd.DataFrame({
        "symbol": symbols,
        "code": codes.tolist(),
        "name": _col(raw, "name").map(_text).tolist(),
        "exchange": exchange.tolist(),
        "list_date": [d[:10] for d in _col(raw, "ipoDate").map(_text)],
        "listing_status": _col(raw, "listingStatus").map(_text).tolist(),
        "fs_type": _col(raw, "fsTableType").map(_text).tolist(),
        "connect": [_truthy(v) for v in _col(raw, "mutualMarketFlag")],
        "margin_eligible": [_truthy(v) for v in
                            _col(raw, "marginTradingAndSecuritiesLendingFlag")],
    }, columns=list(_LXG_STOCK_COLUMNS))
    keep = out["symbol"].notna() & market.eq("a").to_numpy()
    return out[keep.to_numpy()].drop_duplicates(
        "symbol").reset_index(drop=True)


def _sw_levels(frame: pd.DataFrame) -> Optional[Tuple[str, str, str]]:
    """(L1, L2, L3) 申万 names of a per-symbol ``industries.csv`` -- mirrors
    :func:`qf_screen.data.universe._sw_levels` (kept private there, and this
    package must not import it: ``universe`` imports THIS package lazily)."""
    if frame is None or "source" not in frame.columns \
            or "name" not in frame.columns:
        return None
    sources = [str(s).strip() for s in frame["source"]]
    names = [str(n).strip() for n in frame["name"]]
    for source in ("sw_2021", "sw"):
        rows = [i for i, s in enumerate(sources) if s == source]
        if len(rows) >= 3:
            return (names[rows[0]], names[rows[1]], names[rows[2]])
    return None


def _stock_name_of(raw: Optional[pd.DataFrame], symbol: str) -> str:
    """The name of ``symbol`` in raw company rows (import_local_lxg helper)."""
    if raw is None or "name" not in raw.columns \
            or "stockCode" not in raw.columns:
        return ""
    codes = raw["stockCode"].astype(str).str.strip().str.replace(
        r"\.0$", "", regex=True).str.zfill(6)
    hit = raw[codes == symbol[2:]]
    return _text(hit["name"].iloc[0]) if len(hit) else ""
