# Implementation Plan: Expand Built-In Presets Library (45 New Presets → 50 Total)

*Approved from the Copilot session plan (2026-11-27); implementation record
appended at the end.*

## Problem & Approach

`qf_screen/presets/` ships 5 built-in YAML screens (`low_vol_defensive`,
`momentum_trend`, `quality_growth`, `smart_money_ashare`, `value_dividend`).
The engine already registers 60+ features across price/volume, volatility,
valuation, financial statements, A-share alternative data, and industry/board
metadata, plus cross-sectional DSL operators (`pct_rank`, `industry_rank`,
`zscore`, `clip`, `fillna`, ...) and `max_per_industry` diversification caps —
far more surface than five screens exercise.

We expand `qf_screen/presets/` by **45 distinct, well-documented YAML presets**
(50 total) across 8 quantitative strategy families, with full size/board
coverage (Small/Mid/Large-Cap, ChiNext, STAR, Beijing) and industry
diversification. Every preset is calibrated against the real store
(`/home/data/quant_free_data`) so it compiles cleanly and returns sensible,
non-empty results (`>= 5` members) — except where the store's own coverage
makes that impossible, which is documented and tested instead (see the
record: `bj_emerging_leaders`).

## Design Conventions & Guardrails

- **Schema compliance** — only keys in `_TOP_KEYS` / `_UNIVERSE_KEYS` /
  `_FILTER_KEYS` / `_RANK_KEYS`, only registered features and DSL functions;
  `name` matches the file stem.
- **Data-coverage honesty**:
  - Full-market screens use full-coverage features (`pe_ttm`, `pb`,
    `ps_ttm`, `pcf_ttm`, `peg`, `earnings_yield`, `div_yield_ttm`,
    `roe_best`, price/vol/momentum/cap) and mark optional lxg-statement
    balance checks with `na_pass: true`.
  - Statement-intensive screens note the ~765-symbol lxg subset in their
    `description` and apply base-effect/cash guards (`ocf_ttm > 0`,
    `net_margin_py`, `clip(...)` on growth).
  - Alt-data screens avoid strict reliance on the discontinued northbound
    feed (`nb_hold_chg_*`), using active feeds (`holder_num_chg`,
    `report_cnt_90d`, `buy_report_ratio_90d`, `margin_chg_20d`,
    `pledge_ratio`, `insider_net_180d_mc`, `block_discount_180d`) with
    `na_pass: true` on sparse optional legs.
- **Industry diversification** — broad screens set `max_per_industry`
  (typically 3–6) and/or use `industry_rank(...)` where appropriate.
- **Rank blocks stay finite where the funnel is broad**: `CombinedRanker`
  blends only the legs a name actually has, but the headline legs of a
  full-market screen are full-coverage features so the composite is finite
  for every passer.

## Catalog of 45 New Presets

### 1. Value & Income (7)
1. `deep_value_pb` — low P/B & P/E non-financial bargains.
2. `fcf_cash_cow` — high free-cash-flow yield with cash-backed profits.
3. `magic_formula` — Greenblatt (ROA + earnings yield, non-financial).
4. `high_dividend_yield` — high-yield main-board income, low-vol tilt.
5. `dividend_growth_quality` — payers with quality & growth tilt.
6. `low_ps_sales_value` — low P/S with a trend floor.
7. `low_pcf_cash_value` — low P/CF operating-cash value, non-financial.

### 2. Quality & Financial Strength (6)
8. `high_roe_compounders` — durable high-ROE moat compounders.
9. `net_cash_fortress` — cash-rich low-debt balance sheets.
10. `high_margin_moat` — pricing-power leaders.
11. `cash_earnings_quality` — low-accrual cash-backed profits.
12. `quality_at_reasonable_price` — full-market QARP.
13. `financial_blue_chips` — banks/brokers/insurers at low multiples.

### 3. Growth & GARP (5)
14. `garp_peg` — Peter Lynch GARP.
15. `hyper_growth_revenue` — high top-line growers off a real base.
16. `earnings_acceleration` — operating leverage (profit > revenue growth).
17. `growth_momentum` — CANSLIM-style growth + price strength.
18. `value_turnaround_growth` — cheap PEG + positive medium-term trend.

### 4. Technical, Momentum & Breakout (7)
19. `breakout_52w_high` — George–Hwang 52-week-high breakout.
20. `long_term_momentum_12m` — classic 12-1 momentum.
21. `short_term_momentum_3m` — fast 3-1 momentum with a turnover cap.
22. `steady_uptrend_low_vol` — risk-adjusted uptrend (mom / vol).
23. `volume_expansion_trend` — volume-confirmed trend surge.
24. `low_turnover_neglected` — neglected low-turnover value.
25. `volatility_compression` — quiet volatility contraction.

### 5. Mean Reversion & Pullback (4)
26. `uptrend_pullback` — short-term dip inside a long-term uptrend.
27. `short_term_reversal_quality` — 1-month oversold quality bounce.
28. `deep_oversold_value` — contrarian beaten-down value.
29. `quiet_pullback_low_vol` — calm dip in low-volatility uptrends.

