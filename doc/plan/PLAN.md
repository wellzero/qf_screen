# Implementation Plan: `qf_screen` — Chinese A-Share Stock Screener

## Problem Statement & Approach

Build `qf_screen` as a standalone, installable Python package in `/home/mnt/quant_volumn/qf_project/qf_screen` for screening Chinese A-share stocks. It integrates with the existing `qf_project` ecosystem (`quant_free` and `qf_factor`), leaves legacy `quant_free/screen` untouched, and combines:
- **Local-first data** from `QUANT_DATA_DIR` (`/home/data/quant_free_data/cn/`):
  - `lxg` provider (`cn/lxg/company_list.csv`, `cn/equity/<SYM>/lxg/{daily,valuation,company,industries,indices,dividend,margin,mutual_market,volatility,shareholders_num,pledge,block_deal,exec_shares_change,major_sh_change}.csv`, plus financial statements via `qf_factor.factory.lxg_finance`)
  - `ak` provider (`cn/equity/<SYM>/ak/{basic,research_reports}.csv`, covering ~5,693 symbols)
- **Optional on-demand `akshare` public API refresh** (`--live` / `refresh=True`, with disk TTL cache, never called in offline mode or unit tests) for real-time spot snapshots and industry/concept board constituents.

The screener supports both **point-in-time snapshot screening** (fast daily screening with rich CLI/CSV/JSON outputs and per-symbol explanations) and **causal wide-panel screening** (exporting `date,symbol` PIT schedules directly into `qf_factor` backtest/live pipelines and validating historical forward returns).

