# Implementation Plan: `qf_screen` v0.4 — Review of v0.3 & Productization Roadmap

## 1. Review Summary of v0.3 (working tree on top of `487cec5`)

v0.3 delivered a major step forward toward a daily A-share screening product:
- **Tests & lint**: `ruff check qf_screen test` is clean; **173 unit tests** pass and **23 real-data tests** pass in 19.1s.
- **Performance**: Warm `qf-screen run` dropped from ~4.0s (v0.2) to **1.29–1.37s** total wall time (`store` loads from cache in **0.2–0.4s**); cached `compare` across all 5 presets loads the historical window in **0.2s**.
- **Verified v0.3 capabilities (strictly `lxg` + `akshare`)**:
  - Preset-less ad-hoc runs (`qf-screen run --where "..." --rank "..." --format md`) work cleanly.
  - Offline `--industry` filtering works against local `lxg/industries.csv` (`industry_l1`).
  - `qf-screen diff` reports entered, exited, retention %, and rank movers (`momentum_trend`: +4 / -4, 87% retention).
  - `qf-screen inspect SYMBOL` reports multi-preset pass/rank/fail outcomes with rule values + a 37-feature cross-sectional percentile scorecard.
  - `qf-screen validate` and `qf-screen compare` report forward returns, annualized return, volatility, Sharpe, max drawdown, CSI300/CSI500 benchmarks, excess return, information ratio, and turnover.
  - `qf-screen cache {status,clear,warm}` inspects, clears, and pre-warms caches cleanly.

### Bugs & Gaps Found in v0.3
1. **`qf-screen report --presets a,b` fails on comma-separated input (`qf_screen/cli.py`)**:
   - `--presets` uses `nargs="+"` without splitting commas (inconsistent with `--boards`, `--symbols`, `--horizons`), so `--presets momentum_trend,value_dividend` raises `preset 'momentum_trend,value_dividend' not found`.
2. **`analytics.simulate()` NaN fragility & zero-cost assumption (`qf_screen/analytics.py:79`)**:
   - `path = growth.mean(axis=1)` turns the entire portfolio path into `NaN` if any member has a missing/delisted price mid-period.
   - Validation assumes **0 bps** transaction costs and unconstrained entry even on high-turnover screens (`momentum_trend` 73.8%, `smart_money_ashare` 80.6% monthly turnover). A-share realism requires configurable transaction costs (`--cost-bps`, covering sell stamp duty + commission + slippage) and an optional limit-up (涨停) entry guard.
3. **Unbounded cache growth (`941 MB` after a few runs)**:
   - `cn/qf_screen_cache/` grew to **941.4 MB** across 64 entries (`feature`: 427 MB, `history`: 256 MB, `store`: 172 MB) with no `--stale`, `--older-than`, or `--max-gb` LRU pruning.

---

## 2. v0.4 Productization Plan (Strictly `lxg` + `akshare`, No `xq`)

### Todo 1: `v04-fix-sim-and-cli` — Fix `report --presets` & Harden `analytics.simulate()`
- **CLI comma-splitting (`qf_screen/cli.py`)**:
  - Accept both space-separated (`--presets a b`) and comma-separated (`--presets a,b`) values in `report` (and `cache warm --presets`).
- **Mid-period NaN / delisting safety (`qf_screen/analytics.py`)**:
  - Carry last finite price within each holding block (or use `np.nanmean` with explicit fallback when a member stops trading mid-period) so one delisted/missing tail never poisons the entire portfolio path with `NaN`.
- **A-share execution realism (`qf_screen/analytics.py`, `qf_screen/engine.py`, `qf_screen/cli.py`)**:
  - Add `--cost-bps` (default `0`, e.g. `15` bps turnover cost = commission + 5 bps sell stamp tax + slippage) to `validate` and `compare`, reporting both gross and net annualized return / Sharpe when `cost_bps > 0`.
  - Add optional `--skip-limit-up` guard in `validate` / `compare` to skip entries where the stock opened/closed at daily limit-up (+9.8% main board, +19.8% ChiNext/STAR) on entry day.

