# Implementation Plan: Stock & Section Symbol Management Module (`qf_screen.symbols`)

## Problem & Proposed Approach

`qf_screen` currently reads stock metadata passively from `cn/lxg/company_list.csv` + per-symbol `lxg/industries.csv` (`data/universe.py`) and fetches EastMoney industry/concept boards only on-demand via `--live` (`LiveAkClient` in `data/ak_provider.py`). There is no dedicated module to download, persist, query, or manage stock symbols and section (industry/concept/custom) symbols from **akshare** or **lxg (Lixinger / GeoScope)**.

We will create a new `qf_screen/symbols/` subpackage that:
1. Downloads stock symbols and section symbols (EastMoney industry & concept boards + constituents via **akshare**; company list and Shenwan `sw_2021` / CNI `cni` industries + constituents via **lxg GeoScope API**, plus an offline local-import fallback from existing `cn/lxg/` and `cn/equity/*/lxg/industries.csv`).
2. Persists all catalogs and bidirectional section↔stock constituent mappings under `$QUANT_DATA_DIR/<market>/symbol/` (alongside existing `symbol/fh`, `xq`, `xtq`, while keeping `cn/lxg/company_list.csv` synced when downloading from lxg).
3. Provides full programmatic query & custom section management (`SymbolStore`: search stocks/sections, section→members, stock→sections, custom section CRUD).
4. Exposes `qf-screen symbol ...` CLI subcommands and integrates `SymbolStore` into `CNDataStore` / `Universe` / `cli.py` so `--industry` and `--concept` (and custom sections) work **offline** once synced.

## Architecture & File Layout

### New Subpackage: `qf_screen/symbols/`
- `qf_screen/symbols/__init__.py` — Public exports (`SymbolStore`, `SyncResult`, `SymbolError`, `download_ak_stocks`, `download_ak_sections`, `download_lxg_stocks`, `download_lxg_sections`, `sync_symbols`).
- `qf_screen/symbols/store.py` — `SymbolStore` class managing on-disk CSV/JSON files under `<data_dir>/<market>/symbol/`:
  - `ak/stocks.csv`, `ak/industry_sections.csv`, `ak/industry_members.csv`, `ak/concept_sections.csv`, `ak/concept_members.csv`
  - `lxg/stocks.csv`, `lxg/industry_sections.csv`, `lxg/industry_members.csv` (and optional mirror to `<data_dir>/<market>/lxg/company_list.csv`)
  - `custom/stocks.csv`, `custom/sections.csv`, `custom/members.csv`
  - `meta.json` (timestamps, row counts, source provenance)
  - Methods: `stocks()`, `get_stock()`, `sections()`, `get_section()`, `section_members()`, `stock_sections()`, `upsert_section()`, `delete_section()`, `add_section_members()`, `remove_section_members()`, `upsert_stock()`, `remove_stock()`, `import_local_lxg()`, `status()`.
- `qf_screen/symbols/ak_downloader.py` — Lazy `akshare` downloader (injectable `ak` module for tests, reuses `_bypass_proxy_for_eastmoney()`):
  - `download_ak_stocks(ak=None)` (`stock_info_a_code_name` with fallback to `stock_zh_a_spot_em`)
  - `download_ak_sections(kind="industry"|"concept", ak=None, include_members=True, pause=0.3, section_filter=None)` (`stock_board_industry_name_em` / `stock_board_industry_cons_em` and `stock_board_concept_name_em` / `stock_board_concept_cons_em`)
- `qf_screen/symbols/lxg_downloader.py` — Lixinger downloader via GeoScope proxy (`https://geoscopeapp.com/api/v1/data/lixinger/`):
  - Direct connection (`requests.Session` with `trust_env=False` — never routes through HTTP proxies), token from `LIXINGER_API_KEY` / `GEOSCOPE_API_KEY`, rate-limited (~55 req/min), injectable client for tests.
  - `download_lxg_stocks(api_key=None, market="cn", client=None)` (`POST cn/company`)
  - `download_lxg_sections(source="sw_2021", api_key=None, include_members=True, client=None)` (`POST cn/industry` + `POST cn/industry/constituents/{source}`)
