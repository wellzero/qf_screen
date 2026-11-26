"""``qf-screen cache``: what the caches hold, how fresh they are, and how
to pre-warm them after a data pull.  Everything here is safe to run at any
time — the caches only ever cost speed.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Dict, List, Optional, Sequence

from qf_screen.data.table_cache import (
    cache_dir_for, cache_inventory, cache_summary, live_cache_dir, window_summary,
)

logger = logging.getLogger(__name__)

#: a merged-store cache directory name:
#: ``store_tail<rows>_<symbol key>_v<version>`` or
#: ``store_hist<start>-<end>_<symbol key>_v<version>``
_STORE_DIR = re.compile(
    r"^store_(?:tail(\d+)|hist(\d{4}-\d\d-\d\d)-(\d{4}-\d\d-\d\d|open))_([0-9a-f]+)_v(\d+)$")


def default_fingerprint(data_dir, market: str = "cn") -> tuple:
    """``(fingerprint, symbol key)`` the DEFAULT universe gives the daily
    store cache — what ``cache status`` compares the cached fingerprint
    with.  Costs the universe census (itself cached) plus one ``stat`` per
    source file; it loads no tables."""
    from qf_screen.data import build_universe
    from qf_screen.data.merged_store import CNDataStore, source_fingerprint

    universe = build_universe(data_dir, market)
    symbols = list(universe.symbols)
    return (source_fingerprint(universe.root, symbols, universe.dir_names,
                               CNDataStore._CACHE_SOURCES),
            CNDataStore.symbols_key(symbols))


def cache_status(data_dir, market: str = "cn", check_fresh: bool = True) -> Dict[str, object]:
    """The inventory of both cache directories, rolled up per kind and per
    store window (the derived tables), plus one row per merged-store cache
    with what it covers.

    ``fresh`` on a daily-store row is ``True`` / ``False`` when its
    fingerprint can be compared with the default universe's (same symbol
    list) and ``None`` when the cache was built for another symbol set or
    the check is off; a history-window cache is never judged.
    """
    inventory = cache_inventory(data_dir, market)
    fingerprint = symbol_key = None
    if check_fresh:
        fingerprint, symbol_key = default_fingerprint(data_dir, market)
    stores: List[Dict[str, object]] = []
    for entry in inventory:
        if entry["kind"] not in ("store", "history"):
            continue
        name = str(entry["name"])
        row: Dict[str, object] = {
            "name": name, "kind": entry["kind"], "bytes": entry["bytes"],
            "mtime": entry["mtime"], "age_days": entry["age_days"], "fresh": None}
        match = _STORE_DIR.match(name)
        if match:
            row["mode"] = (f"tail({match.group(1)} rows)" if match.group(1)
                           else f"history {match.group(2)}..{match.group(3)}")
        try:
            meta = json.loads((cache_dir_for(data_dir, market) / name / "meta.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        calendar = meta.get("calendar") or []
        row.update({
            "symbols": len(meta.get("symbols") or []),
            "sessions": len(calendar),
            "window": f"{calendar[0]}..{calendar[-1]}" if calendar else None,
            "fingerprint": meta.get("fingerprint")})
        if entry["kind"] == "store" and match and fingerprint is not None:
            if match.group(4) == symbol_key:
                row["fresh"] = meta.get("fingerprint") == fingerprint
            else:
                row["note"] = "built for a custom symbol set (not comparable)"
        stores.append(row)
    return {
        "cache_dir": str(cache_dir_for(data_dir, market)),
        "live_cache_dir": str(live_cache_dir(data_dir, market)),
        "entries": len(inventory),
        "bytes": sum(int(entry["bytes"]) for entry in inventory),
        "kinds": cache_summary(inventory),
        "windows": window_summary(inventory),
        "stores": stores,
        "source_fingerprint": fingerprint,
    }


def cache_warm(data_dir=None, market: str = "cn", presets: Optional[Sequence[str]] = None,
               symbols: Optional[Sequence[str]] = None, max_workers: int = 8,
               start=None, end=None, freq: Optional[str] = None) -> Dict[str, object]:
    """Build every cache a preset's daily run (or its schedule over
    ``start`` .. ``end``) needs, so the first real run of the day is warm:
    the universe census, the merged store tables, the derived tables and
    the price features each screen touches.  Returns what was warmed and
    how long each step took."""
    from qf_screen.data import CNDataStore
    from qf_screen.engine import ScreenEngine, ScreenSpec
    from qf_screen.presets import list_presets

    timings: Dict[str, float] = {}
    began = time.perf_counter()
    store = CNDataStore(data_dir=data_dir, market=market, symbols=symbols, start=start,
                        end=end, max_workers=max_workers)
    timings["store"] = time.perf_counter() - began
    engine = ScreenEngine(store)
    warmed: Dict[str, Dict[str, object]] = {}
    for name in list(presets) if presets else list_presets():
        clock = time.perf_counter()
        spec = ScreenSpec.from_yaml(name)
        if start is None:
            result = engine.run(spec)
            row: Dict[str, object] = {"members": len(result.members),
                                      "asof": str(result.asof.date())}
        else:
            schedule, _funnel = engine.schedule(spec, start=start, end=end, freq=freq)
            row = {"rows": len(schedule),
                   "dates": int(schedule["date"].nunique()) if len(schedule) else 0}
        row["seconds"] = time.perf_counter() - clock
        warmed[name] = row
    timings["presets"] = sum(float(item["seconds"]) for item in warmed.values())
    timings["total"] = time.perf_counter() - began
    return {"store": f"{len(store.provenance)} symbols, {len(store.calendar)} sessions "
                     f"{store.calendar[0].date()}..{store.calendar[-1].date()}",
            "window": store.window_key,
            "cache_dir": str(cache_dir_for(store.data_dir, store.market)),
            "presets": warmed, "seconds": timings}


__all__ = ["cache_status", "cache_warm", "default_fingerprint"]
