"""Fingerprinted parquet cache for wide ``date x symbol`` tables.

The alt-data / dividend / factor-bridge / price-feature builders cost real
I/O or real arithmetic on EVERY process that touches the feature.  This
module gives them a shared, boring cache under
``<data_dir>/<market>/qf_screen_cache/``:

* one parquet per ``key`` (the wide ``date x symbol`` table) plus a
  ``.meta.txt`` sidecar holding the fingerprint;
* the fingerprint is whatever the caller derives from its SOURCE files
  (typically ``"<file count>:<mtime sum>:<calendar bounds>"``) — a mismatch
  is a miss and the table is rebuilt;
* writes are atomic (``tmp`` + ``replace``) so a killed process cannot
  leave a half-written cache behind;
* every failure is a silent miss: a cache only ever costs speed.

The cache only ever grows by use, so it has a garbage collector
(:func:`cache_gc_plan`, behind ``qf-screen cache clear --stale /
--older-than / --max-gb``): what no run will read again goes (another
version's layout, an interrupted write, a merged-store cache whose source
census moved or that is incomplete -- and the derived tables of such a
window, which are aligned to it), then what has not been used for a
while, then the least recently used until a budget is met.  The daily
window's fresh caches are never touched by ``--stale``.

**Layout.**  A wide table is stored as ONE flat parquet column (row-major
``date x symbol`` values) with the dates and symbols in the schema
metadata.  Parquet is column-major and a screener table has ~600 rows and
~5,500 columns: in the row-per-date layout every one of those columns is a
separate 600-value chunk, 0.12 s to read ONE 16 MB table (measured
2026-11-26) — ~1 s for the 16 store tables and most of a warm run.  As a
single column the same table reads in ~10 ms (see :func:`write_wide` /
:func:`read_wide`).  A file without the layout tag is a miss, so nothing
written by an older version is ever misread.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: sidecar format version — bump to invalidate every derived-table cache
#: at once.  v2 added the warning lines the build replays on a hit; v3
#: names the feature in the staleness warning (the replayed text is part
#: of what the cache serves, so rewording it is a format change); v4 is
#: the flat single-column parquet layout.
_META_VERSION = "v4"

#: layout tag + axis lists, kept in the parquet schema metadata (a file
#: without the tag is a miss)
_LAYOUT = "qf_screen.wide.flat.v1"
_LAYOUT_KEY = b"qf_screen_layout"
_DATES_KEY = b"qf_screen_dates"
_SYMBOLS_KEY = b"qf_screen_symbols"


def cache_dir_for(data_dir, market: str) -> Path:
    """The qf_screen cache directory under the store (created lazily)."""
    return Path(data_dir) / market / "qf_screen_cache"


def fingerprint(files: list, extra: str = "") -> str:
    """``"<count>:<mtime sum>[:extra]"`` over the source file list — the
    cheap staleness signal every derived-table cache uses.

    The SUM of the files' nanosecond mtimes, not the newest one: any
    rewrite moves the sum, including one that leaves the file older than
    the list's newest file (an archive extracted with its timestamps, a
    repair that copies an old file over a new one) -- which a maximum
    would not notice.  An absent path counts for nothing.  Plain
    ``os.stat``: pathlib's ``Path.stat`` re-parses the path on every call
    and cost ~5x more, which at ~20,000 source files was 0.2 s of every
    warm run.
    """
    present, total = 0, 0
    for path in files:
        if path is None:
            continue
        try:
            total += os.stat(path).st_mtime_ns
        except OSError:
            continue
        present += 1
    fp = f"{present}:{total}"
    return f"{fp}:{extra}" if extra else fp


def derived_fingerprint(files: list, calendar, symbols, extra: str = "") -> str:
    """``fingerprint`` over ``files`` + the calendar bounds + the SYMBOL-LIST
    hash (+ ``extra``) -- what every derived WIDE table keys on.

    The symbol hash is load-bearing: the price files update intraday (a
    store that loaded 5,473 names at 09:00 may load 5,472 at 15:00) while
    an alt source's own mtimes stand still -- without it a cached wide
    table can come back with yesterday's columns and silently misalign
    every cross-section built on it.
    """
    import hashlib

    digest = hashlib.sha1("|".join(symbols).encode()).hexdigest()[:10]
    return fingerprint(files, extra=f"{len(calendar)}:{calendar[0].date()}:"
                                    f"{calendar[-1].date()}:{digest}:{extra}")


# ------------------------------------------------------------------ codec
def write_wide(path, table: pd.DataFrame) -> None:
    """Write a wide ``date x symbol`` frame in the flat single-column layout
    (float32 stays float32).  A table with no rows or no columns is not
    written — an empty table is never a valid cache entry."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if table.empty or not isinstance(table.index, pd.DatetimeIndex):
        raise ValueError("write_wide needs a non-empty date x symbol frame")
    values = np.ascontiguousarray(table.to_numpy())
    if values.dtype.kind not in "fc":
        values = values.astype(float)
    dates = pd.DatetimeIndex(table.index).strftime("%Y-%m-%d").tolist()
    symbols = [str(column) for column in table.columns]
    arrow = pa.Table.from_arrays(
        [pa.array(values.reshape(-1))], names=["v"],
        metadata={_LAYOUT_KEY: _LAYOUT.encode(),
                  _DATES_KEY: json.dumps(dates).encode(),
                  _SYMBOLS_KEY: json.dumps(symbols).encode()})
    pq.write_table(arrow, path, compression="lz4")


