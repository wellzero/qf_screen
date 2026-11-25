# qf_screen — Chinese A-share stock screener

A standalone, local-first screener over the `QUANT_DATA_DIR` store, with two
modes that share ONE definition of a screen:

* **snapshot** — `qf-screen run value_dividend`: today's members, the
  cumulative funnel that produced them, per-symbol explanations, ranked by
  a blended score;
* **causal wide panel** — `qf-screen pit momentum_trend --start … --end …`:
  the same screen as a point-in-time `date,symbol` schedule (zero
  look-ahead) that loads straight into the `qf_factor` backtest/live
  plumbing, plus forward-return validation against the screen's own
  candidate pool.

It integrates the `qf_project` ecosystem (`quant_free` constants/helpers,
`qf_factor` selection screens / ST band detector / lxg finance bridge) and
leaves the legacy `quant_free/screen` untouched.

## Install

```bash
pip install -e ../quant_free --no-deps && pip install -e ../qf_factor --no-deps
pip install -e . --no-deps --no-build-isolation     # into /opt/quantfree
pytest test -m unit          # hermetic; -m data adds real-store smokes
```

## Quickstart

```bash
qf-screen list                                   # the built-in presets
qf-screen run momentum_trend --top 10 --explain SH600018
qf-screen run value_dividend --as-of 2026-11-20 --out members.csv
qf-screen pit momentum_trend --start 2026-01-05 --end 2026-11-24 \
          --freq M --out schedule.csv            # qf_factor-ready
qf-screen validate momentum_trend --start 2026-01-05 --end 2026-09-30
qf-screen data-status                            # coverage / freshness census
```

Library use:

```python
from qf_screen import CNDataStore, ScreenEngine, ScreenSpec

store = CNDataStore()                    # last ~600 sessions, 8 workers
engine = ScreenEngine(store)
result = engine.run(ScreenSpec.from_yaml("value_dividend"))
print(result.summary());  print(result.funnel)
result.explain(result.members[0])        # rule-by-rule why
schedule, funnel = engine.schedule(spec, "2026-01-05", "2026-11-24", freq="M")
report = engine.validate(spec, "2026-01-05", "2026-09-30")
```

## Presets

| name | thesis | legs |
|---|---|---|
| `value_dividend` | cheap, profitable, low-levered payers | P/E<15, ROE>12%, debt<60%, yield≥3% |
| `quality_growth` | profitable growers at a reasonable price | rev/profit growth>15%, ROE>12%, GM>20%, P/E<40 |
| `momentum_trend` | the classic price/volume stack | >200d MA, 6-1 momentum, near 52w high, low vol, not lottery |
| `low_vol_defensive` | the low-risk anomaly | bottom-third vol, no lottery, drawdown guard |
| `smart_money_ashare` | smart-money confirmation | northbound↑, margin↑, analyst buys, low pledge, insiders not selling |

Every preset starts from a shared hygiene block (traded, price ≥ 2 CNY,
20d mean traded value ≥ 1e8) — `load_preset` prepends any that are missing.

## The expression DSL

Filters and scores are written in a small expression language compiled via
`ast` to vectorized closures — **no `eval`**, no attributes, no
subscripts, whitelisted functions only:

```yaml
filters:
  - {name: value,    expr: "pe_ttm > 0 and pe_ttm < 15"}
  - {name: quality,  expr: "roe > 0.12 and debt_assets < 0.6"}
  - {name: dividend, expr: "div_yield_ttm >= 0.03"}
rank:
  - {expr: "pct_rank(earnings_yield)", weight: 1.0}
  - {expr: "industry_rank(div_yield_ttm)", weight: 0.5}
```

Functions: `rank`, `pct_rank`, `zscore`, `industry_rank` (cross-sectional),
`abs`, `clip`, `fillna`, `log`, `sqrt`, `sign`, `minimum`, `maximum`.
Comparisons with NaN are **False** (missing data fails a filter; opt out
per-filter with `na_pass: true`).  `not` inverts that literally —
`not (pe > 15)` *passes* unknown P/E; write `pe > 0 and pe < 15` when
unknown must stay out.  `==`/`!=` are rejected (meaningless on NaN
cross-sections); unknown features, unknown keys and non-boolean filters
are compile-time errors.

## Features (the registry)