## Package & Ecosystem Conventions
- Shared venv: `/opt/quantfree/bin/python` (Python 3.12); editable install via `/opt/quantfree/bin/pip install -e . --no-deps --no-build-isolation`
- `pyproject.toml`: setuptools backend, console script `qf-screen = "qf_screen.cli:main"`, ruff lint `select = ["E4", "E7", "E9", "F"]`, pytest `--strict-markers` (`unit`, `data`, `slow`)
- Reused sibling primitives:
  - `quant_free.common.const_filename` (`get_data_dir`, `EQUITY_DIR`, `PROVIDER_LXG`, `PROVIDER_AK`, `DAILY_CSV`)
  - `quant_free.common.symbol_format` (`to_canonical`) and `quant_free.datapull.cn_delisted_backfill` (`canonical_equity_symbol`, `_INDEX_RE`)
  - `qf_factor.framework.selection.screens` (causal price/volume screen primitives & wide loaders)
  - `qf_factor.framework.selection.screener` (`StockScreener`, `ScreenRule`, `composite_score`)
  - `qf_factor.framework.selection.st_status` (PIT ST/*ST exclusion)
  - `qf_factor.factory.lxg_finance` (lxg financial statement pivot bridge)

## Architecture & Module Layout

```
qf_screen/
├── PLAN.md
├── pyproject.toml
├── README.md
├── .gitignore
├── qf_screen/
│   ├── __init__.py
│   ├── cli.py                  # `qf-screen` CLI (list, run, pit, validate, data-status)
│   ├── engine.py               # ScreenEngine, ScreenSpec, ScreenResult, PIT export & validation
│   ├── data/
│   │   ├── __init__.py
│   │   ├── universe.py         # Canonical CN A-share universe, board/ST/industry/index metadata
│   │   ├── lxg_provider.py     # Local lxg readers (daily, valuation, dividend, alt signals, finance)
│   │   ├── ak_provider.py      # Local ak/basic + research_reports + optional live akshare client
│   │   └── merged_store.py     # CNDataStore: unified snapshot & causal wide-panel data facade
│   ├── rules/
│   │   ├── __init__.py
│   │   ├── base.py             # Rule interface (snapshot + causal wide), Ranker, ScreenRule bridge
│   │   ├── hygiene.py          # Non-ST, board, listing age, liquidity, price, suspension guards
│   │   ├── fundamental.py      # PE/PB/PS/PCF/PEG, market cap, dividend, ROE/margin/FCF/growth
│   │   ├── technical.py        # Wrappers over qf_factor screens (mom, trend, 52w high, low-vol, MAX, breakout)
│   │   ├── ashare_alt.py       # Northbound, margin, shareholder count, pledge, insider, analyst, industry
│   │   └── dsl.py              # Safe AST-validated YAML/expression rule & rank parser (no eval)
│   └── presets/
│       ├── __init__.py         # Preset loader & registry
│       ├── value_dividend.yaml
│       ├── quality_growth.yaml
│       ├── momentum_trend.yaml
│       ├── low_vol_defensive.yaml
│       └── smart_money_ashare.yaml
└── test/
    ├── conftest.py             # Synthetic lxg + ak fixture tree builder (hermetic)
    ├── test_data_store.py      # Universe dedup, lxg+ak merge, mocked live akshare, provenance
    ├── test_rules.py           # Hygiene, fundamental, technical, alt rules, NA semantics
    ├── test_dsl.py             # Safe expression parser, cross-sectional ops, rejection of unsafe AST
    ├── test_engine_and_pit.py  # ScreenEngine, explain/funnel, PIT causality (no look-ahead), validation
    ├── test_cli.py             # CLI subcommands (list, run, pit, validate, data-status)
    └── test_real_data.py       # @pytest.mark.data smoke tests against QUANT_DATA_DIR
```

## Todos

1. **`scaffold-package`**: Store this implementation plan to `PLAN.md` in the repo root, create `pyproject.toml`, `.gitignore`, package skeleton (`qf_screen/`), and install editable into `/opt/quantfree`.
2. **`data-providers`**: Implement `qf_screen/data/universe.py`, `lxg_provider.py`, `ak_provider.py` (local + optional cached live `akshare`), and `merged_store.py` (`CNDataStore` for both snapshot and causal wide-panel modes).
3. **`rule-primitives`**: Implement `qf_screen/rules/{base,hygiene,fundamental,technical,ashare_alt}.py` integrating `qf_factor` selection screens, ST status, and `lxg_finance`.
4. **`dsl-and-presets`**: Implement safe AST-validated YAML/expression engine (`qf_screen/rules/dsl.py`) and built-in YAML presets (`value_dividend`, `quality_growth`, `momentum_trend`, `low_vol_defensive`, `smart_money_ashare`).
5. **`engine-and-pit`**: Implement `qf_screen/engine.py` (`ScreenEngine`, `ScreenResult`, funnel & per-symbol `explain`, `qf_factor`-compatible PIT schedule export, and historical forward-return validation).
6. **`cli-interface`**: Implement `qf_screen/cli.py` (`qf-screen` with `list`, `run`, `pit`, `validate`, `data-status` subcommands and table/CSV/JSON formatting).
7. **`tests-and-docs`**: Write comprehensive hermetic `unit` tests, `data` smoke tests, and `README.md`; verify with `pytest` and `ruff`.

## Key Design Notes & Considerations

- **Conservative NA & PIT semantics**: Missing values default to failing a filter (`na_pass=False` unless explicitly enabled). All wide-panel / PIT schedule tables enforce a strictly-before `lag=1` shift matching `qf_factor.framework.selection.screens` so exported schedules have zero look-ahead.
- **Data merge priority**: For daily price/valuation/market cap, `lxg` and `ak/basic.csv` are merged by date so symbols without `lxg` (~3,200 symbols) or dates newer than `lxg` seamlessly fall back to `ak/basic.csv`. Live `akshare` refresh is strictly opt-in (`--live` / `refresh=True`) and cached on disk with configurable TTL to avoid rate limits.
- **Safety in custom rules**: The YAML expression DSL uses Python `ast.parse` with a strict node/function whitelist (arithmetic, comparisons, boolean ops, `rank`, `pct_rank`, `zscore`, `industry_rank`, `abs`, `clip`, `fillna`) — never `eval()`.
- **No modifications to sibling repos**: `quant_free/screen` and `qf_factor` remain untouched; `qf_screen` only imports their public/shared primitives.

---

# v0.2 — Bug Fixes & Productization (implemented 2026-11-25)

Running the v0.1 presets against the real `QUANT_DATA_DIR` store surfaced
five high-impact bugs and four productization gaps. All are fixed, tuned
and pinned by regression tests (84 unit + 12 real-data, ruff clean).

## Bugs fixed

1. **numexpr wide-op corruption (CRITICAL)** — pandas delegates wide
   (`>10k` cell) `DataFrame op DataFrame` to numexpr; this environment's
   numexpr 2.10.2 / numpy 2.4 silently corrupts float32+NaN input (1.38M
   of 3.45M cells wrong on a 630×5472 division). `SH600519`'s
   `ma_ratio_200` read `0.0` instead of `1.096`; 26 of 5,472 names passed
   `trend`. Fix: `import qf_screen` disables pandas' numexpr path AND the
   wide feature builders go through plain-numpy helpers (`_safe_div`,
   `_safe_sub`) — correct even on a hostile host. Pinned by
   `test_safe_div_exact_with_numexpr_forced_on` /
   `test_ma_ratio_builder_exact_at_width`.
2. **`days[:, None]` 2-D broadcast** — `_div_yield_table` and
   `_window_sum` fed `(N, 1)` needles to `np.searchsorted`; the
   cumulative-sum division broadcast to `(N, N)` and every row was
   divided by the FIRST row's price. Fix: 1-D needles; multi-symbol
   distinct-value tests (`test_div_yield_distinct_per_symbol`,
   `test_window_sum_distinct_per_symbol`).
3. **Stale alt data vetoed every screen** — lxg's `mutual_market` /
   `margin` files lag `ak/basic.csv` (northbound disclosure ended
   2026-09-30; margin runs ~2 weeks behind), so change features were NaN
   on the decision date and `smart_money_ashare` returned 0/5,472. Fix:
   bounded `ffill(limit=QF_SCREEN_ALT_FFILL, default 20 sessions)` —
   strictly causal, a dead source reverts to honest NaN — plus loud
   staleness warnings replayed from the table cache into
   `ScreenResult.warnings` and a `data-status` `alt_freshness` census.
4. **`schedule()` / `validate()` ignored `rank` + `top_n`** — the PIT
   export wrote all passers while `run` wrote the ranked top-N. Fix: the
   same `_rank_members` selection backs both (`apply_top_n=False` exports
   the raw pool); filters/rankers compile once per schedule, not per date.
5. **`explain()` empty for rejected symbols** — `panel_values` held only
   members. Fix: the snapshot covers every candidate; rule expressions
   ride along (`rule_exprs`).

## Productization

* **Caching** — fingerprinted parquet cache for the merged store tables
  (`cn/qf_screen_cache/store_<mode>_<symbols>_v<version>/`): repeat runs
  read ~1 s instead of ~8–10 s of merging (`--no-cache` bypasses).
  Derived alt/dividend/factor tables cache with (count, max-mtime,
  calendar bounds, SYMBOL-LIST hash) fingerprints — the symbol hash is
  load-bearing: price files update intraday while alt mtimes stand
  still. Atomic writes; sidecar warnings replay on hits; `v2:` sidecar
  version invalidates old caches.
* **Skip lists** — alt/dividend/factor builders iterate only symbols
  that have the file (`store.symbols_with_file`), skipping ~3,400 empty
  dirs; wide outputs assemble into numpy matrices (no column-insert
  fragmentation).
* **Coverage features** — `roe_implied` (`pb/pe_ttm`, positive-only,
  full market), `roe_best` (statement + implied fallback), `dyr` (lxg
  layout-B trailing yield; `div_yield_ttm` fallback),
  `market_cap_bn` / `float_market_cap_bn` (亿元), `amount_ma20_mn`
  (百万元).
* **qf_factor CSV bridge** — `factor("<family>", "<column>")` in the DSL
  or `factor_<family>__<column>` anywhere a feature name works; resolves
  `<DIR>/<provider>/<Family>.csv` (providers `xq,lxg,xtq` via
  `QF_SCREEN_FACTOR_PROVIDERS`, the legacy bare `<DIR>/<Family>.csv`
  last), forward-filled onto the calendar and disk-cached. String
  literals are compile-time legal ONLY inside `factor(...)`.
* **CLI** — `qf-screen features [PATTERN]`; `run` gained `--where`
  (repeatable DSL filter), `--rank`, `--boards`, `--industry` /
  `--concept` (live 东财 constituents), `--no-cache` and
  `--format markdown`; human units (亿元 / %) in tables, funnel survival
  shares, warnings on stderr. `UniverseSpec.symbols` is now enforced
  (`SymbolSetFilter`) instead of silently ignored.
* **Presets tuned on real data (2026-11-24)** — `value_dividend` 13
  members (quality via `roe_best`: 690 vs 168 passers),
  `quality_growth` 30, `momentum_trend` 30 (trend 422 passers after the
  numexpr fix, was 26), `low_vol_defensive` 30,
  `smart_money_ashare` 3 (northbound/pledge/insiders `na_pass` with the
  staleness warning; margin + analysts stay strict).

## Verification pass (2026-11-26)

Every item above was re-checked against the real store: the shipped 96
tests green, ruff clean, all five presets reproducing their tuned member
counts (13 / 30 / 30 / 30 / 3 as of 2026-11-24), `pit` / `validate`
applying `rank` + `top_n`, the factor bridge resolving in both DSL forms,
`--explain` working on rejected names. The pass found six defects,
fixed and pinned (suite now 111 unit + 13 real-data):

1. **Warm runs handed the cache back** — the store loaded in ~1 s, then
   feature building spent ~6 s: two `combine_first` calls (~2 s each at
   store width) and a per-process re-pivot of the statement payload. Fix:
   `_coalesce` (plain numpy, `combine_first`-identical) and statement
   fields cached as wide step tables (`finance_step_<feature>.parquet`,
   bit-identical to the shipped algorithm on all 16 fields).
   `value_dividend` 8.5 s → 3.7 s, `quality_growth` 7.8 s → 3.8 s. The
   "~1 s" above is the STORE load; a repeat run is ~3.5–4.5 s end to end
   (0.3 s imports, ~1 s universe build — it re-reads 2,042
   `industries.csv` per process — ~1 s store read, ~1 s feature math).
   The first run after a data update still rebuilds the derived tables
   (7–26 s): their fingerprints carry the calendar bounds.
2. **`--no-cache` missed the derived tables** — only the store consulted
   `use_cache`; alt / dividend / factor / statement tables were still
   served from disk. Every derived cache now takes `use_cache` (no read,
   no write); a real-store `--no-cache` run is identical to the cached one.
3. **CLI units were numerically wrong for two feature families** —
   `turnoverrate` / `turnover_ma*` (percent points) printed 100x too
   large ("138.1%" for 1.38 %) and `*_mn` features were scaled down a
   second time ("0.0亿" for 1,128 百万). Explicit unit suffixes and the
   percent-point names now decide before the substring hints; `dyr`
   prints as a yield.
4. **Staleness warnings did not name their feature** — `nb_hold_chg_20d`
   and `nb_hold_chg_60d` both warned about `mutual_market.csv` with two
   different counts. The feature now heads the line; sidecar version
   `v3` re-records the text.
5. **Stale docstring / metadata** — `factor_bridge`'s docstring stated
   the reverse search order; the installed dist still said 0.1.0.
6. **`--industry` + `--concept` together dropped one restriction** — the
   second flag silently replaced the first, so
   `--industry 电力设备 --concept 机器人` screened the whole concept
   board. Both are restrictions: they now intersect, and an empty
   intersection is an error rather than an empty screen. (The board
   matcher had no offline test; it has two.)

---

# v0.3 — Daily Workflows, Validation Analytics, a Sub-Second Warm Run (implemented 2026-11-26)

The plan, as drafted and approved, and its full implementation record live
in **`PLAN_v0.3.md`** (its own file, at the user's request — not a section
of this one).  This is the one-page summary; the numbers were measured on
the real `QUANT_DATA_DIR` store with `qf-screen 0.3.0`.

| todo | outcome |
|---|---|
| `v03-fix-cli-bugs` | `--top` now replaces the preset's `top_n` (it was read as `args.top_n`, a name the parser never set) and the summary matches the table; `run` works with no preset (`--where` / `--rank` on the required hygiene block); `--format markdown --out FILE` writes the document and `md` is an alias; `--industry` matches the local 申万 classification offline; `pit` / `validate` take `--where`, `--rank`, `--boards`, `--top`, `--all-passers` |
| `v03-lxg-ak-coverage-and-presets` | factor bridge reads `lxg,ak` only (other provider trees opt-in via `QF_SCREEN_FACTOR_PROVIDERS`); `quality_growth` requires a profitable, cash-backed base and caps growth for ranking (31 → 23 passers, the 1,992 % name out); `value_dividend`'s balance leg is `na_pass` with financials excluded (13 → 45 passers); `--live` fills the industry tags of names without `lxg/industries.csv` |
| `v03-daily-workflows` | `diff`, `inspect`, `report`; industry mix on every run; `max_per_industry` in YAML and `--max-per-industry` |
| `v03-validate-analytics` | turnover, annualized return / volatility / Sharpe / max drawdown / cumulative return for the schedule, the candidate pool and the CSI 300 / 500 lxg index files; `--curve`; `compare`; each window keeps its own derived tables (a `run` after a `validate`: 12 s → 0.9 s) |
| `v03-startup-cache-ops` | warm `run` ≈3.6–4.0 s → 0.8–1.0 s: universe census cached, wide tables stored as one flat parquet column, price features cached behind the store fingerprint, the `yfinance` import deferred; `qf-screen cache {status,clear,warm}` |
| `v03-tests-and-docs` | 173 unit + 23 real-data tests, `ruff` clean; `README.md` and this file updated |

Found and fixed on the way (each pinned by a test): the derived-table cache
was shared between the daily window and validation windows, so the two
modes evicted each other; a missing industry came back from the static
feature as the text `"nan"`, which `industry_rank` ranked as an industry of
its own and an industry cap counted against; the statement payload cache
thrashed between windows the same way.

Known limits — in `PLAN_v0.3.md` under the record.



---

# v0.4 — User Presets, a Watchlist Monitor, HTML Reports, an Archive, Webhooks, Cache Pruning (implemented 2026-11-26)

The plan, as the user drafted it, and its full implementation record live
in **`PLAN_v0.4.md`**.  This is the one-page summary; the numbers were
measured on the real `QUANT_DATA_DIR` store with `qf-screen 0.4.0`.

| todo | outcome |
|---|---|
| `v04-fix-sim-and-cli` | `report --presets a,b` (and `inspect`, `cache warm`) takes commas; `simulate()` carries a stopped name's last close forward inside a holding block and holds cash in a period with nothing buyable; `validate` / `compare` take `--cost-bps` (per side: the net figures beside the gross ones) and `--skip-limit-up` (an entry that closed at its board's limit is refused and counted). Measured on `momentum_trend`, Jan–Sep 2026: 2 of 240 entries refused; 15 bps a side takes the annualized return from −30.2 % to −32.2 % at 73 % monthly turnover |
| `v04-user-presets-and-watchlists` | every `*.yaml` in `~/.config/qf_screen/presets/` (or `$QF_SCREEN_PRESETS_DIR`) is a preset, found by every command; one named like a built-in replaces it; `init-preset NAME [--from PRESET]` writes a documented file; the loader rejects typo'd keys at every level. `qf-screen watch` runs ST / pledge / trend-break / liquidity / suspension rules over the names you hold, with a `preset_exit` alert under `--preset`, custom rules in a watchlist file, three formats and `--fail-on-alert` (exit 2) |
| `v04-html-report-and-charts` | `report` / `validate` / `compare --format html`: one self-contained page — inline SVG charts (cumulative value, drawdown, industry concentration), sortable and filterable tables, a crosshair readout, `+NEW` / `−OUT` badges and rank-move pills, a table twin for each chart, dark and print variants; nothing is fetched. Verified by rendering in headless Chromium |
| `v04-automation-and-history` | `run` / `report --save-history` archives each session's ranked members (idempotent per date and preset); `qf-screen history PRESET` reads back each member's streak. Exit code 2 for `diff --fail-on-change` and `watch --fail-on-alert`; strictly opt-in webhooks (`json` / `feishu` / `dingtalk` / `slack`) on `diff`, `watch`, `report`, exit code 3 when a push fails |
| `v04-cache-gc-and-release` | `cache clear --stale` / `--older-than DAYS` / `--max-gb N [--dry-run]`, combined or narrowed with `--what`; `cache status` judges validation windows too. 219 unit + 30 real-data tests, `ruff` clean; `README.md` and this file updated |

A warm `run` is unchanged at 0.8–1.0 s; the HTML briefing costs the same
as the Markdown one (2.5 s); `watch` 0.7–1.0 s; `history` 0.4 s.

Decisions that differ from the plan's letter, and what was not done, are
in `PLAN_v0.4.md` under the record.  The main ones: `--cost-bps` is **per
side** of a trade; the limit-up guard tests the **close** (the store has
no opening prices for most names); `watch` adds a `suspended` alert and
an unknown-symbol alert; no webhook was sent to a real platform (the
transport is tested over loopback).