def read_wide(path, index=None, columns=None) -> Optional[pd.DataFrame]:
    """The frame :func:`write_wide` wrote, or ``None`` when absent, not in
    this layout, unreadable, or (when ``index`` / ``columns`` are given) not
    aligned to them.

    The values are NOT copied or transposed by hand: the frame's single
    block is the column's own buffer seen as ``date x symbol``, so reading
    a 618 x 5,472 table costs the file read and nothing else.
    """
    try:
        import pyarrow.parquet as pq

        arrow = pq.read_table(path)
        meta = arrow.schema.metadata or {}
        if meta.get(_LAYOUT_KEY) != _LAYOUT.encode():
            return None
        dates = json.loads(meta[_DATES_KEY])
        symbols = json.loads(meta[_SYMBOLS_KEY])
        if columns is not None and symbols != [str(c) for c in columns]:
            return None
        calendar = pd.DatetimeIndex(dates, name="date")
        if index is not None and not calendar.equals(pd.DatetimeIndex(index)):
            return None
        rows, cols = len(dates), len(symbols)
        column = arrow.column(0)
        if rows == 0 or cols == 0 or len(column) != rows * cols:
            return None
        values = column.to_numpy(zero_copy_only=False).reshape(rows, cols)
    except Exception:  # noqa: BLE001 - a corrupt cache is a miss, not an error
        return None
    if not values.flags.writeable:                 # a buffer over the file
        values = values.copy()
    return pd.DataFrame(values, index=calendar, columns=pd.Index(symbols))


def read_cached_table(path: Path, fp: str):
    """A cached wide table whose fingerprint matches, else ``None``.

    Returns ``(table, warnings)`` — the sidecar's fingerprint line plus any
    WARNING lines the build recorded (source-staleness notes that must
    replay on a cache hit, not just on the build that discovered them).
    """
    if not path.exists():
        return None
    meta = path.with_name(path.name + ".meta.txt")
    try:
        lines = meta.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0].strip() != f"{_META_VERSION}:{fp}":
            return None
        table = read_wide(path)
        if table is None or table.empty:
            return None
        return table, [ln for ln in lines[1:] if ln.strip()]
    except Exception:  # noqa: BLE001 - a corrupt cache is a miss, not an error
        return None


def write_cached_table(path: Path, fp: str, table: pd.DataFrame,
                       warnings: Optional[list] = None) -> None:
    """Persist ``table`` + fingerprint atomically (best effort).

    The parquet is replaced BEFORE the sidecar is rewritten: interrupted
    between the two, the sidecar names an older fingerprint than the file
    it describes, which is a miss and a rebuild -- never a stale hit.
    """
    if table.empty:
        return
    try:
        import pyarrow  # noqa: F401 - parquet engine; skip the write if absent

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        write_wide(tmp, table)
        tmp.replace(path)
        body = f"{_META_VERSION}:{fp}"
        if warnings:
            body += "\n" + "\n".join(warnings)
        meta = path.with_name(path.name + ".meta.txt")
        meta_tmp = meta.with_name(meta.name + ".tmp")
        meta_tmp.write_text(body, encoding="utf-8")
        meta_tmp.replace(meta)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache write failed for %s (%s) -- continuing without",
                       path.name, exc)