### 6. Size & Board-Specific A-Share (7)
30. `small_cap_value` — 20–100亿 value.
31. `small_cap_quality_momentum` — 20–120亿 quality + trend.
32. `mid_cap_core_growth` — 100–500亿 core.
33. `large_cap_blue_chip` — ≥500亿 income core.
34. `chinext_growth_leaders` — ChiNext profitable growth & momentum.
35. `star_tech_leaders` — STAR hard-tech quality & trend.
36. `bj_emerging_leaders` — Beijing Stock Exchange liquid profitable leaders.

### 7. A-Share Alternative / Smart Money & Governance (5)
37. `chip_concentration` — shrinking shareholder count.
38. `analyst_consensus_buy` — sell-side coverage & buy consensus.
39. `margin_momentum_ashare` — rising margin financing balance.
40. `clean_governance_insider` — low pledge, no insider net selling.
41. `institutional_block_value` — block-deal activity at a modest discount.

### 8. Industry-Neutral & Multi-Factor Composite (4)
42. `industry_neutral_value` — sector-relative cheap & profitable.
43. `industry_neutral_momentum` — sector-relative winners.
44. `all_weather_multifactor` — Value + Quality + Momentum + Low-Vol.
45. `value_momentum_combo` — Asness value + momentum barbell.

## Todos

1. `write-plan-doc` — this file.
2. `presets-value-quality` — the 13 Value & Income + Quality presets.
3. `presets-growth-technical` — the 16 Growth & GARP + Technical/Momentum +
   Mean Reversion presets.
4. `presets-size-alt-multifactor` — the 16 Size & Board + Alt/Smart Money +
   Industry-Neutral/Multi-Factor presets.
5. `calibrate-and-test` — run every preset against `/home/data/quant_free_data`,
   tune thresholds until each returns `>= 5` sensible members (relaxing while
   preserving intent where the plan flagged risk: `institutional_block_value`,
   `bj_emerging_leaders`, `net_cash_fortress`); add a hermetic validation test
   for all built-in presets; run pytest (hermetic and `test_real_data.py`).
6. `update-docs` — README preset catalog table and summary count.

## Notes & Considerations

- `test_real_data.py::test_every_preset_is_nonempty_and_sane` runs every
  preset in `list_presets()` (except `smart_money_ashare`) and asserts
  `len(result.members) >= 5`, monotone funnel, decreasing scores.
- Calibration context (probed 2026-11-26): the market is in a deep drawdown
  (median `ma_ratio_200` 0.73; 376 names above the 200d MA), sell-side report
  counts are thin (`report_cnt_90d >= 5` matches 2 names — use `>= 2` like
  `smart_money_ashare`), and the default lxg+ak merge prices only ONE
  Beijing-board name (the 275 BJ equity dirs carry `xq`/`xtq` pages only, an
  opt-in provider), so `bj_emerging_leaders` cannot reach 5 members on this
  store — it ships anyway (the screen is correct; the data is absent), is
  excluded from the count-asserting tests like `smart_money_ashare`, and gets
  a targeted degradation test instead.
- Rank-expression shapes borrow from the mined qf_mine expression library
  (Amihud illiquidity `|ret| / amount`, ranked low-vol, ranked momentum,
  size `log(amount)`) translated onto this engine's feature registry.

---

## Implementation Record (2026-11-27)

Landed the same day, in one session, on top of `03d91bc` (v0.3 branch,
clean tree).

### What shipped

* **45 new presets** in `qf_screen/presets/` — the catalog above, verbatim
  names and theses.  All follow the house style: `name` = stem, a real
  `description` (data coverage, na_pass legs, statement-subset honesty),
  the shared hygiene block (auto-prepended), a `rank` block of
  full-coverage headline legs, `top_n` 20–30, `max_per_industry` 3–5 on
  broad screens.
* Rank blocks borrow the mined qf_mine shapes where they fit:
  Amihud illiquidity `abs(ret_21d) / amount_ma20` orders
  `low_turnover_neglected`'s tail, `mom / vol` ranks
  `steady_uptrend_low_vol`, `-log(market_cap_bn)` tilts
  `small_cap_quality_momentum` smaller.
* **`doc/plan/PLAN_presets.md`** (this file), the README preset catalog
  (family-grouped tables for all 50 + the `stmt` / `na_pass` /
  data-limited annotations) and the repo-tree preset line (50, 8
  families).

### Calibration (2026-11-26 decision row, 5,502 symbols)

A cross-sectional probe of every thresholded feature ran FIRST (coverage,
quantiles, pass counts), then every preset ran against the store:

* **48 of 50 presets cleared `>= 5` members on the first pass** — no
  threshold had to be moved after the probe.  Members/passers, tightest
  first: `analyst_consensus_buy` 12/12, `star_tech_leaders` 10/12,
  `margin_momentum_ashare` 14/14, `chinext_growth_leaders` 17/17,
  `high_margin_moat` 25/26, `financial_blue_chips` 20/21 … through the
  broad screens at their 20–30 `top_n`.  Every funnel monotone, every
  member list score-ordered.
