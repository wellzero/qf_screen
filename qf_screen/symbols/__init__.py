"""Stock & section (industry/concept/custom) symbol management.

``SymbolStore`` persists the catalogs under
``$QUANT_DATA_DIR/<market>/symbol/`` and answers both directions
(section -> members, stock -> sections); the downloaders fill it from
akshare (东财 boards) and the lxg GeoScope proxy (company list, 申万
industries), and :func:`sync_symbols` orchestrates a refresh::

    from qf_screen import SymbolStore, sync_symbols

    sync_symbols(data_dir="…", source="all")     # network (or "local")
    store = SymbolStore("…")
    store.section_members("白酒")

Custom sections live beside the synced ones and extend the offline
``--industry`` / ``--concept`` pools (see ``qf-screen symbol --help``).
"""
from __future__ import annotations

from .ak_downloader import download_ak_sections, download_ak_stocks
from .lxg_downloader import GEO_BASE, GeoScopeClient, download_lxg_sections, \
    download_lxg_stocks
from .store import SECTION_KINDS, SymbolError, SymbolStore
from .sync import SyncResult, sync_symbols

__all__ = [
    "SymbolStore", "SymbolError", "SyncResult", "sync_symbols",
    "download_ak_stocks", "download_ak_sections",
    "download_lxg_stocks", "download_lxg_sections",
    "GeoScopeClient", "GEO_BASE", "SECTION_KINDS",
]