def cached_wide_table(directory: Path, key: str, fp: str,
                      build: Callable[[], pd.DataFrame],
                      warnings_sink: Optional[list] = None,
                      use_cache: bool = True) -> pd.DataFrame:
    """Read-through cache of one derived wide table.

    ``key`` names the parquet file (``<key>.parquet``); ``build`` runs only
    on a miss.  The build result is cached whatever it is (an all-NaN table
    is a valid cached result — it means "no such data on disk").  Warnings
    the build appended to ``warnings_sink`` are persisted in the sidecar
    and re-appended on every cache HIT (staleness is a property of the
    data, not of the process that happened to build the table).

    ``use_cache=False`` (``--no-cache``) skips the cache BOTH ways: the
    table is built from source and nothing is read or written, so the flag
    really does force source truth for every feature a screen touches.
    """
    if not use_cache:
        return build()
    path = directory / f"{key}.parquet"
    cached = read_cached_table(path, fp)
    if cached is not None:
        table, replay = cached
        if warnings_sink is not None:
            warnings_sink.extend(replay)
        logger.debug("table cache hit: %s", path.name)
        return table
    before = len(warnings_sink) if warnings_sink is not None else 0
    table = build()
    write_cached_table(path, fp, table,
                       warnings_sink[before:] if warnings_sink is not None else None)
    return table


# ------------------------------------------------------------- inventory
#: cache entry kinds by file-name prefix (first match wins)
KINDS: Tuple[Tuple[str, str], ...] = (
    ("store", "store_tail"), ("history", "store_hist"), ("universe", "universe_"),
    ("feature", "feature_"), ("statement", "finance_"), ("dividend", "div_yield_"),
    ("alt", "alt_"), ("factor", "factor_"))
#: what each kind holds, for the ``cache status`` listing
KIND_LABELS: Dict[str, str] = {
    "store": "merged store tables (the daily snapshot window)",
    "history": "merged store tables (a pit / validate date window)",
    "universe": "universe census (dirs, names, industries)",
    "feature": "price / volume feature tables",
    "statement": "lxg statement fields (payload + step tables)",
    "dividend": "dividend-yield tables", "alt": "lxg / ak alternative-data tables",
    "factor": "factor-bridge tables", "live": "akshare --live responses",
    "tmp": "interrupted writes",
    "obsolete": "an older qf_screen layout's files (never read; `cache clear "
                "--what obsolete`)",
    "other": "unrecognised",
}
#: derived-table kinds: since v0.3 these live under ``tables/<window>/``; a
#: file of one of these kinds at the top level is an older layout's
_DERIVED_KINDS = frozenset({"feature", "dividend", "alt", "factor"})
#: the one statement entry that legitimately stays at the top level (it is
#: window-independent): ``finance_payload_<symbol key>``
_WINDOW_FREE = re.compile(r"^finance_payload_[0-9a-f]+\.parquet$")
_STORE_VERSION = re.compile(r"_v(\d+)$")
#: a merged-store cache directory's name, and the window it serves:
#: ``store_<window key>_v<version>`` (the key is what ``tables/<window>``
#: is named after -- the two belong together)
_STORE_NAME = re.compile(r"^store_(.+)_v\d+$")

#: the ``--live`` akshare cache lives next to the qf_screen cache
LIVE_DIR = "ak_live"
#: derived feature tables live under ``<cache>/tables/<window>/``: a table is
#: aligned to ONE store window (its calendar and symbol list), and the daily
#: snapshot and a validation window are different ones.  Sharing a file per
#: key made the two modes evict each other -- every ``validate`` left the
#: next morning's ``run`` a 12 s rebuild.
TABLES_DIR = "tables"
_SIDECARS = (".meta.txt", ".meta.json")