### Todo 2: `v04-user-presets-and-watchlists` — User Preset Registry & Watchlist Risk Monitor
- **User preset discovery (`qf_screen/presets/__init__.py`)**:
  - Auto-discover user YAML presets from `$QF_SCREEN_PRESETS_DIR` (default `~/.config/qf_screen/presets/`) alongside built-in presets across `list`, `run`, `diff`, `inspect`, `report`, `compare`, and `pit`.
  - Mark user presets clearly in `qf-screen list` (built-in vs user) and allow user presets to override or extend built-ins cleanly.
  - Add `qf-screen init-preset NAME [--from PRESET] [--out PATH]` to scaffold a documented custom YAML screen ready to edit.
- **Watchlist & portfolio risk monitor (`qf_screen/watch.py`, `qf_screen/cli.py`)**:
  - Add `qf-screen watch --symbols A,B` or `--watchlist FILE` (YAML/TXT list of symbols + optional target preset / custom risk rules).
  - Surface daily risk alerts per holding:
    - ST/*ST flag triggered
    - Pledge ratio spike (`pledge_ratio >= 0.30`)
    - Trend break (`ma_ratio_60 < 1` or `ma_ratio_200 < 1`)
    - Liquidity collapse (`amount_ma20 < 5e7`)
    - Preset exit alert (if a target `--preset` is specified and the holding no longer passes)
  - Support `--format table|markdown|json` and `--fail-on-alert` (exit code 2 when any alert fires).

### Todo 3: `v04-html-report-and-charts` — Self-Contained Interactive HTML Reports
- **Zero-dependency HTML renderer (`qf_screen/html_report.py`, `qf_screen/cli.py`)**:
  - Add `--format html` to `report`, `validate`, and `compare`.
  - Generate a single, self-contained HTML file (no external CDN/JS/CSS dependencies) containing:
    - **Embedded SVG charts**: cumulative equity curve vs universe/CSI300/CSI500, drawdown curve, and industry concentration horizontal bar chart.
    - **Interactive tables**: lightweight inline JS for column sorting and symbol/industry search filtering.
    - **Diff badges**: green `+NEW`, red `-OUT`, and rank-delta pills (`↑+5` / `↓-3`) in `report`.

### Todo 4: `v04-automation-and-history` — Daily Snapshot Archive, Exit Codes & Webhooks
- **Snapshot history archive (`qf_screen/history.py`, `qf_screen/cli.py`)**:
  - Add optional `--save-history [DIR]` (default `~/.local/share/qf_screen/history/`) on `run` and `report` to append daily `(asof, preset, symbol, rank, score)` records (idempotent per `(asof, preset)`).
  - Add `qf-screen history PRESET [--days 30]` to show persistence/streak counts (how many consecutive sessions each current member has stayed in the top-N).
- **Automation exit codes & webhooks (`qf_screen/notify.py`, `qf_screen/cli.py`)**:
  - Add `--fail-on-change` to `qf-screen diff` (exit code 2 when entered/exited is non-empty) for cron/CI triggers.
  - Add `--webhook-format json|feishu|dingtalk|slack` (and optional `--webhook-url` / `$QF_SCREEN_WEBHOOK_URL`) on `diff`, `watch`, and `report` so post-close 15:30 CST jobs can push concise alerts without extra glue scripts.

### Todo 5: `v04-cache-gc-and-release` — Cache Pruning/GC, Tests & Docs
- **Cache garbage collection (`qf_screen/cache_ops.py`, `qf_screen/data/table_cache.py`, `qf_screen/cli.py`)**:
  - Extend `qf-screen cache clear` with:
    - `--stale`: remove only superseded/mismatched store & derived caches while keeping the active fresh window.
    - `--older-than DAYS`: prune entries not accessed/modified within `DAYS`.
    - `--max-gb N`: LRU-evict oldest cache entries until total size is under `N` GB.
    - `--dry-run`: preview what would be deleted and bytes reclaimed.
- **Verification & documentation**:
  - Add focused hermetic unit tests (`test/test_v04_product.py`) covering: comma-separated `report --presets`, `simulate()` mid-period NaN + `cost_bps` + limit-up guard, user preset discovery & `init-preset`, `watch` alerts & exit codes, HTML/SVG generation (valid markup, zero external URLs), snapshot history idempotency, webhook payload formatting (mocked HTTP, offline-safe), and cache GC (`--stale`, `--older-than`, `--max-gb`, `--dry-run`).
  - Expand `test/test_real_data.py` (`@pytest.mark.data`) to smoke-test `report --presets a,b`, `watch`, `--format html`, and `validate --cost-bps 15`.
  - Update `README.md` and bump version to `0.4.0` in `pyproject.toml` / `qf_screen/__init__.py`.
  - Verify with `/opt/quantfree/bin/ruff check qf_screen test`, `/opt/quantfree/bin/pytest test/ -m unit`, and `/opt/quantfree/bin/pytest test/ -m data`.

---

## 3. Design Constraints & Non-Goals
- **Data sources**: Strictly `lxg` and `akshare` (local-first `QUANT_DATA_DIR`, `--live` strictly opt-in; no `xq` dependency).
- **Causality**: All backtest/validation/schedule paths remain strictly causal (`t-1` signals -> `t` holdings).
- **Offline safety**: Unit tests and default CLI runs never make network calls (webhooks and `--live` are strictly opt-in).

---

## Implementation record (2026-11-26)

Implemented against the real `QUANT_DATA_DIR` store; `qf-screen 0.4.0`,
**219 unit** (173 + 46 in `test/test_v04_product.py`) and **30 real-data**
(23 + 7) tests green, `ruff` clean.  Numbers below were measured on this
machine with a warm page cache, on the store as it stood on 2026-11-26.

### 1. `v04-fix-sim-and-cli`

* **`--presets a,b`** — a shared argparse action splits commas on
  `report`, `inspect` and `cache warm`; spaces, commas and a repeat all
  mean the same list (`a b`, `a,b`, `a b,b`).
* **`simulate()`** carries the last finite price forward *inside* each
  holding block, so a raw table with a suspension or a delisted tail
  poisons no date (the caller's forward-filled table and a raw one now
  give the same path).  A period left with nothing holdable is a period
  in cash — flat rows — where it used to be a hole in the path.
* **`--cost-bps BPS`** (`validate`, `compare`) is the cost **per side**
  of a trade: the initial purchase, each rebalance's sells and buys (the
  share of the old list sold plus the share of the new list bought, from
  the list overlap) and the final liquidation.  Re-weighting the names
  that stay is not charged; the pool and the indexes stay gross.  The
  report carries `stats["portfolio_net"]`, the `portfolio_net` curve,
  `report.net` (excess, information ratio, `cost_drag_ann`) and
  `per_date["cost"]`; `compare` puts each `*_net` row under its gross
  twin.
* **`--skip-limit-up`** refuses an entry whose raw close sits at its
  board's daily limit against the previous *traded* close (10 % main,
  20 % ChiNext / STAR, 30 % Beijing, less a 0.2 % tolerance for the
  rounding of the limit price).  That share of the book stays in cash for
  the period; `report.skipped` and `per_date["skipped"]` count it.
* Measured, `momentum_trend`, 2026-01-05..2026-09-30, monthly, 15 bps a
  side with the guard on: 2 of 240 entries refused (丰元股份 +10.01 % on
  2026-04-30, 弘亚数控 +9.98 % on 2026-06-30 — both checked by hand
  against the raw closes); annualized return −30.2 % → −32.2 %, Sharpe
  −1.48 → −1.60, max drawdown −26.3 % → −27.0 %, at 73 % turnover a
  month.  That is the arithmetic: 2 × 0.73 × 15 bps × 7 rebalances + two
  sides ≈ 1.8 % over eight months.

### 2. `v04-user-presets-and-watchlists`

* **Registry** — `list_presets()` is the built-ins plus every
  `*.yaml` / `*.yml` in `$QF_SCREEN_PRESETS_DIR` (default
  `~/.config/qf_screen/presets/`), named by file stem; a user file named
  like a built-in replaces it.  `list` marks `built-in`, `user: <file>` or
  `user preset, REPLACES the built-in`, and lists a preset that does not
  load with the reason.  `init-preset NAME [--from PRESET] [--out PATH]
  [--force]` writes a commented file and loads it back through the real
  loader before returning.
* **Loader** — rejects unknown keys inside `universe` and inside each
  `filter` / `rank` entry, a non-boolean `na_pass`, a rank entry without
  an expression or with a non-positive weight, an unknown board, a bad
  `top_n` / `min_listed_days` / `symbols`.  Every built-in still loads,
  and every built-in round-trips through the scaffold.
* **`watch`** — the five rules of the plan (`st`, `pledge >= 0.30`,
  `trend_break` on the 60- or 200-day mean, `liquidity < 5e7`,
  `preset_exit`), plus `suspended` (see decisions).  Table, Markdown and
  JSON; `--fail-on-alert` exits 2.  A watchlist file (YAML mapping, a
  bare list, or plain text) carries `preset`, `symbols`, `rules` (custom
  DSL rules with a severity), `disable` and `thresholds`.
* Measured: `watch --symbols SH600519,SZ000858,SH600036 --preset
  value_dividend` — 1.02 s; without a preset 0.72 s.

### 3. `v04-html-report-and-charts`

* `--format html` on `report`, `validate`, `compare`; `--out` writes the
  page, otherwise it goes to stdout.  Sizes: the briefing 77 KB (five
  presets, top 8), a validation 213 KB, a five-preset comparison 296 KB.
* Briefing: headline tiles, an overview table, one section per preset —
  the industry-concentration bar chart beside the entered / exited lists
  with their `+NEW` / `−OUT` badges, and the member table with badges and
  rank-move pills (`↑+5` / `↓−3`).  Validation: tiles (gross, with the net
  figure under each), the cumulative curve and the drawdown, the
  statistics, horizon and rebalance tables.  Comparison: tiles, one
  cumulative chart and one drawdown chart (each preset a line), the
  metrics-by-preset table (click a preset to rank the metrics by it).
* Charts follow one fixed method: presets take categorical slots in the
  order named (never by rank, never a ninth colour — past eight the chart
  stops and says so), the pool and the indexes are grey context lines
  with their own dash, a net path is its gross twin's colour dashed,
  industry bars are one hue.  The palette was run through the
  colour-vision validator for both surfaces (worst adjacent pair ΔE 11.8
  under protanopia on white, 10.1 under deuteranopia on the dark surface);
  four light slots sit below 3:1 on white, which the direct end-labels
  and the table twin relieve.
* Interaction: column sorting, one filter box for every member table
  (live counts, sections with no match dimmed), a crosshair that snaps to
  the nearest session with a readout of every series (keyboard: Home /
  arrows / End), legend click to isolate a series, a tooltip on every bar,
  a "Line patterns" toggle.  Dark mode is a selected palette; print and
  forced-colours apply the line patterns and hide the controls.
* Verified by rendering in headless Chromium, light and dark, with a clean
  console and every interaction exercised (`test_html_reports_work_in_a_browser`,
  skipped where no Chromium is installed).  That is where four defects
  were caught that markup checks cannot see: the `<g class="series">`
  group was never closed, the lines were plotted at their *values* rather
  than pixel heights, an SVG group ignored `element.hidden` (the crosshair
  never showed), and a generated CSS rule escaped its `.patterns` scope.

### 4. `v04-automation-and-history`

* **Archive** — one CSV per preset (`asof,symbol,rank,score`) under
  `$QF_SCREEN_HISTORY_DIR` (default `~/.local/share/qf_screen/history/`);
  `--save-history [DIR]` on `run` and `report`; a second run of the same
  session replaces that session's rows (written atomically).  `report`
  archives the full list whatever `--top` shows.
* **`history PRESET [--days N]`** — per current member: `streak`,
  `present`, `stayed`, `first_seen`, the rank and the score; the names
  that left inside the window, with the run they had and the rank they
  held; `new_today`.  Table, Markdown, JSON.
* **Exit codes** — 2 for `diff --fail-on-change` and `watch
  --fail-on-alert`; 3 for a webhook that could not be delivered (it
  outranks 2).
* **Webhooks** — `--webhook-format json|feishu|dingtalk|slack`,
  `--webhook-url`, `--webhook-secret` on `diff`, `watch`, `report`.  The
  format flag is the opt-in switch.  A 200 that is a refusal (飞书 `code`,
  钉钉 `errcode`, Slack `"ok": false`) is a failure.

### 5. `v04-cache-gc-and-release`

* `cache clear --stale | --older-than DAYS | --max-gb N [--what KIND]
  [--dry-run]`, planned by `cache_gc_plan`, run by `cache_remove`;
  `cache status` now judges validation-window caches too and prints the
  pruning hint.
* Measured on the real cache: with 0.4.0 installed, `--stale` planned
  exactly the four `_v030` merged-store caches (612.6 MB) and nothing
  else — the derived tables of those windows stayed, and were still cache
  hits for 0.4.0.  On the pruned cache (104 entries, 1.1 GB), `--stale`
  and `--older-than 30` find nothing, and `--max-gb 0.5` would evict 64
  least-recently-used entries (580 MB) to reach 497 MB.  Each plan costs
  0.3–0.4 s.

Warm wall times, unchanged from v0.3 for what existed:

| command | seconds |
|---|---|
| `run`, per preset | 0.80–1.02 |
| ad-hoc `run --where --rank` | 0.72 |
| `run --save-history` | 0.78 |
| `diff` | 0.92 |
| `inspect` | 2.19 |
| `report` (Markdown / HTML) | 2.50 / 2.47 |
| `watch` (no preset / with) | 0.72 / 1.02 |
| `history` | 0.36 |
| `validate --cost-bps 15 --skip-limit-up` (table / HTML), warm window | 0.75 / 0.76 |
| `compare` of three presets, HTML, warm window | 1.01 |
| `cache status` / a pruning plan | 0.32–0.39 |

### Decisions that differ from the plan's letter

* **`--cost-bps` is per side, not per unit of turnover.**  The plan's
  "15 bps turnover cost = commission + 5 bps sell stamp tax + slippage"
  reads like an all-in round trip; at 15 bps a side a round trip costs
  30.  The choice is spelled out in `--help` and the README: for the
  all-in figure, pass half of it (`--cost-bps 7.5`).
* **The limit-up guard tests the close only.**  The plan says "opened or
  closed"; the store keeps raw closes for every name and opening prices
  only for the lxg layout-A subset, and the validation buys at the close —
  a name that opened limit-up and closed lower was buyable.
* **`watch` adds `suspended` and an unknown-symbol alert.**  Not in the
  plan.  A missing value never alerts, which is right for every
  comparison rule — and wrong for a holding that cannot be priced at all:
  a long suspension freezes its averages and dries up its liquidity
  without ever crossing a threshold, and a mistyped symbol would drop out
  of the monitor silently.
* **A watchlist may `disable` rules and re-tune `thresholds`**, and
  custom rules carry a severity: "custom risk rules" needed a way to
  replace a built-in that does not fit the book.
* **Webhooks**: the format flag is the switch (a URL in the environment
  alone sends nothing — the plan's own constraint that default runs make
  no network call), `--webhook-secret` was added because 飞书 签名校验 and
  钉钉 加签 need it, and a failed delivery is exit code 3.
* **`history` lists the names that left** as well as the ones that
  stayed, and counts over *archived* sessions (a day skipped is not a
  broken streak), saying so when member counts swing across the window.
* **`--stale` also removes the derived tables of a window** whose every
  store cache is stale or broken.  They are aligned to it; some may still
  be valid, but the next run of that window would rebuild them in place
  anyway, so nothing is lost — and the fresh daily window is never
  touched.
* **The preset loader is stricter** (unknown keys at the `universe`,
  `filter` and `rank` level, …): the plan asks for hand-edited presets,
  and a typo'd `weigth` or `na_pas` used to switch a knob off silently.

### Found and fixed on the way (each pinned by a test)

* `simulate()` left a hole in the path for a period with no holdable
  name; it is a period in cash now.
* A custom watch rule naming an unknown feature raised a `KeyError` from
  the panel, and a bad watchlist file a traceback; both are
  `qf-screen watch: <why>` now.  An unknown threshold passed to the
  library was accepted silently.
* YAML 1.1 reads `1e8` as a string, so `thresholds: {liquidity: 1e8}` was
  refused as "not a number".
* A line chart raised `IndexError` for a series shorter than the axis (a
  benchmark file that ends early).
* The lazy export `qf_screen.ExprRule` named a class that never existed.
* The member table's caption said "Top 30" where it showed fewer.

### Not done, and limits

* **No webhook was sent to a real platform.**  The payloads follow the
  four formats' documented contracts and the two signatures follow the
  platforms' published samples, but only the transport was exercised for
  real — over loopback, against a server the test owns.
* **`--live` was not exercised against the network** (unchanged from
  v0.3).
* **No limit-down exit guard.**  A name locked at limit-down on a
  rebalance day is still "sold" at its last close.
* The limit-up guard does not know an ST name's 5 % band, nor the
  no-limit first sessions of a new listing; the presets exclude ST.
* `watch`'s `st` rule speaks from the company-list snapshot's date
  forward (2026-10-31 on this store), like every other use of that list.
* An ad-hoc screen archives under the name `adhoc`; two different ad-hoc
  screens would share one history file.
* `--older-than` depends on the filesystem's `atime`: refreshed at most
  once a day on a `relatime` mount, never on `noatime` (where it degrades
  to "not written for N days").
* A comparison charts at most eight presets; the table has them all.
* The browser test needs Chromium and is skipped without it; the markup
  checks (balanced tags, well-formed SVG, no external resource) run
  everywhere.
* Industry tags are today's classification, as in v0.3.

### Housekeeping performed

* The editable install was refreshed (`pip show qf-screen` → 0.4.0).
* With 0.4.0 installed, `qf-screen cache clear --stale` removed the four
  `_v030` merged-store caches, 612.6 MB: the cache went from 1.7 GB to
  1.1 GB, and no derived table had to be rebuilt.
* A stray `mover.yaml` that a debugging script of this session wrote into
  `~/.config/qf_screen/presets/` (outside the test fixture's isolated
  environment) was removed, with the directory it created.

### Added after this record (2026-11-26, same session)

* **The HTML pages follow a host's theme.** `:root` now honours
  `data-theme="light" | "dark"`, so a page shown in a frame can take the
  surface of the app around it instead of the OS's.  The host is the
  qf_tools website app (`qf_tools/website/qf_screen/`, which shows these
  pages in a same-origin frame).  Nothing else changes: with no attribute
  the page still follows the OS, and print is still light.  One markup
  assertion and the Chromium test pin it; the standalone files a
  `--format html` writes are byte-for-byte as before.
* **The presentation modules moved under `qf_screen/ui/`** (at the user's
  request, later on 2026-11-26): `html_report.py`, `watch.py`,
  `briefing.py` and `render.py` are now `qf_screen/ui/…`.  The plan's text
  above still names the old paths.  `cli.py` stayed at the package root:
  it is the entry point the installed `qf-screen` command imports by name.
  The one importer outside this repo, the qf_tools website app
  (`qf_screen.render`, for its cell formatting), followed the same day.
  The move also closed a defect of the lazy export table: `qf_screen.watch`
  is a function, and while its module was `qf_screen/watch.py` the two
  shared a name, so the moment the module was imported (the CLI does)
  `qf_screen.watch(...)` failed with "'module' object is not callable".  Two
  tests now pin it: every root export is the same object before and after
  its module loads, and no module at the package root is named like an
  export.