- `qf_screen/symbols/sync.py` — High-level `sync_symbols(store, source="all", what=..., ...)` orchestrator returning a structured `SyncResult`.

### Integrations with Existing Modules
- `qf_screen/__init__.py` — Add lazy exports for `SymbolStore`, `SymbolError`, `SyncResult`, `sync_symbols`.
- `qf_screen/data/universe.py` — Allow `build_universe()` to optionally fill missing stock names / `industry_l1` from `SymbolStore` when `cn/lxg/company_list.csv` or `industries.csv` has gaps (preserving cache compatibility).
- `qf_screen/data/merged_store.py` — Let `CNDataStore.board_members()` and `enrich_industry_tags()` read from local `SymbolStore` offline before falling back to `LiveAkClient`.
- `qf_screen/cli.py`:
  - Update `_industry_symbols()` and `_board_symbols()` to resolve from `SymbolStore` (lxg + ak + custom) offline first, falling back to `--live` when absent.
  - Add `qf-screen symbol` subcommand group: `sync`, `stocks`, `sections`, `members`, `lookup`, `add-section`, `remove-section`, `add-members`, `remove-members`, `status`.

### Documentation
- `doc/plan/PLAN_symbols.md` — Persisted copy of this implementation plan in the repository's `doc/plan/` directory (alongside `PLAN_v0.3.md`, `PLAN_v0.4.md`, `PLAN_web_optimization.md`).

### Tests
- `test/test_symbols.py` — Hermetic `@pytest.mark.unit` tests using `tmp_path`, fake `akshare`, and fake GeoScope HTTP client covering downloaders, `SymbolStore` queries & custom CRUD, `Universe`/`CNDataStore` offline integration, and `qf-screen symbol` CLI commands.

## Todos

0. `save-plan-doc`: Save this implementation plan to `doc/plan/PLAN_symbols.md` in the repository.
1. `symbol-store`: Create `qf_screen/symbols/store.py` with on-disk CSV/JSON schema under `$QUANT_DATA_DIR/<market>/symbol/`, query APIs, custom section/stock CRUD, and `import_local_lxg()`.
2. `ak-downloader`: Create `qf_screen/symbols/ak_downloader.py` for downloading A-share stocks, EastMoney industry & concept sections, and constituents via akshare (lazy import, proxy bypass, injectable client).
3. `lxg-downloader`: Create `qf_screen/symbols/lxg_downloader.py` for downloading company list and `sw_2021`/`cni` industry sections + constituents via GeoScope API (`trust_env=False`, rate limiting, injectable client).
4. `sync-and-exports`: Create `qf_screen/symbols/sync.py` and `qf_screen/symbols/__init__.py`, and add lazy exports in `qf_screen/__init__.py`.
5. `store-universe-integration`: Wire `SymbolStore` into `qf_screen/data/universe.py` and `qf_screen/data/merged_store.py` for offline section/industry lookup and tag enrichment.
6. `cli-symbol-commands`: Add `qf-screen symbol` subcommands (`sync`, `stocks`, `sections`, `members`, `lookup`, `add-section`, `remove-section`, `add-members`, `remove-members`, `status`) and update `--industry` / `--concept` in `qf_screen/cli.py` to use `SymbolStore` offline.
7. `tests-and-verify`: Add comprehensive unit tests in `test/test_symbols.py` and run `pytest -m unit` and `ruff check qf_screen test`.

## Notes & Considerations

