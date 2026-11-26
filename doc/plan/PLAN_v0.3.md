# `qf_screen` — v0.3 Plan (`lxg` + `akshare` only, no `xq`)

> Provenance: drafted 2026-11-26 in a Copilot CLI plan-mode session for this
> repo (three rounds of user feedback: "only need lxg or akshare data to do
> screen" → "save to md file" → "save a new name md file"). The session was
> still waiting at its approve prompt, so the file was never written there;
> the text below is that plan verbatim. The implementation record is the
> last section of this file.

## Review Summary of v0.2 (uncommitted working tree on top of `487cec5`)

The other agent did an **excellent** job implementing the v0.2 plan:
- **Tests & lint**: `ruff check` is clean, **111 unit tests** pass, and **13 real-data tests** pass in 15.1s.
- **All 5 critical v0.1 bugs are verified fixed**:
  1. `numexpr` wide-DataFrame zero corruption fixed (`momentum_trend` now passes 422 stocks on `trend` and yields 30 ranked members).
  2. `days[:, None]` 2D broadcasting fixed in `_div_yield_table` and `_window_sum` (`SH600519` `div_yield_ttm = 3.6%`).
  3. Alt-data bounded `ffill` + staleness warnings implemented (`table_cache.py` even replays staleness warnings on cache hits!).
  4. `schedule()` now shares `_rank_members` with `run()` and honors `top_n` (`apply_top_n=True`).
  5. `explain(symbol)` now shows per-rule expressions AND feature values for rejected symbols (`SH600519` shows `pe_ttm: 22.2`, `roe_best: 30.3%`, `div_yield_ttm: 3.6%`).
- **v0.2 features delivered**: Fingerprinted parquet caching (`table_cache.py`), `qf_factor` CSV bridge (`factor_bridge.py`), `roe_implied`/`roe_best`/`market_cap_bn`, `qf-screen features`, Markdown output, and human-friendly units (`亿`, `%`).

However, live testing against real `QUANT_DATA_DIR` data uncovered **4 real CLI/data bugs** and **5 major product gaps** holding `qf_screen` back from feeling like a complete daily A-share screening product:

### Bugs & Rough Edges Found in v0.2

1. **CLI `--top` bug (`qf_screen/cli.py:127`)**:
   - `_apply_overrides` checks `getattr(args, "top_n", None)`, but the CLI flag is `--top` (`args.top`). Passing `--top 50` on a preset with `top_n: 30` still caps at 30, and `--top 5` prints `30 members` in the summary before truncating the table.
2. **Preset-less ad-hoc screening is impossible (`qf_screen/cli.py:415`)**:
   - `qf-screen run --where "market_cap_bn > 500" --rank "zscore(roe_best)"` fails because `preset` is a required positional argument. Users cannot run a pure ad-hoc screen without dragging in an existing preset's hidden filters.
3. **`--format markdown --out FILE` silently ignores `--out`, and `--format md` is rejected (`qf_screen/cli.py:242-247`)**:
   - `_emit_result` returns early on `args.format == "markdown"` before checking `if args.out:`. Also `pit` and `validate` don't accept `--where`, `--rank`, `--boards`, or `--all-passers`.
4. **`--industry` forces `--live` even when local `industry_l1` is on disk**:
   - Offline `--industry 医药生物` should filter against local `lxg/industries.csv` (`industry_l1`) and only require `--live` when fetching 东财 real-time concepts/boards.

### High-Impact Product Gaps for v0.3 (strictly `lxg` + `akshare` data)

5. **`lxg` + `akshare` Preset Quality & Coverage Polish (no `xq` dependency)**:
   - Keep `qf_screen` strictly sourced from `lxg` and `akshare` (local `lxg`/`ak` files + opt-in live `akshare`).
   - Tune `quality_growth` to avoid 2,000%+ turnaround outliers (e.g., winsorized/capped growth or pairing with positive cash-flow / margin quality) and handle `lxg` statement partial coverage (765 symbols) transparently in `value_dividend` (e.g., `debt_assets` `na_pass` or explicit coverage note in funnel/summary).
   - Add an optional `akshare` financial-indicator / industry enrichment hook under `--live` (disk-cached) for users who want live `akshare` fundamentals/industry tags for symbols missing local `lxg` files.
6. **Daily Trader Workflows (`diff`, `inspect`, `report`, sector cap)**:
   - **`qf-screen diff PRESET [--prev DATE]`**: Show **Entered (+)**, **Exited (-)**, and **Rank movers (↑/↓)** vs the previous trading session or rebalance date.
   - **`qf-screen inspect SYMBOL`**: Multi-preset scorecard for a single stock ("which presets does `SH600519` pass/fail, why, and what are its valuation/quality/momentum/alt stats?").
   - **Industry concentration & `max_per_industry`**: Print sector breakdown in `run` summary (e.g., `有色金属 40%`) and support optional `max_per_industry` in `ScreenSpec` / YAML to prevent single-sector crowding.
   - **`qf-screen report [--presets ...] --out report.md`**: Generate a clean daily Markdown briefing across all presets (summary, diff, top picks, sector concentration, data freshness warnings).
7. **Institutional Validation & Preset Comparison (`validate` + `compare`)**:
   - Upgrade `qf-screen validate` with **turnover**, **annualized return / Sharpe / max drawdown**, **cumulative return curve**, and **benchmark spread** (e.g., equal-weight universe + CSI300/CSI500 when available).
   - Add **`qf-screen compare PRESET1 PRESET2 ...`** to run & rank presets side-by-side over the same historical window.
   - Cache `validate`/`pit` date windows so re-running validation doesn't take 12.6s reloading 5,323 CSVs.
8. **Sub-Second Warm CLI & Cache Operations**:
   - Warm `qf-screen run` currently takes **~4.0s** (`1.1s` store cache + `~2.7s` universe dir scanning & heavy imports). Cache the universe metadata census (fingerprinted on equity dir count + `company_list.csv` mtime) and lazy-import `qf_factor` / `pyarrow` where possible to target **<1s** warm runs.
   - Add **`qf-screen cache {status,clear,warm}`** so users can inspect cache size/freshness, clear stale parquets, or pre-warm caches after a daily data pull.

## Todos (v0.3)

1. **`v03-fix-cli-bugs`**:
   - Fix `args.top` vs `args.top_n` mismatch in `qf_screen/cli.py:127` so `--top N` overrides `spec.top_n` consistently.
   - Allow preset-less ad-hoc runs (`qf-screen run --where "..." [--rank "..."]`) using a clean default A-share hygiene base spec.
   - Fix `--format markdown --out FILE` to actually write the Markdown file, alias `md` -> `markdown`, enable offline `--industry` filtering via local `industry_l1`, and propagate `--where`/`--rank`/`--boards`/`--all-passers` to `pit` and `validate`.
2. **`v03-lxg-ak-coverage-and-presets`**:
   - Keep all screening strictly on `lxg` + `akshare` (no `xq` dependency); default `factor_bridge.py` providers to `lxg` first.
   - Tune `quality_growth.yaml` (prevent 2,000%+ base-effect turnaround outliers from dominating top ranks) and `value_dividend.yaml` (clear `lxg` statement coverage behavior), and add optional disk-cached `--live` `akshare` industry-tag fallback for symbols without local `lxg/industries.csv`.
3. **`v03-daily-workflows`**:
   - Add `qf-screen diff PRESET [--as-of D] [--prev D]` (entered/exited/rank movers).
   - Add `qf-screen inspect SYMBOL [--as-of D]` (cross-preset pass/fail + full stock scorecard).
   - Add industry concentration breakdown to `run` output and optional `max_per_industry` diversity cap in `ScreenSpec` / `ScreenEngine._rank_members`.
   - Add `qf-screen report [--presets ...] [--out FILE]` daily Markdown briefing generator.
4. **`v03-validate-analytics`**:
   - Enhance `ValidationReport` & `qf-screen validate` with period turnover, cumulative return, Sharpe ratio, max drawdown, and benchmark comparison.
   - Add `qf-screen compare PRESET ...` for side-by-side preset validation and cache custom date-window store loads.
5. **`v03-startup-cache-ops`**:
   - Cache `Universe` directory/metadata scan (fingerprinted) and defer heavy imports so warm CLI runs approach <1s.
   - Add `qf-screen cache {status,clear,warm}` subcommand.
6. **`v03-tests-and-docs`**:
   - Add unit & real-data regression tests for CLI fixes, `lxg`+`akshare` preset tuning, `diff`/`inspect`/`max_per_industry`/`report`, enhanced `validate`/`compare`, and `cache` CLI.
   - Update `PLAN.md` and `README.md`, and verify everything with `/opt/quantfree/bin/pytest` and `/opt/quantfree/bin/ruff`.

## Notes

- Strictly `lxg` + `akshare` data sources only (no `xq` / Xueqiu dependency).
- Preserve strict PIT causality across all snapshot, diff, schedule, and validation modes.

---

## Implementation record (2026-11-26)

Implemented against the real `QUANT_DATA_DIR` store; `qf-screen 0.3.0`,
173 unit + 23 real-data tests green, `ruff` clean.  Measured numbers are
from this machine with a warm page cache.

### 1. `v03-fix-cli-bugs`

* **`--top`** — `_apply_overrides` read `args.top_n`, a name the parser
  never set, so the flag did nothing.  It now replaces the preset's
  `top_n` (`0` = no limit) on `run` / `pit` / `validate` / `compare`, and
  the summary counts what is shown.  `momentum_trend --top 50` → 50 rows
  (was 30); `--top 0` → all 130 passers; `--top 5` prints "5 members".
* **Preset-less runs** — `qf-screen run --where "market_cap_bn > 2000"
  --rank "pct_rank(roe_best)" --top 20`.  The base is `ScreenSpec.adhoc`:
  the required hygiene block over the whole universe, no ST, listed ≥ 1
  year.  No preset and no `--where` / `--rank` is an error, not a dump of
  the market.
* **`--format markdown --out FILE`** writes the document (it returned
  before reaching `--out`); `md` is an alias; `--out` now honours
  `--format` everywhere (CSV / JSON / Markdown).
* **`--industry` offline** — matched against the local 申万
  classification of `lxg/industries.csv`, all three levels, exact name
  first then substring; a miss names the near misses and says `--live`
  adds the 东财 boards.  `--industry 医药生物` restricts to the 149
  lxg-classified names without touching the network.
* **`pit` / `validate`** accept `--where`, `--rank`, `--boards`,
  `--top`, `--max-per-industry` and `--all-passers`.

### 2. `v03-lxg-ak-coverage-and-presets`

* **Factor bridge** defaults to the `lxg` and `ak` trees
  (`DEFAULT_PROVIDERS = ("lxg", "ak")`).  A family that lives elsewhere
  (`xq/finance_Q_factor.csv`, `xtq/…`) is opt-in through
  `QF_SCREEN_FACTOR_PROVIDERS`, and the bridge warns where it sits.
* **`quality_growth`** — measured on 2026-11-24: the top of the list was
  1,992 % / 874 % / 2,551 % / 762 % profit growth, nearly all of it growth
  off a prior-year net margin of 4–7 %.  Three changes, all in the YAML:
  a base-effect guard `net_margin_py >= 0.08` (a new feature: the net
  margin of the TTM report known 252 sessions earlier, point-in-time by
  construction), a cash leg `ocf_ttm > 0`, and growth clipped at ±100 %
  inside the rank.  31 → 23 passers; the 1,992 % name is out and the list
  is no longer in raw-growth order.
* **`value_dividend`** — its `debt_assets < 0.6` leg kept only the ~765
  names with lxg statements, so a full-market quality leg fed a
  statement-only balance leg: 13 passers.  The leg is `na_pass` now
  (45 passers) **plus** a `not_financial` leg on a new static `financial`
  flag: `na_pass` alone would have admitted 新华保险 and 招商证券, whose
  balance sheets are levered by construction.  23 of the 30 members are
  names the old screen could not judge.
* **Coverage made visible** — every funnel row carries an `unknown`
  count (names where every feature the rule mentions is missing), `†`
  marks `na_pass` rules, and the run prints a note per such rule
  ("balance: 4708 names passed WITHOUT data").
* **`--live` industry tags** — `CNDataStore.enrich_industry_tags()` gives
  the ~3,400 names without `lxg/industries.csv` the 东财 industry board as
  their `industry_l1` (day-TTL cache; a local tag is never overwritten).

### 3. `v03-daily-workflows`

* `diff PRESET [--as-of D] [--prev D]` — entered / exited / rank movers
  against the session before (or a given date), with retention.  Both
  sides are ordinary `run` snapshots, so the rules and the ranking are the
  same ones that produced each list.
* `inspect SYMBOL [--presets …]` — every preset's verdict; for a reject,
  each failed rule with its expression and the values it read; for a
  member, its rank and score; for a name that clears every rule and still
  misses the list, its rank among the whole passing pool (and whether an
  industry cap displaced it).  Plus a 37-feature scorecard with each
  feature's cross-sectional percentile.
* Industry mix on every `run`; `max_per_industry` in YAML and
  `--max-per-industry N`.  The cap is applied while the ranked list is
  filled, so the next-best names take the freed slots and the list stays
  `top_n` long; the names it displaced are reported.
* `report [--presets …] [--out FILE] [--format markdown|json]` — overview
  (members / entered / exited / retained / top industry), one section per
  preset (change, sector mix, top picks), every data warning.

### 4. `v03-validate-analytics`

* `validate` adds the path of the equal-weight portfolio that follows the
  schedule: annualized return, volatility, Sharpe (risk-free 0), max
  drawdown, cumulative return, turnover per rebalance, the excess over the
  candidate pool (annualized, information ratio, tracking error) and the
  CSI 300 / CSI 500 lxg index files as benchmarks.  `--curve FILE` writes
  the daily cumulative path; `report.per_date` carries the period return,
  turnover and cumulative values per rebalance.
* `compare PRESET … --start --end` — metrics × presets over one window.
  Each preset's excess is against its own candidate pool; the reference
  columns are one whole-market benchmark (every name with a price the
  session before, equal-weighted) and the indexes, so presets that screen
  different boards are compared like for like.
* **Cache windows** — the merged store was already cached per window, but
  every derived table shared one file per key, so a validation window
  overwrote the daily window's tables.  Measured: a `run` right after
  `validate` took 12.2 s.  Derived tables now live under
  `tables/<window>/`: `validate` again on the same window 0.8 s (first
  time 15.6 s), and `run` after it 0.9 s.

### 5. `v03-startup-cache-ops`

Warm `run`, per preset, end to end:

| | before | after |
|---|---|---|
| `value_dividend` | 3.64 s | 0.87 s |
| `momentum_trend` | ≈3.7 s | 0.80 s |
| `quality_growth` | ≈3.6 s | 0.88 s |
| `low_vol_defensive` | ≈3.5 s | 0.82 s |
| `smart_money_ashare` | not measured | 1.00 s |

("before": measured at the start of the session — the first row as a CLI
wall time, the others in-process plus startup; the plan's own figure was
"~4.0 s".)

What it took (profile first: imports 0.34 s, universe 1.0 s, store cache
1.5 s, features ~1 s):

1. **Universe census cached** behind a fingerprint of its own inputs —
   1.0 s → 0.03 s.
2. **Wide tables as one flat parquet column.**  A `date x symbol` table
   has ~600 rows and ~5,500 columns; as parquet that is 5,500 tiny column
   chunks, 0.12 s per 16 MB table.  As one column: 0.01 s.  The 16 store
   tables: ~1 s → ~0.1 s.
3. **Price features cached behind the store's fingerprint** (only the
   builders that are pure functions of the base tables) — `max21` alone
   was 0.53 s per run.
