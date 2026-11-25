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