def kind_of(name: str) -> str:
    """The cache kind a file / directory name belongs to (``"other"`` for
    anything this package did not write)."""
    for kind, prefix in KINDS:
        if name.startswith(prefix):
            return kind
    return "other"


def live_cache_dir(data_dir, market: str) -> Path:
    return Path(data_dir) / market / LIVE_DIR


def _file_kind(path: Path, root: Path) -> Tuple[str, str]:
    """``(kind, entry name)`` of one cache file: a table and its sidecar(s)
    share the entry name."""
    name = path.name
    if name.endswith(".tmp"):
        return "tmp", name
    for suffix in _SIDECARS:                     # "<key>.parquet.meta.txt" ...
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    name = name.rsplit(".", 1)[0] if "." in name else name
    return kind_of(name), name


def _current_version_tag() -> str:
    from qf_screen import __version__

    return "v" + __version__.replace(".", "")


def _obsolete(kind: str, name: str, is_dir: bool) -> bool:
    """Whether a top-level cache entry was written by a layout this version
    never reads: a merged-store cache of another version, or a derived
    table file outside ``tables/`` (every derived table moved under its
    window in v0.3)."""
    if is_dir:
        if kind not in ("store", "history"):
            return False
        match = _STORE_VERSION.search(name)
        return match is None or match.group(1) != _current_version_tag()[1:]
    if _WINDOW_FREE.match(name + ".parquet"):
        return False
    return kind in _DERIVED_KINDS or kind == "statement"


def _iter_cache(data_dir, market: str):
    """Yield ``(kind, entry, path)`` for everything in both cache
    directories.

    ``entry`` is the name :func:`cache_inventory` groups by: a merged-store
    cache yields its DIRECTORY once (``path`` is that directory); a derived
    table is named ``<window>/<key>``; a live response ``live:<key>``.  An
    entry of a layout this version cannot read is reported as ``obsolete``.
    """
    for root in (cache_dir_for(data_dir, market), live_cache_dir(data_dir, market)):
        if not root.is_dir():
            continue
        live = root.name == LIVE_DIR
        for path in sorted(root.iterdir()):
            if path.is_dir():
                if path.name == TABLES_DIR:
                    for window in sorted(path.iterdir()):
                        if not window.is_dir():
                            continue
                        for inner in sorted(window.iterdir()):
                            if inner.is_file():
                                kind, name = _file_kind(inner, window)
                                yield kind, f"{window.name}/{name}", inner
                else:
                    kind = kind_of(path.name)
                    if _obsolete(kind, path.name, True):
                        kind = "obsolete"
                    yield kind, path.name, path
            elif path.is_file():
                kind, name = _file_kind(path, root)
                if not live and _obsolete(kind, name, False):
                    kind = "obsolete"
                yield ("live" if live else kind), ("live:" if live else "") + name, path


def _stat(path: Path):
    try:
        return path.stat()
    except OSError:
        return None


def cache_inventory(data_dir, market: str) -> List[Dict[str, object]]:
    """One row per cache entry under ``qf_screen_cache/`` (and the live
    akshare cache beside it): ``kind``, ``name``, ``bytes``, ``files``,
    ``mtime`` (newest file written), ``atime`` (newest file read) and
    ``age_days`` (since it was written).

    A store cache is a DIRECTORY (its 16 wide tables + provenance +
    ``meta.json``); everything else is a table file with its sidecar, the
    derived tables named ``<window>/<key>``.  Leftover ``*.tmp`` files of
    an interrupted write are counted under ``"tmp"``.
    """
    entries: Dict[Tuple[str, str], List] = {}

    def _add(kind: str, name: str, size: int, mtime: float, atime: float) -> None:
        entry = entries.setdefault((kind, name), [kind, name, 0, 0, 0.0, 0.0])
        entry[2] += size
        entry[3] += 1
        entry[4] = max(entry[4], mtime)
        entry[5] = max(entry[5], atime)

    for kind, name, path in _iter_cache(data_dir, market):
        if path.is_dir():                        # a store cache: all one entry
            for inner in sorted(path.iterdir()):
                if inner.is_file() and (stat := _stat(inner)):
                    _add(kind, name, stat.st_size, stat.st_mtime, stat.st_atime)
        elif (stat := _stat(path)) is not None:
            _add(kind, name, stat.st_size, stat.st_mtime, stat.st_atime)
    rows = []
    for (kind, name), (_kind, _name, size, files, mtime, atime) in entries.items():
        rows.append({
            "kind": kind, "name": name, "bytes": size, "files": files,
            "mtime": pd.Timestamp(mtime, unit="s") if mtime else pd.NaT,
            "atime": pd.Timestamp(atime, unit="s") if atime else pd.NaT,
            "age_days": round((time.time() - mtime) / 86400.0, 2) if mtime else None})
    return sorted(rows, key=lambda e: (str(e["kind"]), str(e["name"])))