4. **`yfinance` import deferred** — it rode in with a `quant_free` symbol
   helper used only when the universe census is built (0.11 s).
5. File censuses through plain `os.stat` instead of pathlib: the store
   fingerprint's ~24,000 `stat` calls cost 0.18 s → 0.03 s.

What is left of a warm run: ~0.3 s interpreter + pandas (the floor),
0.03 s universe, 0.18 s store, 0.25–0.5 s feature tables.
`pyarrow` cannot be deferred: pandas itself imports it.

`qf-screen cache status` (per kind and per window; the daily store marked
fresh or STALE against the current source census), `cache clear [--what
KIND] [--dry-run]`, `cache warm [--presets …] [--start --end]`.  Files of
an older layout are reported as `obsolete`.

### 6. `v03-tests-and-docs`

173 unit (111 before + 62 new, `test/test_v03_{cli,engine,cache}.py`) and
23 real-data (13 + 10) tests; `ruff` clean; `README.md` rewritten for
0.3.0; a v0.3 summary appended to `PLAN.md`.

### Found and fixed on the way (each pinned by a test)

* Derived tables thrashed between the daily window and validation
  windows (above); the statement payload had the same problem between
  symbol sets.
* A missing industry came back from the static feature as the text
  `"nan"` — `industry_rank` ranked it as an industry of its own and an
  industry cap counted names against it.