* **price/volume** — `close` (rebuilt adjusted), `close_raw`, `ret_1d…252d`,
  `mom_63/126/252s21`, `vol_20/60/120d`, `ma_ratio_20/60/200`,
  `high252_ratio`, `max21`, `amount_ma20/60`, `turnover_ma20/60`;
* **valuation** — `pe_ttm`, `pe_static`, `pb`, `ps_ttm`, `pcf_ttm`, `peg`,
  `market_cap`, `float_market_cap`, `earnings_yield`;
* **fundamentals** (lxg statements, PIT by statutory announce date) —
  `roe`, `roa`, `gross_margin`, `net_margin`, `op_margin`, `rev_growth`,
  `profit_growth`, `revenue_ttm`, `profit_ttm`, `ocf_ttm`, `capex_ttm`,
  `fcf_ttm`, `assets`, `liabilities`, `equity`, `interest_debt`, `cash`,
  `debt_assets`, `cash_assets`, `fcf_yield`, `div_yield_ttm`;
* **A-share alt** — `nb_hold_chg_20/60d` (northbound), `margin_chg_20d`,
  `holder_num_chg`, `pledge_ratio`, `report_cnt_90d`,
  `buy_report_ratio_90d`, `insider_net_180d_mc`, `block_discount_180d`;
* **static** — `board`, `name`, `industry_l1/l2`, `listed_days`, `st_flag`.

Feature tables are built lazily and cached; alt features read one
per-symbol file each on first use.  Register your own with
`qf_screen.rules.register_feature`.

## Causality & data honesty

* **One evaluation, two modes.** Feature tables are as-traded (row `t` =
  day `t`'s close).  `run` decides on the as-of close; `schedule` evaluates
  row `t-1` and writes membership at `t` — the strictly-before `lag=1`
  convention of `qf_factor.framework.selection.screens`, so exported
  schedules have zero look-ahead (a unit test perturbs day-`t` data and
  asserts the `t` row does not move).
* **Neither provider's adjusted close is usable.** ak's `close` is
  unadjusted; lxg's 前复权 `close` equals the raw price on 1,740 of 2,446
  lxg symbols and leaves 1,312 of 1,802 bonus-share events unadjusted
  (measured 2026-11).  The store rebuilds the series from raw prices:
  documented lxg dividend events (cash + bonus per share) on their
  ex-dates, share-count splits gated on `total_shares` AND `float_shares`
  scaling together (the float test is what separates a bonus issue from a
  placement), raw returns otherwise.  Against lxg's own series on the 276
  symbols where it is self-consistent (1.1M symbol-days) this leaves 75
  days with a >15% discrepancy versus 761 for the raw price.
* **Traded value for ak-only names** is reconstructed as
  `turnoverrate/100 × float_market_capital` (within 0.2% of lxg's amount).
* **ST exclusion** uses the company-list snapshot **forward-only** (never
  backdated — a TODAY list says nothing about 2021).  `universe.st_band:
  true` opts into qf_factor's price-band detector for historical main-board
  ST state (~20 s, built once).
* **Fundamentals are announcement-stepped**: row `t` carries the latest
  report whose statutory deadline passed.  Coverage is the lxg finance
  subset (~765 of ~5,500 names) — watch the funnel; `data-status` reports
  the census.
* **Incomplete sessions**: a trailing session whose close count collapsed
  (a data update in flight, not a market move) is excluded from the
  default decision date and reported as a warning.

## Live mode (opt-in)

`--live` appends the akshare `stock_zh_a_spot_em` snapshot as a
provisional last row (live close/valuation/turnover; the adjusted chain
extends by `live/prev-raw`).  Disk-cached with a TTL; never touched in
offline mode or unit tests.  `store.board_members("industry")` serves the
东财 board constituents (day TTL).

## Layout

```
qf_screen/
├── data/            universe, lxg/ak providers, merged store, safe parallel map
├── rules/           feature panel, DSL, hygiene/technical/fundamental/alt features
├── presets/         five built-in YAML screens + loader/validator
├── engine.py        ScreenEngine: run / schedule / validate, funnel, explain
└── cli.py           qf-screen: list | run | pit | validate | data-status
```

Design notes and measured findings live in the module docstrings; the
original implementation plan is `PLAN.md`.