def entry_last_use(entry: Dict[str, object]) -> float:
    """When an entry was last used, in epoch seconds: the newer of its
    last write and its last read.

    Read times come from the filesystem's ``atime``, which a ``relatime``
    mount refreshes at most once a day and a ``noatime`` mount never -- so
    on such a mount "not used for N days" degrades to "not written for N
    days", which is what the daily window's caches are anyway.
    """
    stamps = []
    for key in ("mtime", "atime"):
        stamp = entry.get(key)
        if stamp is not None and not pd.isna(stamp):
            stamps.append(pd.Timestamp(stamp).timestamp())
    return max(stamps) if stamps else 0.0


def cache_summary(inventory: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Inventory rolled up per kind: entries, files, bytes, newest and
    oldest entry — one row per kind, biggest first."""
    rolled: Dict[str, Dict[str, object]] = {}
    for entry in inventory:
        row = rolled.setdefault(str(entry["kind"]), {
            "kind": entry["kind"], "entries": 0, "files": 0, "bytes": 0,
            "newest": pd.NaT, "oldest": pd.NaT})
        row["entries"] = int(row["entries"]) + 1
        row["files"] = int(row["files"]) + int(entry["files"])
        row["bytes"] = int(row["bytes"]) + int(entry["bytes"])
        stamp = entry["mtime"]
        if pd.isna(stamp):
            continue
        row["newest"] = stamp if pd.isna(row["newest"]) or stamp > row["newest"] \
            else row["newest"]
        row["oldest"] = stamp if pd.isna(row["oldest"]) or stamp < row["oldest"] \
            else row["oldest"]
    return sorted(rolled.values(), key=lambda r: -int(r["bytes"]))


def window_summary(inventory: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """The derived tables rolled up per store window (``<window>/<key>``
    entries): entries, bytes and the newest write — one row per window."""
    rolled: Dict[str, Dict[str, object]] = {}
    for entry in inventory:
        name = str(entry["name"])
        if "/" not in name:
            continue
        window = name.split("/", 1)[0]
        row = rolled.setdefault(window, {"window": window, "entries": 0, "bytes": 0,
                                         "newest": pd.NaT})
        row["entries"] = int(row["entries"]) + 1
        row["bytes"] = int(row["bytes"]) + int(entry["bytes"])
        stamp = entry["mtime"]
        if not pd.isna(stamp) and (pd.isna(row["newest"]) or stamp > row["newest"]):
            row["newest"] = stamp
    return sorted(rolled.values(), key=lambda r: -int(r["bytes"]))


def _store_state(directory: Path, current: Optional[Tuple[str, str]]) -> Tuple[str, Dict]:
    """``(verdict, meta)`` of a merged-store cache directory.

    ``broken``: its ``meta.json`` or one of its tables cannot be read, so
    no run will ever hit it.  ``stale``: built from a source census that
    is no longer the current one (``current`` is ``(fingerprint, symbol
    key)`` of the default universe), so the next run of its window
    rebuilds it in place.  ``fresh``: the census matches.  ``unjudged``:
    built for another symbol set, or no census to compare with.
    """
    try:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "broken", {}
    if not isinstance(meta, dict) or not meta.get("fingerprint") or not meta.get("calendar") \
            or not meta.get("symbols"):
        return "broken", meta if isinstance(meta, dict) else {}
    if not (directory / "provenance.parquet").exists() or not any(
            directory.glob("wide_*.parquet")):
        return "broken", meta
    match = _STORE_NAME.match(directory.name)
    if current is None or match is None or match.group(1).rsplit("_", 1)[-1] != current[1]:
        return "unjudged", meta
    return ("fresh" if meta.get("fingerprint") == current[0] else "stale"), meta


#: why an entry was picked, in the order the collector applies them
GC_REASONS = ("stale", "old", "lru")


def _idle_text(days: float) -> str:
    """``40 days`` / ``3 hours`` -- an idle time in the unit that reads."""
    return f"{days:.0f} days" if days >= 1 else f"{days * 24:.0f} hours"


def cache_gc_plan(data_dir, market: str, kinds: Optional[Sequence[str]] = None, *,
                  stale: bool = False, older_than: Optional[float] = None,
                  max_gb: Optional[float] = None,
                  current: Optional[Tuple[str, str]] = None,
                  now: Optional[float] = None) -> Dict[str, object]:
    """What ``cache clear`` would remove, and why.

    ``kinds`` narrows the candidates (``None`` / ``"all"``: everything in
    both cache directories).  Three selectors, applied in this order and
    combined:

    * ``stale`` -- what no run will read again: another version's layout,
      an interrupted write, a merged-store cache that is broken or was
      built from a source census that has since moved, and the derived
      tables of every window all of whose store caches are gone bad (they
      are aligned to it).  A window's store cache is judged against
      ``current`` -- ``(fingerprint, symbol key)`` of the default
      universe, :func:`qf_screen.cache_ops.default_fingerprint`; without
      it, and for a cache built on another symbol set, nothing is judged
      and the plan says so.  The fresh daily window is never touched.
    * ``older_than`` (days) -- entries not used for that long, last use
      being :func:`entry_last_use`.
    * ``max_gb`` -- with the above gone, the least recently used entries
      until what is left fits ``max_gb`` (``1024**3`` bytes, the unit
      ``cache status`` prints).

    Returns ``{"remove": rows each with a ``reason`` (``stale`` / ``old`` /
    ``lru``), the ``detail`` behind it and the ``order`` in which the
    collector picked it (least recently used first, within ``lru``); ``keep``:
    the rows that stay; ``total_bytes``, ``after_bytes``, ``counts``: entries
    per reason; ``notes``}.
    """
    now = time.time() if now is None else float(now)
    wanted = None if not kinds or "all" in kinds else set(kinds)
    inventory = cache_inventory(data_dir, market)
    scope = [row for row in inventory if wanted is None or row["kind"] in wanted]
    picked: Dict[Tuple[str, str], Tuple[str, str, int]] = {}
    notes: List[str] = []

    def pick(row: Dict[str, object], reason: str, detail: str) -> None:
        key = (str(row["kind"]), str(row["name"]))
        if key not in picked:                         # the first selector to pick wins
            picked[key] = (reason, detail, len(picked))

    if stale:
        for row in scope:
            if row["kind"] == "obsolete":
                pick(row, "stale", "an older version's layout -- never read")
            elif row["kind"] == "tmp":
                pick(row, "stale", "an interrupted write")
        judged: Dict[str, List[str]] = {}
        unjudged = 0
        for row in inventory:            # every merged-store cache, in scope or not:
            if row["kind"] not in ("store", "history", "obsolete"):   # a window's state is a fact
                continue
            match = _STORE_NAME.match(str(row["name"]))
            if match is None:
                continue
            verdict, _meta = _store_state(cache_dir_for(data_dir, market) / str(row["name"]),
                                          current)
            judged.setdefault(match.group(1), []).append(verdict)
            if row not in scope:
                continue
            if verdict == "stale":
                pick(row, "stale", "a source file changed since it was built")
            elif verdict == "broken":
                pick(row, "stale", "incomplete -- its meta or wide tables are missing")
            elif verdict == "unjudged":
                unjudged += 1
        # a window whose every store cache is gone bad: its tables are aligned to it
        gone = {window for window, verdicts in judged.items()
                if all(verdict in ("stale", "broken") for verdict in verdicts)}
        for row in scope:
            window = str(row["name"]).split("/", 1)[0] if "/" in str(row["name"]) else None
            if window in gone:
                pick(row, "stale", "its window's store cache is stale")
        if current is None:
            notes.append("no current source census: merged-store caches were not judged "
                         "(only other versions' layouts, interrupted writes and incomplete "
                         "caches go)")
        elif unjudged:
            notes.append(f"{unjudged} merged-store cache(s) were built for another symbol set "
                         "and were not judged")
    if older_than is not None:
        for row in scope:
            idle = (now - entry_last_use(row)) / 86400.0
            if idle > float(older_than):
                pick(row, "old", f"not used for {_idle_text(idle)}")
    total = sum(int(row["bytes"]) for row in scope)
    after = total - sum(int(row["bytes"]) for row in scope
                        if (str(row["kind"]), str(row["name"])) in picked)
    if max_gb is not None:
        budget = float(max_gb) * 1024 ** 3
        if after > budget:
            rest = sorted((row for row in scope
                           if (str(row["kind"]), str(row["name"])) not in picked),
                          key=entry_last_use)
            for row in rest:
                if after <= budget:
                    break
                pick(row, "lru", f"least recently used (to fit {max_gb:g} GB)")
                after -= int(row["bytes"])
    remove, keep = [], []
    for row in scope:
        why = picked.get((str(row["kind"]), str(row["name"])))
        if why:
            remove.append({**row, "reason": why[0], "detail": why[1], "order": why[2]})
        else:
            keep.append(row)
    remove.sort(key=lambda row: (GC_REASONS.index(row["reason"]), str(row["kind"]),
                                 str(row["name"])))
    counts = {reason: sum(1 for row in remove if row["reason"] == reason)
              for reason in GC_REASONS}
    return {"remove": remove, "keep": keep, "total_bytes": total, "after_bytes": after,
            "counts": counts, "notes": notes}


def cache_remove(data_dir, market: str, targets: Sequence[Tuple[str, str]]) -> Tuple[int, int]:
    """Delete the listed cache entries (``(kind, name)`` as
    :func:`cache_inventory` names them).  Returns ``(entries removed, bytes
    freed)``.  Nothing outside ``qf_screen_cache/`` and ``ak_live/`` is
    ever touched; a window left without tables is removed with it."""
    wanted = {(str(kind), str(name)) for kind, name in targets}
    entries: set = set()
    freed = 0
    for kind, name, path in _iter_cache(data_dir, market):
        if (kind, name) not in wanted:
            continue
        try:
            if path.is_dir():                    # a store cache: all or nothing
                for inner in path.iterdir():
                    if inner.is_file():
                        freed += inner.stat().st_size
                        inner.unlink()
                path.rmdir()
                entries.add(name)
            else:
                freed += path.stat().st_size
                path.unlink()
                entries.add(f"{kind}:{name}")
        except OSError as exc:
            logger.warning("cache clear: %s not removed (%s)", path.name, exc)
    tables = cache_dir_for(data_dir, market) / TABLES_DIR
    try:
        for window in sorted(tables.iterdir()):
            if window.is_dir() and not any(window.iterdir()):
                window.rmdir()
        if not any(tables.iterdir()):
            tables.rmdir()
    except OSError:
        pass
    return len(entries), freed


def cache_clear(data_dir, market: str, kinds: Sequence[str]) -> Tuple[int, int]:
    """Delete the cache entries whose kind is in ``kinds`` (``"all"`` for
    everything inside the two cache directories, ``tmp`` files included).
    Returns ``(entries removed, bytes freed)`` -- an entry is what
    :func:`cache_inventory` lists: a table with its sidecar, or a store
    directory.  To prune instead of clearing by kind, plan with
    :func:`cache_gc_plan` and remove with :func:`cache_remove`."""
    everything = "all" in set(kinds)
    targets = [(str(row["kind"]), str(row["name"])) for row in cache_inventory(data_dir, market)
               if everything or row["kind"] in kinds]
    return cache_remove(data_dir, market, targets)


__all__ = [
    "GC_REASONS", "KINDS", "KIND_LABELS", "LIVE_DIR", "TABLES_DIR", "cache_clear",
    "cache_dir_for", "cache_gc_plan", "cache_inventory", "cache_remove", "cache_summary",
    "cached_wide_table", "derived_fingerprint", "entry_last_use", "fingerprint", "kind_of",
    "live_cache_dir", "read_cached_table", "read_wide", "window_summary", "write_cached_table",
    "write_wide",
]