* Fingerprints used the newest source mtime, which does not move when a
  file is restored with an old timestamp; they are mtime sums now.
* `compare`'s "universe" reference column was the FIRST preset's candidate
  pool — main boards only next to a preset screening all of them, and
  different from what that preset's own `validate` printed as its
  baseline.  It is an explicit whole-market benchmark now.

### Decisions that differ from the plan's letter

* **Factor bridge**: the plan says "default providers to `lxg` first" and,
  in its notes, "no `xq` dependency".  Read together, `xq` is not in the
  default at all.  To keep it as a last-resort fallback instead, set
  `DEFAULT_PROVIDERS = ("lxg", "ak", "xq", "xtq")` in
  `qf_screen/data/factor_bridge.py`.
* **`quality_growth` base floor 8 %, not 5 %**: at 5 % the 1,992 % name
  stayed at #1.
* **`value_dividend`**: the `not_financial` leg is an addition — the plan
  offered `na_pass` or a coverage note.
* **Universe fingerprint**: equity-dir count + the industries census +
  `company_list.csv`, as a mtime sum — the plan named dir count +
  `company_list.csv` mtime, which misses a new `industries.csv` inside an
  existing directory.

### Not done, and limits

* **The `akshare` financial-indicator hook was not built.**  The plan's
  "financial-indicator / industry enrichment hook under `--live`" is
  realised for industry tags only.  Live fundamentals would be a new data
  source — there is no on-disk layout for akshare fundamentals in the
  store — with its own point-in-time questions; it should be its own plan.
* **`--live` was not exercised against the network.**  Both live paths
  (spot row, industry boards) are covered by unit tests on an injected
  fake akshare module; no request was made in this session.
* **Benchmarks lag**: the lxg index files end 2026-10-30 on this store, so
  a validation window past that date gets a shortened benchmark curve and
  a note saying so.
* **Industry tags are today's classification.**  A historical schedule is
  capped with them too — the one non-point-in-time input of the engine.
  Names with no tag at all are never capped (they share no industry).
* **Timings are this machine's**, with the page cache warm.
* The Copilot session that drafted this plan is still waiting at its
  approve prompt; approving it there would overwrite this file with the
  bare plan and drop this record.

### Housekeeping performed

* `qf-screen cache clear --what obsolete` removed 52 entries (1.7 GB)
  written by the v0.2 layouts and by this session's intermediate formats —
  unreadable by 0.3.0.
* The editable install was refreshed (`pip show qf-screen` → 0.3.0).
* The v0.2 work was still uncommitted when this began; it and the v0.3
  changes were committed together right after the record was written
  (`git log` has the commit).