- **No network in unit tests**: Both `ak_downloader` and `lxg_downloader` accept injectable clients/modules so unit tests remain 100% hermetic.
- **GeoScope safety**: Always set `session.trust_env = False` when calling `geoscopeapp.com` (matching `quant_free/datapull/lxg_geoscope_download.py`) to avoid proxy-triggered API key suspension; never touch `www.lixinger.com` directly.
- **Canonical symbols**: Normalize all stock codes via `quant_free.common.symbol_format.to_canonical` and derive boards via `qf_screen.data.universe.board_of`.
- **Backward compatibility**: Writing `cn/lxg/company_list.csv` during lxg stock sync preserves exact column names expected by `universe._parse_company_list()` and automatically invalidates the universe cache via mtime fingerprint.

---

## Implementation record (2026-11-27)

Implemented on branch `v0.3` by Claude Code from the Copilot session draft
(`plan.md`), todos 0–7 in order.

* `doc/plan/PLAN_symbols.md` — this file (todo 0).
* `qf_screen/symbols/store.py` — `SymbolStore` over
  `<data_dir>/<market>/symbol/{ak,lxg,custom}/…` + `meta.json`; queries
  (`stocks`, `sections`, `section_members`, `stock_sections`, `membership`,
  `board_frame`, `industry_l1`, `stock_names`), custom CRUD, atomic CSV
  writes, `import_local_lxg()` from `cn/lxg/company_list.csv` +
  `cn/equity/*/lxg/industries.csv`, `company_list.csv` mirror writer,
  `status()` / `fingerprint()`.
* `qf_screen/symbols/ak_downloader.py` — `download_ak_stocks`
  (`stock_info_a_code_name`, spot fallback), `download_ak_sections`
  (东财 industry/concept boards + constituents, board filter, pause,
  proxy bypass reused from `data.ak_provider`).
* `qf_screen/symbols/lxg_downloader.py` — GeoScope `POST cn/company` /
  `cn/industry` / `cn/industry/constituents/{source}`; direct connection
  (`trust_env=False`, explicit `proxies=None`), Bearer key from
  `LIXINGER_API_KEY`/`GEOSCOPE_API_KEY`, ~55 req/min pacing, injectable
  client.
* `qf_screen/symbols/sync.py` — `sync_symbols()` orchestrator
  (`source=ak|lxg|local|all`) returning `SyncResult` (per-source counts,
  collected errors, notes); tolerates the currently-empty upstream
  constituents endpoint.
* `qf_screen/symbols/__init__.py` + lazy exports in `qf_screen/__init__.py`.
* `qf_screen/data/universe.py` — `build_universe(symbol_store=...)` fills
  missing names / `industry_l1`; `universe_fingerprint()` folds the symbol
  catalog in ONLY when it exists on disk, so caches built before the
  feature still hit.
* `qf_screen/data/merged_store.py` — `CNDataStore.symbol_store`;
  `board_members()` serves the local catalog offline (ak + custom) before
  the live client; `enrich_industry_tags()` fills from the local catalog
  without `--live`.
* `qf_screen/cli.py` — `qf-screen symbol {sync,stocks,sections,members,
  lookup,add-section,remove-section,add-members,remove-members,status}`;
  `_industry_symbols`/`_board_symbols` resolve from the catalog (lxg + ak
  + custom) offline first; `SymbolError` reported as a one-line exit.
* `test/test_symbols.py` — hermetic unit tests (fake akshare module, fake
  GeoScope client, conftest synthetic store) covering downloaders, store
  CRUD/persistence, local import, sync, universe fill + cache stability,
  offline board members / industry tags, and the CLI commands.

Verification: `pytest -m unit` green (241 tests, 19 of them in
`test/test_symbols.py`); the full suite (`pytest -q`, unit + data marks)
green at 271; `ruff check qf_screen test` clean.

Two behavior notes from the implementation:

* the universe fingerprint folds the symbol catalog in ONLY when catalog
  files exist on disk, so caches built before this module still hit (and
  the first `symbol sync` honestly invalidates them);
* a synced catalog changes two CLI miss-messages (they gain a
  ``qf-screen symbol sync`` hint), and the ``--live`` hint stays in the
  industry-miss message regardless, so behavior does not depend on test
  order against the shared synthetic store.
