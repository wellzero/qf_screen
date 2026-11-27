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

Around the two modes sits the daily workflow (v0.4): your own presets next
to the built-ins, a watchlist risk monitor, self-contained HTML reports, a
snapshot archive of what each screen held each day, webhooks and exit
codes for a post-close job, and a garbage collector for the caches.

It integrates with the `qf_project` ecosystem (`quant_free` constants/helpers,
`qf_factor` selection screens / ST band detector / lxg finance bridge) and
leaves the legacy `quant_free/screen` untouched.

**Data sources: `lxg` and `akshare` only.**  Every screen reads the local
`lxg/` and `ak/` trees plus the opt-in live `akshare` refresh — nothing
else.  (Precomputed factor CSVs of other provider trees are opt-in, see
[the bridge](#the-qf_factor-csv-bridge).)

## Install

```bash
pip install -e ../quant_free --no-deps && pip install -e ../qf_factor --no-deps
pip install -e . --no-deps --no-build-isolation     # into /opt/quantfree
pytest test -m unit          # hermetic; -m data adds real-store smokes
```

## Quickstart

```bash
qf-screen list                                   # the presets: built-in and your own
qf-screen features                               # every feature + DSL function
qf-screen run momentum_trend --top 10 --explain SH600018
qf-screen run value_dividend --as-of 2026-11-20 --out members.csv
qf-screen run --where "market_cap_bn > 2000" --rank "pct_rank(roe_best)" --top 20
                                                 # ad-hoc: no preset, no YAML
qf-screen run momentum_trend --industry 医药生物 --max-per-industry 3
                                                 # offline 申万 industry, sector cap
qf-screen diff value_dividend                    # entered / exited / rank movers
qf-screen inspect SH600519                       # one stock through every preset
qf-screen report --out report.md                 # the daily briefing
qf-screen report --format html --out report.html # ... as one self-contained page
qf-screen pit momentum_trend --start 2026-01-05 --end 2026-11-24 \
          --freq M --out schedule.csv            # qf_factor-ready
qf-screen validate momentum_trend --start 2026-01-05 --end 2026-09-30 \
          --cost-bps 15 --skip-limit-up          # net of costs, limit-up entries refused
qf-screen compare value_dividend momentum_trend low_vol_defensive \
          --start 2026-01-05 --end 2026-09-30    # presets side by side
qf-screen init-preset my_screen --from momentum_trend   # your own preset, documented
qf-screen watch --symbols SH600519,SZ000858 --preset value_dividend   # risk alerts
qf-screen run momentum_trend --save-history      # archive today's list ...
qf-screen history momentum_trend                 # ... how long each member has stayed
qf-screen data-status                            # coverage / freshness census
qf-screen cache status                           # what is cached, how fresh
qf-screen cache warm                              # after a data pull
qf-screen cache clear --dry-run --stale          # what no run will read again
qf-screen run value_dividend --no-cache          # source truth: no cache at all
```

A warm `run` takes **0.8–1.0 s** for every built-in preset.  The first run
after a data update rebuilds what changed (seconds to ~30 s for the
statement tables); `qf-screen cache warm` does that ahead of time.  A
validation window is a cold merge the first time (~15 s) and under a second
after — and it no longer slows the daily `run` down (each window keeps its
own derived tables).  `--no-cache` bypasses every disk cache — merged
tables, universe census and derived feature tables alike — and writes
nothing, so compare it against a cached run when you suspect a stale table.

Library use:

```python
from qf_screen import CNDataStore, ScreenEngine, ScreenSpec
from qf_screen.presets import list_presets
from qf_screen.ui.briefing import daily_report

store = CNDataStore()                    # last ~600 sessions, 8 workers
engine = ScreenEngine(store)
spec = ScreenSpec.from_yaml("value_dividend")
result = engine.run(spec)
print(result.summary());  print(result.funnel);  print(result.industry_mix())
result.explain(result.members[0])        # rule-by-rule why
result.notes()                           # na_pass legs, names an industry cap displaced
change = engine.diff(spec)               # vs the session before
card = engine.inspect("SH600519")        # every preset + a feature scorecard
markdown, data = daily_report(engine, [ScreenSpec.from_yaml(n) for n in list_presets()])
schedule, funnel = engine.schedule(spec, "2026-01-05", "2026-11-24", freq="M")
report = engine.validate(spec, "2026-01-05", "2026-09-30")   # + path stats, curve
table, reports = engine.compare([spec_a, spec_b], "2026-01-05", "2026-09-30")
report = engine.validate(spec, "2026-01-05", "2026-09-30", cost_bps=15, skip_limit_up=True)
report.stats["portfolio_net"]                     # the same book after 15 bps a side
save_snapshot(None, "value_dividend", result.asof, result.members, result.scores)
view = history_view(load_history(None, "value_dividend"), "value_dividend")
alerts = watch(engine, ["SH600519", "SZ000858"], preset="value_dividend")
```

## v0.4 — user presets, a watchlist monitor, HTML reports, an archive, webhooks

Plan and implementation record: `doc/plan/PLAN_v0.4.md`.  Still strictly
`lxg` + `akshare` data; a warm `run` is unchanged at 0.8–1.0 s.

**Fixed.**  `report --presets a,b` (and `inspect`, `cache warm`) failed on
a preset literally named `a,b`: preset lists now take spaces or commas.

**Validation prices the book like a book.**  The per-horizon forward
returns measure the *screen* (every schedule member, gross) and are
unchanged.  Two opt-in flags act on the portfolio *path*:

| flag | what it does |
|---|---|
| `--cost-bps BPS` | transaction cost per side of every trade — the initial purchase, each rebalance's sells and buys, and the final liquidation — so a round trip always pays both sides whatever the window. The report then carries the net figures beside the gross ones (`portfolio_net`, `excess_net`, `sharpe_net` …; the pool and the indexes stay gross) and what each rebalance paid. |
| `--skip-limit-up` | a member whose close sits at its board's daily limit on the entry day cannot be bought at that close and is not: it is left out of that holding period (its share of the book stays in cash) and counted. |

The guard reads the close against the previous *traded* close, by board
(10 % main, 20 % ChiNext / STAR, 30 % Beijing; a close within 0.2 % of
the limit counts, because the limit price is rounded to 0.01 CNY).  It is
a test on the close — the store keeps raw closes for every name but
opening prices only for the lxg layout-A subset, and the validation buys
at the close.  On the 2026-11 store, `momentum_trend` over January–September
2026: 2 of 240 entries were refused, and 15 bps a side took the
annualized return from −30.2 % to −32.2 % at 73 % turnover a month.
Also hardened: a member that stops trading mid-period is carried at its
last close inside `simulate()` itself (one missing price used to turn
`NaN` the whole cross-sectional mean, and with it every later value of
the path), and a period left with nothing to hold is a period in cash
rather than a hole in the path.

**User presets.**  Every `*.yaml` in `$QF_SCREEN_PRESETS_DIR` (default
`~/.config/qf_screen/presets/`) is a preset named by its file stem.  Every
command that takes a preset name finds it — `run`, `diff`, `inspect`,
`report`, `pit`, `validate`, `compare`, `cache warm` — and `qf-screen
list` marks where each one comes from.  A user file named like a built-in
(`value_dividend.yaml`) **replaces** it; any other name extends the list.
`qf-screen init-preset NAME [--from PRESET] [--out PATH] [--force]` writes
a documented starting file (or a copy of another preset) and never
overwrites an edited one.  The loader now rejects what would fail late or
silently no-op in a hand-edited file: unknown keys inside `universe` and
inside each `filter` / `rank` entry, `na_pass` that is not a boolean, a
rank entry without an expression or with a non-positive weight, a board no
screener knows, a `top_n` that is not a non-negative integer.

**Watchlist risk monitor.**  `qf-screen watch --symbols A,B` (or
`--watchlist FILE`) runs risk rules over the names you hold on the decision
date:

| rule | fires when | severity |
|---|---|---|
| `st` | the name is ST/*ST in the company-list snapshot | high |
| `pledge` | `pledge_ratio >= 0.30` | high |
| `trend_break` | `ma_ratio_60 < 1` or `ma_ratio_200 < 1` (the message says which) | medium |
| `liquidity` | `amount_ma20 < 5e7` | high |
| `suspended` | no close on the as-of session (suspended, halted or delisted) | high |
| `preset_exit` | with `--preset NAME`: the name is not in that preset's list — told which rules it fails (with what they read), or where it ranks among the passers when only the truncation kept it out | medium |

A missing value never alerts (a rule reports a *known* problem); the two
rules about absent data itself — `suspended`, and an unknown symbol —
always do.  `--format table|markdown|json`; `--fail-on-alert` exits 2.
A watchlist file carries the list and may re-tune the monitor:

```yaml
preset: value_dividend           # alert when a holding leaves this preset
symbols: [SH600519, SZ000858]    # a plain list, one symbol per line, works too
rules:                           # custom rules: DSL boolean, true = alert
  - {name: fallen,  expr: "high252_ratio < 0.60", severity: high}
  - {name: too_hot, expr: "ret_21d > 0.30", description: ran too far}
disable: [pledge]                # built-in rules to switch off
thresholds: {pledge: 0.20}       # ... or re-tune (pledge, liquidity)
```

**HTML reports.**  `report`, `validate` and `compare` take `--format
html`: one self-contained page — inline SVG charts (cumulative value
against the pool and the indexes, drawdown, industry concentration),
tables that sort by any column and filter by a symbol / name / industry
box, a crosshair readout on every line chart, `+NEW` / `−OUT` badges and
rank-move pills in the briefing, and a table twin for every chart.  No
CDN, font or image is fetched: the page can be mailed or opened offline.
Presets keep a fixed colour whatever they rank; the pool and the indexes
are context, drawn in grey; a net-of-cost path is its gross twin's colour,
dashed.  Dark mode is its own selected palette, and print / forced-colours
fall back to line patterns.  The page follows the OS's light / dark
setting; a host that embeds it in a frame picks the surface instead with
`data-theme="light"` or `data-theme="dark"` on the root (`<html>`).

**Snapshot archive.**  `run` / `report --save-history [DIR]` appends the
session's ranked members to one CSV per preset under
`$QF_SCREEN_HISTORY_DIR` (default `~/.local/share/qf_screen/history/`),
idempotent per (date, preset) — a second run of the same session replaces
that session's rows.  `qf-screen history PRESET [--days N]` reads it back:
for every current member, `streak` (sessions in a row up to today),
`present` (sessions in the window), `stayed`, `first_seen`; the names that
left inside the window are listed with the run they had.  A "session" is
an archived one: a day skipped is not a broken streak, and the command
says so when member counts swing across the window (a session archived
with a smaller `--top` makes the names beyond it look new).

**Automation.**

| exit code | meaning |
|---|---|
| 0 | all is well |
| 1 | a screen could not be built |
| 2 | `diff --fail-on-change` saw a change, or `watch --fail-on-alert` saw an alert |
| 3 | a webhook that was asked for could not be delivered (it outranks 2: an alert nobody received is not a quiet day) |

`diff`, `watch` and `report` take `--webhook-format
json|feishu|dingtalk|slack`, `--webhook-url` (or `$QF_SCREEN_WEBHOOK_URL`)
and `--webhook-secret` (or `$QF_SCREEN_WEBHOOK_SECRET`; 飞书 签名校验, 钉钉
加签).  They are strictly opt-in: nothing is sent unless `--webhook-format`
is on the command line, so a URL set in a login profile turns no run into
a network call.  `json` posts the command's own payload; the others post
a concise text reading (entered / exited / movers; each alert with its
severity; one line per preset).  A 200 that is a refusal (飞书 `code`,
钉钉 `errcode`, Slack `"ok": false`) counts as a failure.

**Cache pruning.**  `cache clear` deletes by kind as before, or prunes:

```bash
qf-screen cache clear --dry-run --stale            # what no run will read again
qf-screen cache clear --older-than 30              # ... not used for 30 days
qf-screen cache clear --max-gb 2                   # ... least recently used until 2 GB
```

`--stale` removes another version's layout, interrupted writes, a
merged-store cache built from a source census that has since moved or
that is incomplete, and the derived tables of every window all of whose
store caches are gone bad — the fresh daily window is never touched.
`--older-than` goes by last use (the newer of an entry's write and read
time; a `relatime` mount refreshes read times at most once a day, a
`noatime` mount never).  `--max-gb` evicts the least recently used until
the cache fits.  The three combine, `--what KIND` narrows them, and
`--dry-run` lists what would go with the reason.  `cache status` now
judges validation-window caches fresh or stale too.

**Found and fixed on the way.**  `simulate()` drew a period left with no
holdable name as a hole in the path (it is a period in cash now); a custom
watch rule naming an unknown feature surfaced as a `KeyError` from the
panel, and a bad watchlist file as a traceback; YAML 1.1 reads `1e8` as a
string, so a natural threshold was refused; the lazy export
`qf_screen.ExprRule` named a class that never existed.

## v0.3 — daily workflows, validation analytics, a sub-second warm run

Plan and implementation record: `doc/plan/PLAN_v0.3.md`.  Strictly `lxg` +
`akshare` data.

**Four CLI bugs fixed.**  `--top N` was read under a name the parser never
set and silently ignored (a `top_n: 30` preset stayed at 30, and `--top 5`
printed the untruncated count) — it now replaces the preset's `top_n`
(`0` = no limit) and the summary agrees with the table.  `run` works with
no preset (`--where` / `--rank` on the required hygiene block).  `--format
markdown --out FILE` writes the document (it returned before looking at
`--out`) and `md` is an alias.  `--industry` works **offline** against the
local 申万 classification of `lxg/industries.csv` (all three levels) — under
`--live` the 东财 industry boards join in for names with no local
classification.  `pit` and `validate` accept `--where`, `--rank`,
`--boards`, `--top` and `--all-passers`.

**Daily workflows.**

| command | what it answers |
|---|---|
| `diff PRESET [--as-of D] [--prev D]` | entered (+) / exited (−) / rank movers vs the session before, with retention |
| `inspect SYMBOL [--presets …]` | which presets a stock passes or fails, the rule and the values that rejected it, its rank when it is a member — and a scorecard of 37 features with each one's cross-sectional percentile |
| `report [--presets …] [--out report.md]` | the daily Markdown briefing: overview, membership change, top picks, sector mix, every data warning |
| `run … --max-per-industry N` | at most N names per industry; the next-best names fill the freed slots, so the list stays `top_n` long |

Every `run` prints the **industry mix** of its members.  The industry tag
is the local 申万 first level; names without one (no `lxg/industries.csv`)
show as `未分类` — under `--live` the 东财 industry board classifies them
instead (a local tag is never overwritten).  The tag is today's
classification: a historical schedule is capped with it too, the one
non-point-in-time input of the engine.

**The funnel says what a rule could not judge.**  Only ~765 names carry lxg
statements; a rule on `debt_assets` cannot judge the other ~4,700.  Each
funnel row now carries an `unknown` count (names where every feature the
rule mentions is missing), a `†` marks rules with `na_pass`, and the run
prints a note per such rule ("balance: 4708 names passed WITHOUT data").
A rule you cannot judge is a choice you can now see.

**Presets retuned on real data.**  `quality_growth` headed its list with
turnarounds off a thin base (1,992 % profit growth off a 6.8 % prior-year
margin).  It now requires the base to have been profitable
(`net_margin_py >= 0.08`: the net margin of the TTM report known a year
earlier) and cash-backed (`ocf_ttm > 0`), and caps growth at ±100 % for
ranking — 23 passers as of 2026-11-24, the 1,992 % name no longer among
them.  `value_dividend` was
shrunk to the statement subset by its balance leg; that leg is `na_pass`
now (13 → 45 passers) with banks / brokers / insurers kept out explicitly
by the new `financial` flag — a structurally levered balance sheet must not
be waved through unjudged.

**Validation analytics.**  `validate` keeps the per-horizon forward returns
and adds the path of the equal-weight portfolio that follows the schedule
(rebalanced on each schedule date, held in between): annualized return,
volatility, Sharpe (risk-free 0), max drawdown, cumulative return, the
turnover per rebalance, the excess over the candidate pool (annualized,
information ratio, tracking error) and benchmarks — CSI 300 / CSI 500 read
from the store's `lxg` index files when they are there.  `--curve FILE`
writes the daily cumulative curve.  `compare PRESET …` validates several
presets over one window side by side: each preset's excess is against its
own candidate pool, and one reference column — the equal-weight whole
market (every name with a price the session before) — plus the indexes sit
next to presets that screen different boards.

**A sub-second warm run.**  ≈3.6–4.0 s → 0.8–1.0 s, from these changes: the
universe census (1 s: 2,042 `industries.csv` parses + a scan of ~9,800
directories) is cached behind a fingerprint of its own inputs; wide tables
are stored as one flat parquet column instead of ~5,500 (the 16 store
tables read in ~0.1 s instead of ~1 s); the trailing-window price features
are cached behind the store's own fingerprint; and the `yfinance` import
that rode in with a `quant_free` symbol helper is deferred to first use.
`qf-screen cache {status,clear,warm}` inspects the caches (per kind and
per window, with the daily store marked fresh or stale), removes them by
kind, or pre-builds them.

## v0.2 — bug fixes & productization

Five high-impact bugs found by running the v0.1 presets against the real
store, all fixed and pinned by regression tests:

1. **numexpr silent corruption** — pandas routes wide (`>10k` cell)
   `DataFrame / DataFrame` ops through numexpr, and this environment's
   numexpr corrupts float32 arrays containing NaN (1.4M of 3.4M cells
   wrong on a 630×5472 division: `ma_ratio_200` collapsed to `0.0`, 26 of
   5,472 names passed `trend`).  `import qf_screen` now disables pandas'
   numexpr path, and every wide feature builder divides through plain
   numpy (`_safe_div`) so a host that re-enables numexpr stays correct.
2. **`days[:, None]` 2-D broadcast** — the dividend/alt window builders
   fed `(N, 1)` needles into `np.searchsorted`, silently dividing every
   row by the FIRST row's price.  1-D now, with multi-symbol
   distinct-value tests.
3. **stale alt data vetoes everything** — lxg alt files lag the price
   files by days to weeks, so `nb_hold_chg_*`/`margin_chg_20d` were NaN on
   the decision date (0 of 5,472 names).  Change features now carry the
   last computed value forward under a BOUNDED ffill (`QF_SCREEN_ALT_FFILL`,
   default 20 sessions — strictly causal, and a dead source reverts to
   honest NaN), and every screen warns loudly when a source ends well
   before the calendar.
4. **`schedule`/`validate` ignored `rank` and `top_n`** — the PIT export
   wrote every passer while `run` wrote the ranked top-N.  `schedule`
   now applies the same ranking + truncation (`apply_top_n=False` exports
   the raw pool).
5. **`explain` on a rejected symbol was empty** — feature values and rule
   expressions now populate for ANY candidate.

Also new: a **fingerprinted parquet cache** under `cn/qf_screen_cache/`
(`--no-cache` bypasses all of it); `roe_implied` (`pb/pe_ttm`) and
`roe_best` extend fundamental screens from the ~764 statement names to the
full market; readable-unit features (`market_cap_bn`, `amount_ma20_mn`, …);
a **qf_factor CSV bridge** (below); and a product-grade CLI (`features`,
`--where`, `--rank`, `--boards`, `--industry`, `--concept`, `--format
markdown`, human units, funnel survival).  The tables print 亿元 / %
according to each feature's unit: `*_bn` and `*_mn` features are already
亿元 / 百万元, `turnoverrate` and `turnover_ma*` are **percent points**
(`1.4` == 1.4 %), CNY statement amounts print in 亿元, every other
percentage-like feature is a fraction.

## The qf_factor CSV bridge

The qf_factor calc scripts store one CSV per symbol under
`<equity>/<SYM>/<provider>/<Family>.csv`, with `symbol` + `date` leading
columns and one column per factor.  The bridge turns any
`(<family>, <column>)` pair into a screenable feature on first mention,
stepped onto the calendar with forward-fill and disk-cached:

```yaml
filters:
  - {name: alpha, expr: 'factor("Alpha101", "alpha1") > 0'}
rank:
  - {expr: "pct_rank(factor_Alpha101__alpha1)", weight: 1.0}
```

The bridge reads the **`lxg` and `ak` provider trees** (the screener is an
lxg + akshare product).  A family that lives elsewhere —
`xq/finance_Q_factor.csv`, `xtq/Autodsl.csv` … — is opt-in:
`QF_SCREEN_FACTOR_PROVIDERS=lxg,xq` (searched in the order you list).
Without the opt-in such a feature is all-NaN and the bridge says where the
family sits.  The legacy bare `<DIR>/<Family>.csv`
only fills the gaps.  A symbol without the file is NaN and fails filters
conservatively.

## Presets

54 built-ins across 9 strategy families (added 2026-11-27, calibrated on the
real store — see `doc/plan/PLAN_presets.md`).  **Statement features**
(`roe`, `ocf_ttm`, margins, growth …) cover only the ~765 names with lxg
statement pages: presets marked *stmt* screen that subset by design; *na_pass*
marks legs that decline to judge names without the data instead of failing
them.

**Value & income**

| name | thesis | legs |
|---|---|---|
| `value_dividend` | cheap, profitable, low-levered payers | P/E<15, ROE>12% (`roe_best`, full market), debt<60% where statements exist (na_pass; financials excluded), yield≥3% |
| `deep_value_pb` | non-financial bargains | P/B<1.2, P/E<15, `roe_best`>6%, debt<70% (na_pass) |
| `fcf_cash_cow` | *stmt* high free-cash-flow yield | FCF yield≥4%, OCF≥80% of profit, debt<60% |
| `magic_formula` | *stmt* Greenblatt | rank ROA + earnings yield equally; OCF>0, ROA>2% |
| `high_dividend_yield` | main-board income | yield≥4.5%, P/E<20, low-vol tilt, debt<70% (na_pass) |
| `dividend_growth_quality` | payers compounding | yield≥2.5%, `roe_best`>12%; growth/debt legs na_pass |
| `low_ps_sales_value` | cheap sales, profitable | P/S<1.5, P/E<25, `roe_best`>5%, ≥90% of 200d MA |
| `low_pcf_cash_value` | cheap operating cash | P/CF<15, `roe_best`>10%, non-financial, ≥70% of 52w high |

**Quality & financial strength**

| name | thesis | legs |
|---|---|---|
| `quality_growth` | *stmt* profitable growers at a reasonable price | rev/profit growth>15% off a base with ≥8% prior-year margin, OCF>0, ROE>12%, GM>20%, debt<60%, P/E<40; growth capped at ±100% for ranking |
| `high_roe_compounders` | *stmt* durable moats | ROE>15%, GM>30%, NM>10%, OCF>0, debt<60%, P/E<45 |
| `net_cash_fortress` | *stmt* cash-rich balance sheets | cash/assets≥25%, debt<40%, OCF>0 |
| `high_margin_moat` | *stmt* pricing power | GM≥40%, OM≥15%, ROE>12%, OCF>0 |
| `cash_earnings_quality` | *stmt* low-accrual earners | OCF≥profit, FCF>0, ROE>10% |
| `quality_at_reasonable_price` | full-market QARP | `roe_best`>14%, P/E<22, P/B<4, ≥95% of 200d MA |
| `financial_blue_chips` | banks/brokers/insurers | `financial`>0.5, P/E<10, P/B<1.5, yield≥3% |

**Growth & GARP**

| name | thesis | legs |
|---|---|---|
| `garp_peg` | Lynch GARP | 0<PEG<1.2, P/E<35, `roe_best`>10% |
| `hyper_growth_revenue` | *stmt* real-base top-line growth | rev>25%, profit>15%, prior-year margin≥5%, OCF>0 |
| `earnings_acceleration` | *stmt* operating leverage | profit>20%, rev>10%, profit>rev, OCF>0 |
| `growth_momentum` | CANSLIM growth + price | growth>15% (na_pass), >60d MA, ≥75% of 52w high |
| `value_turnaround_growth` | cheap growth turning | P/E<20, 0<PEG<1.0, 3-1 momentum>0 |

**Technical, momentum & breakout**

| name | thesis | legs |
|---|---|---|
| `momentum_trend` | the classic price/volume stack | >200d MA, 6-1 momentum, near 52w high, low vol, not lottery |
| `breakout_52w_high` | George–Hwang | ≥90% of 52w high, above 20/60/200d MAs, not lottery |
| `long_term_momentum_12m` | 12-1 momentum | mom_252s21>15%, >200d MA, low vol, not lottery |
| `short_term_momentum_3m` | fast 3-1 momentum | mom_63s21>5%, >20/60d MAs, turnover≤15% |
| `steady_uptrend_low_vol` | risk-adjusted uptrend | above 20/60/200d MAs; rank mom/vol |
| `volume_expansion_trend` | volume-confirmed trend | amount_ma20>1.25×amount_ma60, >20/60d MAs |
| `low_turnover_neglected` | neglected-name effect | bottom-35% turnover, P/E<25, `roe_best`>8%; Amihud rank tilt |
| `volatility_compression` | quiet before the move | vol_20d<0.75×vol_120d, >60/200d MAs, ≥60% of 52w high |

**Mean reversion & pullback**

| name | thesis | legs |
|---|---|---|
| `uptrend_pullback` | dip in an uptrend | >103% of 200d MA, −15%<ret_21d<0, `roe_best`>8% |
| `short_term_reversal_quality` | oversold quality bounce | −25%<ret_21d<−5%, `roe_best`>12%, P/E<25 |
| `deep_oversold_value` | contrarian value | ret_126d<−10%, P/E<15, P/B<1.8, yield≥2% |
| `quiet_pullback_low_vol` | calm dip | bottom-35% vol, >200d MA, <20d MA |

**Size & board-specific**

| name | thesis | legs |
|---|---|---|
| `small_cap_value` | 20–100亿 value | P/E<20, P/B<2.5, `roe_best`>8% |
| `small_cap_quality_momentum` | 20–120亿 quality+trend | `roe_best`>10%, mom_63s21>0, >60d MA |
| `mid_cap_core_growth` | 100–500亿 core | `roe_best`>12%, P/E<30, >200d MA |
| `large_cap_blue_chip` | ≥500亿 income core | `roe_best`>12%, yield≥2%, low vol, ≥3亿/day |
| `chinext_growth_leaders` | ChiNext leaders | `roe_best`>10%, P/E<60, growth (na_pass), >60d MA |
| `star_tech_leaders` | STAR hard-tech | `roe_best`>8%, P/E<80, GM>30% (na_pass), >60d MA |
| `bj_emerging_leaders` | Beijing board | `roe_best`>8%, P/E<40 — **data-limited**: the default lxg+ak merge prices ~1 BJ name, so membership is 0–1 until BJ daily data lands |

**A-share alt / smart money & governance**

| name | thesis | legs |
|---|---|---|
| `smart_money_ashare` | smart-money confirmation | margin↑, analyst buys, no pledge overhang, insiders not selling, >60d MA; northbound confirms while the data lives (na_pass since the 2026-09 disclosure stop) — **data-limited** in deep drawdowns |
| `chip_concentration` | chips consolidating | holder count −5%, >60d MA, pledge<20% (na_pass) |
| `analyst_consensus_buy` | sell-side conviction | ≥2 reports/90d, ≥80% 买入/增持, `roe_best`>8% |
| `margin_momentum_ashare` | leveraged money leaning in | margin balance +5%/20d, >20/60d MAs, pledge<20% (na_pass) |
| `clean_governance_insider` | clean names | pledge<10%, insiders not net selling (na_pass), `roe_best`>10%, P/E<30 |
| `institutional_block_value` | block-deal footprint | block discount within ±10%, `roe_best`>10%, P/E<30 |

**Industry-neutral & multi-factor**

| name | thesis | legs |
|---|---|---|
| `industry_neutral_value` | cheap for its sector | industry_rank(earnings yield)≥70%, industry_rank(ROE)≥60%, cap 3 |
| `industry_neutral_momentum` | winners for their sector | industry_rank(6-1 mom)≥75%, >200d MA, cap 3 |
| `all_weather_multifactor` | 4-pillar blend | value+quality+momentum+low-vol ranked equally, cap 4 |
| `value_momentum_combo` | Asness barbell | P/E<20, P/B<3, 6-1 momentum>0, >200d MA, cap 5 |

**Undervalued + tailwind** (value with a catalyst — the "margin of safety
that something is starting to happen to" family)

| name | thesis | legs |
|---|---|---|
| `undervalued_tailwind` | the core: cheap, floored, and re-rating on volume | P/E<25, P/B<2.5, `roe_best`>8%, 3-1 mom>0, >60d MA, amount_ma20≥amount_ma60, growth>−10% (na_pass), cap 4 |
| `davis_double_play` | *stmt* earnings growth + multiple expansion together | P/E<35, profit>10%, rev>0, `roe_best`>8%, OCF>0, >60d MA; clip-ranked growth, cap 3 |
| `left_side_accumulation` | step 1: the patient base-position list, no confirmation asked | P/E<15, P/B<1.5, `roe_best`>6%, yield≥2.5%, ret_63d>−15%, mom>−10%, growth>−30% (na_pass), cap 5 |
| `capital_inflection_value` | the "ignored" status breaking, read from flows | P/E<20, P/B<3, `roe_best`>6%, margin balance +3%/20d OR holders −3%, ≥97% of 60d MA, not lottery, cap 4 |

Every preset starts from a shared hygiene block (traded, price ≥ 2 CNY,
20d mean traded value ≥ 5e7) — `load_preset` prepends any that are missing.
A preset may set `max_per_industry: N` for the sector cap, and any filter
may carry `na_pass: true` (names with every mentioned feature missing pass
instead of fail — "no KNOWN problem"; the funnel shows how many).

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
  - {expr: "pct_rank(clip(profit_growth, -1, 1))", weight: 1.0}   # capped
```

Functions: `rank`, `pct_rank`, `zscore`, `industry_rank` (cross-sectional),
`abs`, `clip`, `fillna`, `log`, `sqrt`, `sign`, `minimum`, `maximum`, and
`factor("<family>", "<column>")` for the qf_factor CSV bridge (string
literals allowed ONLY there).  Comparisons with NaN are **False** (missing
data fails a filter; opt out per-filter with `na_pass: true`).  `not`
inverts that literally — `not (pe > 15)` *passes* unknown P/E; write
`pe > 0 and pe < 15` when unknown must stay out.  `==`/`!=` are rejected
(meaningless on NaN cross-sections); unknown features, unknown keys and
non-boolean filters are compile-time errors.

## Features (the registry)

`qf-screen features [PATTERN]` lists them all with descriptions.  Highlights:

* **price/volume** — `close` (rebuilt adjusted), `close_raw`, `ret_21d…252d`,
  `mom_63/126/252s21`, `vol_20/60/120d`, `ma_ratio_20/60/200`,
  `high252_ratio`, `max21`, `amount_ma20/60`, `turnover_ma20/60`;
* **valuation** — `pe_ttm`, `pe_static`, `pb`, `ps_ttm`, `pcf_ttm`, `peg`,
  `market_cap`, `float_market_cap`, `earnings_yield`, `market_cap_bn` /
  `float_market_cap_bn` (亿元), `amount_ma20_mn` (百万元);
* **fundamentals** (lxg statements, PIT by statutory announce date) —
  `roe`, `roa`, `gross_margin`, `net_margin`, `op_margin`, `rev_growth`,
  `profit_growth`, `net_margin_py` (the prior-year base the growth is
  measured against), `revenue_ttm`, `profit_ttm`, `ocf_ttm`, `capex_ttm`,
  `fcf_ttm`, `assets`, `liabilities`, `equity`, `interest_debt`, `cash`,
  `debt_assets`, `cash_assets`, `fcf_yield`, `div_yield_ttm` (event-based
  with the layout-B `dyr` fallback), `roe_implied` (`pb/pe_ttm`, full
  market), `roe_best` (statement ROE with the implied fallback);
* **A-share alt** — `nb_hold_chg_20/60d` (northbound), `margin_chg_20d`
  (both bounded-ffilled on lagging files), `holder_num_chg`,
  `pledge_ratio`, `report_cnt_90d`, `buy_report_ratio_90d`,
  `insider_net_180d_mc`, `block_discount_180d`;
* **static** — `board`, `name`, `industry_l1/l2/l3`, `financial` (1 for a
  bank / securities firm / insurer, 0 otherwise), `listed_days`, `st_flag`;
* **dynamic** — `factor_<family>__<column>` resolves a qf_factor CSV column
  on first use (see the bridge section above).

Feature tables are built lazily; the alt / dividend / statement / factor
features read only the symbols that have the file and cache their derived
wide tables, fingerprinted on the source-file census (count + mtime sum —
any rewrite moves it, even one that leaves a file older than the newest),
the calendar bounds and the symbol list.  Register your own with
`qf_screen.rules.register_feature` (`cache="store"` for a builder that is a
pure function of the store's base tables).

## Causality & data honesty

* **One evaluation, every mode.** Feature tables are as-traded (row `t` =
  day `t`'s close).  `run` decides on the as-of close; `schedule` evaluates
  row `t-1` and writes membership at `t` — the strictly-before `lag=1`
  convention of `qf_factor.framework.selection.screens`, so exported
  schedules have zero look-ahead (a unit test perturbs day-`t` data and
  asserts the `t` row does not move).  `diff` compares two such snapshots;
  `validate` acts on each schedule date's close.
* **Neither provider's adjusted close is usable.** ak's `close` is
  unadjusted; lxg's 前复权 `close` equals the raw price on 1,740 of 2,446
  lxg symbols and leaves 1,312 of 1,802 bonus-share events unadjusted
  (measured 2026-11).  The store rebuilds the series from raw prices:
  documented lxg dividend events (cash + bonus per share) on their
  ex-dates, share-count splits gated on `total_shares` AND `float_shares`
  scaling together (the float test is what separates a bonus issue from a
  placement), raw returns otherwise.
* **Traded value for ak-only names** is reconstructed as
  `turnoverrate/100 × float_market_capital` (within 0.2% of lxg's amount).
* **ST exclusion** uses the company-list snapshot **forward-only** (never
  backdated — a TODAY list says nothing about 2021).  `universe.st_band:
  true` opts into qf_factor's price-band detector for historical main-board
  ST state (~20 s, built once).
* **Fundamentals are announcement-stepped**: row `t` carries the latest
  report whose statutory deadline passed.  Coverage is the lxg finance
  subset (~765 of ~5,500 names) — the funnel's `unknown` column shows what
  each rule could not judge.
* **Incomplete sessions**: a trailing session whose close count collapsed
  (a data update in flight, not a market move) is excluded from the
  default decision date and reported as a warning.

## Validation: what the numbers mean

`validate` buys the schedule's members at each rebalance date's close.
With `--cost-bps` the path is net of costs and `report.stats["portfolio_net"]`,
`report.curve["portfolio_net"]`, `report.net` (excess, information ratio,
`cost_drag_ann`) and `report.per_date["cost"]` sit beside the gross ones;
with `--skip-limit-up` the refused entries are counted in `report.skipped`
and `report.per_date["skipped"]`.  The per-horizon block is unchanged from v0.1 (mean forward return of the
members vs every name passing the universe constraints the session before
— so the spread isolates the FILTERS).  The portfolio block follows the
equal-weight portfolio that acts on the schedule: rebalanced on each
schedule date, held (not re-weighted) until the next, a suspended name
valued at its last close, a name with no price on its entry date dropped
from that period.  `portfolio`, `universe` (the same weighting of the
candidate pool) and each benchmark are reduced over the SAME sessions:
annualized return, annualized volatility, Sharpe (risk-free 0), maximum
drawdown, cumulative return.  Benchmarks come from
`cn/index/<DIR>/lxg/daily.csv` (`--benchmarks 000300,000905` by default,
`''` for none); when an index file ends before the window its curve stops
there and the report says so.  `report.per_date` carries, per rebalance,
the forward returns, the period return, the turnover (share of names that
are new) and each series' cumulative value at that date; `report.curve`
is the daily cumulative path.

## Caches

Everything this package writes lives under
`<data>/cn/qf_screen_cache/` (and `ak_live/` for `--live` responses):

| kind | what | keyed on |
|---|---|---|
| `store` / `history` | merged wide tables of the daily window / of a `pit`·`validate` window | source-file census of the price files + window + version |
| `universe` | the universe census (dirs, names, industries) | equity-dir count, industries census, `company_list.csv` |
| `feature` | trailing-window price/volume tables | the store's fingerprint + window |
| `statement` | lxg statement payload (window-independent) and step tables | statement-file census (+ window for the steps) |
| `alt`, `dividend`, `factor` | the other derived tables | their own source-file census + window |

Derived tables live under `tables/<window>/`, so a validation window and
the daily snapshot never evict each other.  Every cache only ever costs
speed: a miss is a rebuild, a corrupt or foreign-layout file is a miss,
and writes are atomic.

```bash
qf-screen cache status [--json]        # per kind and per window; every store fresh or STALE
qf-screen cache clear [--what KIND ...] [--dry-run]   # default: all
qf-screen cache clear [--stale] [--older-than DAYS] [--max-gb N] [--what KIND]
                                       [--dry-run]    # prune (above)
qf-screen cache warm [--presets ...] [--start D --end D] [--symbols A,B]
```

`cache status` reports files of an older layout as `obsolete` (never read;
`clear --what obsolete` after an upgrade).

## Live mode (opt-in)

`--live` appends the akshare `stock_zh_a_spot_em` snapshot as a
provisional last row (live close/valuation/turnover; the adjusted chain
extends by `live/prev-raw`), and reads the 东财 board constituents
(day TTL, disk-cached; never touched offline or by the unit tests).
`--concept` restricts a screen to a 东财 concept board; `--industry`
additionally matches the 东财 industry boards and gives an industry tag to
names that have no local classification (which is what lets the sector mix
and `--max-per-industry` see the ~3,400 names without `lxg/industries.csv`).
An exact board name wins over a longer name containing it, otherwise every
board whose name contains the text.

## Layout

```
qf_screen/
├── data/            universe (+ census cache), lxg/ak providers, merged store (+ parquet cache),
│                    factor CSV bridge, table cache (flat codec, inventory), safe parallel map
├── rules/           feature panel (+ store-keyed feature cache), DSL, hygiene/technical/
│                    fundamental/alt features
├── presets/         54 built-in YAML screens (9 strategy families; family
│                    folders one directory deep, e.g. undervalue_tailwind/,
│                    count too) + the registry (built-in and user presets,
│                    `$QF_SCREEN_PRESETS_DIR`) + the init-preset scaffold
├── ui/              what qf_screen shows (reached through the command line):
│   ├── render.py        the units: one feature value in its human unit; text / Markdown tables
│   ├── briefing.py      the text / JSON / Markdown views (run, diff, inspect, report, compare)
│   ├── html_report.py   the self-contained HTML pages: inline SVG charts, tables, script
│   └── watch.py         the watchlist risk monitor: rules, watchlist files, views
├── engine.py        ScreenEngine: run / schedule / validate / diff / inspect / compare
├── analytics.py     schedule → equal-weight portfolio path, gross or net of costs;
│                    return / Sharpe / drawdown; the limit-up test
├── history.py       the snapshot archive and the persistence view
├── notify.py        webhook delivery (json / feishu / dingtalk / slack)
├── cache_ops.py     cache status / warm / prune
└── cli.py           qf-screen: list | init-preset | features | run | diff | inspect |
                              report | watch | history | pit | validate | compare | cache |
                              data-status
```

Environment variables: `QUANT_DATA_DIR` (the store), `QF_SCREEN_PRESETS_DIR`
(user presets), `QF_SCREEN_HISTORY_DIR` (the snapshot archive),
`QF_SCREEN_WEBHOOK_URL` / `QF_SCREEN_WEBHOOK_SECRET` (only used when a
command opts in with `--webhook-format`), `QF_SCREEN_FACTOR_PROVIDERS`
(the CSV bridge's provider trees).

Design notes and measured findings live in the module docstrings; the
plans and their records are under `doc/plan/` — `PLAN.md` (v0.1, v0.2 and
one-page summaries) and `PLAN_v0.3.md`, `PLAN_v0.4.md`.