* Thresholds set FROM the probe where the plan's sketch would have been
  too tight for the data: `analyst_consensus_buy` needs
  `report_cnt_90d >= 2` (only 2 names carry ≥5 reports in 90d; ≥2 is the
  `smart_money_ashare` convention), buy ratio ≥0.80 kept.
* `bj_emerging_leaders` ships at 0–1 members: the 275 BJ equity dirs
  carry only `xq`/`xtq` pages (opt-in provider), the default lxg+ak merge
  prices ONE Beijing name.  Same treatment as `smart_money_ashare`'s dead
  northbound leg: the description says why, targeted tests pin the
  graceful behavior (below).

### A real bug the library found (and fixed)

`engine.inspect`'s failed-rule card parsed every failed rule's
`rule_exprs` entry with `expr_names()` — but a universe filter
(`BoardFilter`) has no DSL expr; the slot carries its human description,
and `"boards: bj"` does not parse.  Every previously shipped preset used
board groups the inspected names PASSED, so the path had never run; the
first Beijing-only preset made any main-board name fail its boards rule
and crashed `inspect` (and `watch`'s preset-exit alert walks the same
slot).  Fix: `qf_screen.engine.rule_features()` — `expr_names` guarded by
`DslError`, returning `[]` for non-DSL text — used by both consumers;
regression-pinned by `test_inspect_survives_a_failed_boards_rule`.

### Tests

* **New `test/test_presets_library.py`** (hermetic, store-free): the
  catalog is exactly the 50 named presets; each loads, validates, compiles
  (boolean filters, numeric rank legs), carries the hygiene block in
  order, and is documented; the two data-limited presets say so in their
  descriptions; the boards-rule inspect regression.
* **`test_real_data.py`**: `DATA_LIMITED = {smart_money_ashare,
  bj_emerging_leaders}` replaces the single-name skip in the parametrized
  non-empty test and the daily-briefing `>= 1 members` loop (the section
  header is still asserted for every preset); new
  `test_bj_preset_degrades_gracefully_without_bj_prices` (funnel's boards
  row passes ≤3, nothing invented, funnel monotone).
* **Count-pin updates**: `test_v03_cli.py` inspect/report-JSON preset
  counts now read `len(built_in_presets())`; the real-data HTML-report
  markup assertion expects `len(list_presets())` plots (one per briefed
  preset); the v0.4 browser test scrolls the first bar-chart row into
  view before hovering (50 preset sections push it below the fold —
  tooltip behavior itself unchanged).
* **Results**: hermetic `294 passed` (39 s); real-data `75 passed`
  (123 s); `ruff check qf_screen test` clean.

### Not done, and limits

* The presets are calibrated to ONE session's cross-section (2026-11-26,
  a deep drawdown: 376 names above the 200d MA).  Trend-heavy screens
  will be short lists until the tape turns; that is the screens'
  honesty, and the daily report shows it.
* No historical `validate` run per preset was added to CI — the
  real-data suite pins snapshots, not paths; running 50 `validate`s
  per CI run was judged too slow.  `qf-screen compare` is the tool for
  that, on demand.
* The daily `report` / web briefing now runs 50 screens (≈20–30 s warm
  on this store) — the qf_tools web app and any cron that runs `report`
  should expect the longer run.
* `bj_emerging_leaders` stays data-limited until BJ daily prices land in
  the default merge (loading the `xq` provider or an lxg/ak BJ pull);
  the screen itself is ready.

### Extension (2026-11-27, later the same day): +4 "Undervalued + Tailwind"

A user request to encode the 低估值+顺风 framework ("low valuation +
catalyst", a GARP / Davis-Double-Play variation) added a 9th family of
4 presets on top of the 50: `undervalued_tailwind` (the core: cheap +
quality floor + mom/MA60/volume confirmation + na_pass growth guard),
`davis_double_play` (*stmt*: cheap + real statement growth + OCF + the
re-rating underway), `left_side_accumulation` (step 1 of the playbook:
deep value + paid cushion + stabilization, deliberately NO
confirmation leg), and `capital_inflection_value` (the flow-leg
tailwind: margin balance rising OR holders concentrating, OR so a name
missing one file can still pass via the other, no na_pass by design).
Calibrated the same way — cross-sectional probe first, then real-store
runs: 30/18/30/20 members, funnels monotone, scores ordered, on the
2026-11-27 tape (582→59 / 110→21 / 232→73 / 188→64).  The engine has
no per-name valuation history, so the framework's "own 30th
percentile" ideal is proxied by absolute multiple floors and said so
in the descriptions.  Catalog test pinned to 54; README counts and
family table updated.  The four files live in the package's first
family folder, `presets/undervalue_tailwind/` (named by file stem as
usual): `built_in_presets` / `preset_path` / `preset_origin` /
`init_preset` now discover `*/*.yaml` one directory deep through a
shared `_built_in_files()` (a top-level stem wins a collision), the
package-data glob ships `*/*.yaml`, and the library test pins the
folder layout.
