"""Local-first CN data layer: universe, lxg / ak providers, merged store."""
from __future__ import annotations

from .ak_provider import AkProvider, LiveAkClient, LiveDataError, normalize_spot
from .lxg_provider import LXG_FILES, LxgProvider
from .merged_store import (
    BASE_FIELDS,
    CNDataStore,
    build_adjusted_close,
    prepare_dividend_events,
)
from .universe import (
    ALL_BOARDS,
    BOARD_GROUPS,
    BOARD_LABELS,
    BOARD_LIMITS,
    BJ,
    CHINEXT,
    SH_MAIN,
    STAR,
    ST_STATUSES,
    SZ_MAIN,
    Universe,
    board_of,
    build_universe,
    is_a_share,
    is_st_name,
    resolve_boards,
    scan_equity_dirs,
)

__all__ = [
    "AkProvider", "LiveAkClient", "LiveDataError", "normalize_spot",
    "LXG_FILES", "LxgProvider", "BASE_FIELDS", "CNDataStore",
    "build_adjusted_close", "prepare_dividend_events",
    "ALL_BOARDS", "BOARD_GROUPS", "BOARD_LABELS", "BOARD_LIMITS", "BJ",
    "CHINEXT", "SH_MAIN", "STAR", "ST_STATUSES", "SZ_MAIN", "Universe",
    "board_of", "build_universe", "is_a_share", "is_st_name",
    "resolve_boards", "scan_equity_dirs",
]
