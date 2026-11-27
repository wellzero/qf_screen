"""Smoke tests against the REAL store under ``QUANT_DATA_DIR``.

Skipped automatically when the tree is absent, so a checkout without the
data volume still produces a green hermetic run (the qf_factor tier
contract).

v0.2: every built-in preset must produce a SENSIBLE, non-empty snapshot on
the real store, its wide math must agree with plain numpy (the numexpr
regression caught in the wild), and the factor bridge must resolve a real
qf_factor CSV column.

v0.3 (the tail of the file): the CLI overrides that used to be ignored, the
offline ``--industry`` restriction, ``diff`` / ``inspect`` / ``report``,
the validation's benchmark path, ``compare``, the cache status, the two
retuned presets, and a timing bound loose enough for any quiet machine but
tight enough to catch a cache that silently stopped hitting (a broken file
lookup once made every table rebuild: 12-30 s per preset).
"""
from __future__ import annotations

import json
import os
import re
import time

import numpy as np
import pandas as pd
import pytest

from qf_screen import cache_ops
from qf_screen.ui.briefing import daily_report
from qf_screen.cli import main
from qf_screen.data import CNDataStore, build_universe
from qf_screen.engine import ScreenEngine, ScreenSpec
from qf_screen.presets import list_presets

DATA_DIR = os.environ.get("QUANT_DATA_DIR", "/home/data/quant_free_data")
HAVE_DATA = os.path.isdir(os.path.join(DATA_DIR, "cn", "equity"))

pytestmark = [pytest.mark.data,
              pytest.mark.skipif(not HAVE_DATA, reason="no market data tree")]


@pytest.fixture(scope="module")
def engine():
    store = CNDataStore(DATA_DIR, max_workers=4)
    return ScreenEngine(store)


def test_universe_builds():
    universe = build_universe(DATA_DIR)
    assert 4000 < len(universe) < 10000
    assert len(universe.st_named) > 50
    assert "SH600519" in universe


def test_tail_store_loads_and_adjusts():
    store = CNDataStore(DATA_DIR, max_workers=4)
    assert len(store.provenance) > 4000
    # the rebuilt adjusted close must exist and stay finite where traded
    close = store.wide("close")
    traded = store.wide("close_raw").notna()
    assert (close.notna() | ~traded).all().all()
    status = store.data_status()
    assert status["price"]["split_adjusted_events"] > 100
    assert "alt_freshness" in status
    assert status["alt_freshness"]["mutual_market"]["files"] > 1000


def test_wide_math_matches_numpy_on_real_width(engine):
    """ma_ratio_200 must equal close / MA computed in plain numpy — the
    in-the-wild numexpr failure left 5,100 of 5,472 names at 0.0."""
    panel = engine.panel
    close = panel.store.wide("close").astype(float)
    ma = close.rolling(200, min_periods=100).mean()
    expected = np.divide(close.to_numpy(dtype=float), ma.to_numpy(dtype=float))
    pos = len(panel.calendar) - 2
    got = panel.cross_section("ma_ratio_200", pos)
    ok = np.isfinite(expected[pos])
    assert ok.sum() > 4000                          # real coverage
    np.testing.assert_allclose(got[ok], expected[pos][ok], rtol=1e-6)
    # the plan's reference point: SH600519 was 0.0 (corrupted), is ~1.096
    moutai = panel.table("ma_ratio_200")["SH600519"].iloc[pos]
    assert 0.5 < moutai < 2.0


@pytest.mark.parametrize("preset", sorted(set(list_presets()) - {"smart_money_ashare"}))
def test_every_preset_is_nonempty_and_sane(engine, preset):
    """Each preset: non-empty membership, a monotone funnel, and ranked
    members.  (smart_money_ashare is checked separately: its northbound
    leg is na_pass against a dead 2026-09 disclosure source, and in a deep
    drawdown its honest membership can be tiny.)"""
    spec = ScreenSpec.from_yaml(preset)
    result = engine.run(spec)
    assert len(result.members) >= 5, f"{preset}: {len(result.members)} members"
    funnel = result.funnel
    assert list(funnel["cumulative"]) == sorted(funnel["cumulative"], reverse=True)
    if spec.rank:
        assert result.scores.is_monotonic_decreasing


def test_smart_money_degrades_gracefully_on_dead_northbound(engine):
    """Northbound disclosure ended 2026-09: the preset must still produce
    members (na_pass), the funnel must show the leg stepping aside, and
    the run must WARN about the stale source."""
    spec = ScreenSpec.from_yaml("smart_money_ashare")
    result = engine.run(spec)
    funnel = dict(zip(result.funnel["rule"], result.funnel["passing"]))
    assert funnel["northbound"] > 1000              # the leg does not veto
    assert len(result.members) >= 1
    assert any("mutual_market" in w for w in result.warnings)


def test_div_yield_has_real_coverage(engine):
    panel = engine.panel
    pos = len(panel.calendar) - 2
    values = panel.cross_section("div_yield_ttm", pos)
    covered = np.isfinite(values)
    assert 1500 < covered.sum() < len(values)       # event + dyr fallback
    yields = values[covered]
    assert 0 <= np.nanmedian(yields) < 0.10         # a sane yield range
    assert (yields < 0.30).all()                    # no 2D-broadcast blowups


def test_roe_best_covers_the_full_market(engine):
    pos = len(engine.panel.calendar) - 2
    values = engine.panel.cross_section("roe_best", pos)
    covered = np.isfinite(values)
    assert covered.sum() > 3000                     # far beyond the 764 statements
    sensible = values[covered]
    assert -1.0 < np.nanmedian(sensible) < 0.50


def test_factor_bridge_resolves_a_factor_csv_of_the_lxg_tree(engine):
    """``lxg/Alpha101.csv`` (the bridge reads the lxg / ak trees by
    default): stepped onto the calendar, forward-filled, real coverage.
    The ``xq/`` family that used to be the example is opt-in now --
    ``QF_SCREEN_FACTOR_PROVIDERS`` names it."""
    panel = engine.panel
    table = panel.table("factor_Alpha101__alpha1")
    assert table.shape[0] == len(panel.calendar)
    covered = table.iloc[-1].notna()
    assert 200 < covered.sum()                      # 300 on the 2026-11 store
    assert np.isfinite(table["SH600519"].iloc[-1])  # held from the file's last row
    from qf_screen.data.factor_bridge import DEFAULT_PROVIDERS, providers
    assert DEFAULT_PROVIDERS[0] == "lxg" and "xq" not in DEFAULT_PROVIDERS
    assert providers() == DEFAULT_PROVIDERS                         # no opt-in here


def test_all_presets_schedule_nonempty(engine):
    spec = ScreenSpec.from_yaml("momentum_trend")
    calendar = engine.panel.calendar
    start = str(calendar[calendar.searchsorted(pd.Timestamp("2026-06-01"))].date())
    schedule, funnel = engine.schedule(spec, start=start,
                                       end=str(calendar[-1].date()), freq="M")
    assert not schedule.empty
    per_day = schedule.groupby("date")["symbol"].count()
    assert (per_day <= 30).all()                    # top_n honored per date


def test_unit_dependent_features_hold_their_units(engine):
    """The CLI prints turnover features as PERCENT POINTS and ``_mn``
    features as 百万元.  Pin the store's units so a provider change cannot
    turn every table 100x wrong without a test noticing (measured
    2026-11-26: median turnover rate 2.12, i.e. 2.12 %)."""
    panel = engine.panel
    pos = len(panel.calendar) - 2
    for name in ("turnoverrate", "turnover_ma20", "turnover_ma60"):
        median = np.nanmedian(panel.cross_section(name, pos))
        assert 0.2 < median < 25, (name, median)
    cny = panel.cross_section("amount_ma20", pos)
    millions = panel.cross_section("amount_ma20_mn", pos)
    ok = np.isfinite(cny) & (cny > 0)
    assert ok.sum() > 4000
    np.testing.assert_allclose(millions[ok] * 1e6, cny[ok], rtol=1e-9)


# ============================ v0.3 (2026-11-26) ============================
def _rows(text: str) -> list:
    return [line for line in text.splitlines() if re.match(r"^(SH|SZ|BJ)\d", line)]


def _passers(text: str) -> int:
    return int(re.search(r": (\d+) members as of", text).group(1))


def test_top_replaces_the_preset_top_n(capsys):
    """``--top N`` used to be read under the wrong attribute name and
    silently ignored (a ``top_n: 30`` preset stayed at 30)."""
    base = ["run", "momentum_trend", "--data-dir", DATA_DIR, "--as-of", "2026-11-24",
            "--format", "csv"]
    assert main([*base, "--top", "0"]) == 0
    every = len(_rows(capsys.readouterr().out))
    assert main([*base, "--top", "50"]) == 0
    text = capsys.readouterr().out
    assert len(_rows(text)) == min(50, every) and _passers(text) == min(50, every)
    assert main([*base, "--top", "3"]) == 0
    text = capsys.readouterr().out
    assert len(_rows(text)) == 3 and _passers(text) == 3       # the summary agrees
    if every > 30:                                     # 130 passers on the 2026-11 store
        assert min(50, every) == 50                    # upward, past the preset's 30


def test_adhoc_run_and_markdown_out(capsys, tmp_path):
    assert main(["run", "--data-dir", DATA_DIR, "--as-of", "2026-11-24",
                 "--where", "market_cap_bn > 2000", "--rank", "pct_rank(roe_best)",
                 "--top", "5", "--format", "md"]) == 0
    text = capsys.readouterr().out
    assert "## `adhoc` — 5 members as of 2026-11-24" in text
    assert "where1" in text and "liquidity" in text     # hygiene + the ad-hoc filter
    out = tmp_path / "value.md"
    assert main(["run", "value_dividend", "--data-dir", DATA_DIR, "--as-of", "2026-11-24",
                 "--format", "md", "--out", str(out)]) == 0
    assert f"wrote markdown to {out}" in capsys.readouterr().out
    body = out.read_text(encoding="utf-8")
    assert body.startswith("## `value_dividend` — ") and "### funnel" in body


def test_industry_restriction_works_offline(engine, capsys):
    """``--industry`` used to need ``--live``; it now matches the local 申万
    classification of ``lxg/industries.csv``."""
    assert main(["run", "momentum_trend", "--data-dir", DATA_DIR, "--as-of", "2026-11-24",
                 "--top", "0", "--industry", "医药生物", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    symbols = [row["symbol"] for row in payload["members"]]
    assert len(symbols) >= 10
    assert set(payload["industry_mix"]) == {"医药生物"}
    assert payload["funnel"][1]["rule"] == "symbols" \
        and int(payload["funnel"][1]["passing"]) > 100
    with pytest.raises(SystemExit) as exc:
        main(["run", "momentum_trend", "--data-dir", DATA_DIR, "--industry", "不存在的行业"])
    assert "no industry matches" in str(exc.value) and "--live" in str(exc.value)


def test_diff_inspect_and_report(engine):
    spec = ScreenSpec.from_yaml("momentum_trend")
    diff = engine.diff(spec)
    assert diff.prev < diff.asof
    current, previous = set(diff.current.members), set(diff.previous.members)
    assert set(diff.entered["symbol"]) == current - previous
    assert set(diff.exited["symbol"]) == previous - current
    assert diff.retained == len(current & previous)
    assert (diff.movers["change"] == diff.movers["rank_prev"] - diff.movers["rank_now"]).all()

    card = engine.inspect("SH600519")
    assert len(card.presets) == len(list_presets())
    assert card.info["name"] and card.info["board"] == "沪市主板" and card.info["industry"]
    for entry in card.presets:
        result = engine.run(ScreenSpec.from_yaml(entry["preset"]))
        assert entry["member"] == ("SH600519" in set(result.members))
        if not entry["member"]:
            # out on a rule (with what the rule read), or out on the
            # truncation alone (then: where it ranks among the passers)
            assert entry["failed"] and all(f["expr"] for f in entry["failed"]) \
                or entry["passes_rules"] and entry["pool_rank"] > entry["members"]
    pe = card.stats[card.stats["feature"] == "pe_ttm"].iloc[0]
    assert 0 < pe["pct_rank"] < 1 and pe["covered"] > 4000

    markdown, data = daily_report(engine, [ScreenSpec.from_yaml(n) for n in list_presets()],
                                  top=5)
    assert markdown.startswith("# qf-screen daily briefing — ")
    for name in list_presets():
        assert f"## `{name}` — " in markdown and len(data["presets"][name]["members"]) >= 1
    assert "## Data warnings" in markdown            # the store has stale alt sources
    assert main(["report", "--data-dir", DATA_DIR, "--as-of", "2026-11-24", "--top", "3"]) == 0


def test_validate_follows_the_portfolio_against_its_benchmarks(engine):
    report = engine.validate(ScreenSpec.from_yaml("momentum_trend"),
                             start="2026-01-05", end="2026-09-30",
                             horizons=(5, 10, 20), freq="M")
    assert {"portfolio", "universe", "csi300", "csi500"} <= set(report.stats)
    assert {"ann_return", "ann_vol", "sharpe", "max_drawdown", "cum_return"} <= \
        set(report.stats["portfolio"])
    assert report.stats["portfolio"]["days"] > 100
    assert -1 < report.stats["portfolio"]["max_drawdown"] <= 0
    assert report.turnover["rebalances"] >= 5 and 0 <= report.turnover["mean"] <= 1
    assert report.spread["excess_cum"] == pytest.approx(
        report.curve["portfolio"].iloc[-1] - report.curve["universe"].iloc[-1])
    # the benchmark is the lxg index file's own close path over the same sessions
    index = engine.store.index_close("000300")
    dates = report.curve["csi300"].dropna().index
    aligned = index.reindex(engine.panel.calendar, method="ffill")
    positions = [engine.panel.calendar.get_loc(d) for d in dates]
    assert report.stats["csi300"]["cum_return"] == pytest.approx(
        aligned.iloc[positions[-1]] / aligned.iloc[positions[0] - 1] - 1.0, rel=1e-9)
    assert "turnover" in report.summary() and "csi300" in report.summary()
    # the same over the CLI, curve and all
    assert main(["validate", "momentum_trend", "--data-dir", DATA_DIR,
                 "--start", "2026-06-01", "--end", "2026-09-30", "--horizons", "5",
                 "--where", "close_raw > 0", "--rank", "pct_rank(high252_ratio)",
                 "--boards", "main", "--top", "5"]) == 0


def test_compare_runs_presets_side_by_side(engine, capsys, tmp_path):
    specs = [ScreenSpec.from_yaml(n) for n in ("value_dividend", "momentum_trend",
                                               "low_vol_defensive")]
    table, reports = engine.compare(specs, start="2026-01-05", end="2026-09-30",
                                    horizons=(5, 20), freq="M")
    assert list(table.columns)[:3] == [s.name for s in specs]
    assert "universe" in table.columns and "csi300" in table.columns
    assert {"members", "turnover", "ann_return", "sharpe", "max_drawdown", "excess_ann",
            "spread_20d", "hit_20d"} <= set(table.index)
    for spec in specs:
        assert table.loc["ann_return", spec.name] == pytest.approx(
            reports[spec.name].stats["portfolio"]["ann_return"])
    out = tmp_path / "compare.csv"
    assert main(["compare", "value_dividend", "momentum_trend", "--data-dir", DATA_DIR,
                 "--start", "2026-06-01", "--end", "2026-09-30", "--horizons", "5",
                 "--benchmarks", "", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "ann_return" in text and "spread_5d" in text
    assert list(pd.read_csv(out, index_col=0).columns) == ["value_dividend",
                                                           "momentum_trend", "universe"]


def test_cache_status_finds_a_fresh_store_cache():
    status = cache_ops.cache_status(DATA_DIR)
    kinds = {row["kind"] for row in status["kinds"]}
    assert {"store", "universe", "feature"} <= kinds
    tail = [row for row in status["stores"]
            if row["kind"] == "store" and row.get("fresh") is not None]
    assert tail and all(row["fresh"] for row in tail)
    assert all(row["window"] for row in status["stores"])


def test_tuned_quality_growth_grows_off_a_profitable_base(engine):
    """Every member's growth is measured against a base that already earned
    an 8 % net margin and is cash-backed; the growth ranks are capped, so
    the raw percentage no longer orders the list."""
    result = engine.run(ScreenSpec.from_yaml("quality_growth"))
    assert len(result.members) >= 5
    panel, pos = engine.panel, engine.panel.position_of(result.asof)
    margin = panel.cross_section("net_margin_py", pos)
    cash = panel.cross_section("ocf_ttm", pos)
    for symbol in result.members:
        at = panel.symbols.get_loc(symbol)
        assert margin[at] >= 0.08 and cash[at] > 0, symbol
    above = result.to_frame()
    above = above[above["profit_growth"] > 1.0]       # the names the cap ties ...
    if len(above) >= 3:                                # 7 on the 2026-11 store
        # ... so the list is no longer in raw-growth order (the member frame
        # is in score order): 融捷 874 % ranks above 中际旭创 210 %, which
        # ranks above 兆易 752 %
        assert not above["profit_growth"].is_monotonic_decreasing


def test_tuned_value_dividend_judges_leverage_where_it_can(engine):
    """The balance leg passes names without lxg statements unjudged (the
    screen is no longer the ~765-name statement subset) -- visibly, and
    never for a bank / broker / insurer."""
    result = engine.run(ScreenSpec.from_yaml("value_dividend"))
    assert len(result.members) >= 5
    panel, pos = engine.panel, engine.panel.position_of(result.asof)
    leverage = panel.cross_section("debt_assets", pos)
    financial = panel.cross_section("financial", pos)
    judged = 0
    for symbol in result.members:
        at = panel.symbols.get_loc(symbol)
        assert financial[at] == 0, symbol
        if np.isfinite(leverage[at]):
            assert leverage[at] < 0.6
            judged += 1
    assert judged >= 1                                 # a statement name still clears it
    funnel = dict(zip(result.funnel["rule"], result.funnel.to_dict(orient="records")))
    assert funnel["balance"]["na_pass"] is True and funnel["balance"]["unknown"] > 1000
    assert any("balance" in note and "na_pass" in note for note in result.notes())


def test_warm_run_is_served_from_the_caches():
    """A cold merge plus the universe scan take 30 s and more on this store;
    a warm run about a second.  The bound is loose for any machine but a
    cache that silently stops hitting (a broken file lookup once made every
    derived table rebuild) runs tens of seconds and fails it."""
    CNDataStore(DATA_DIR, max_workers=4)                # whatever it costs: warm it
    began = time.perf_counter()
    store = CNDataStore(DATA_DIR, max_workers=4)
    result = ScreenEngine(store).run(ScreenSpec.from_yaml("value_dividend"))
    elapsed = time.perf_counter() - began
    assert len(result.members) >= 5 and elapsed < 8.0, f"warm run took {elapsed:.1f}s"


# ============================ v0.4 (2026-11-26) ============================
ASOF = "2026-11-24"                      # the decision date the tests above use too
WINDOW = ["--start", "2026-06-01", "--end", "2026-09-30", "--horizons", "5"]   # cached by them
PRESETS = ("low_vol_defensive", "momentum_trend", "quality_growth", "smart_money_ashare",
           "value_dividend")


def _run(argv, capsys, fmt=None):
    """``main([...])`` must succeed; its stdout comes back as text or JSON."""
    assert main([*argv, *(["--format", fmt] if fmt else [])]) == 0
    out = capsys.readouterr().out
    return json.loads(out) if fmt == "json" else out


def test_report_accepts_comma_separated_presets(tmp_path, capsys):
    """``report --presets a,b`` failed on a preset literally named ``a,b``."""
    out = tmp_path / "report.md"
    _run(["report", "--data-dir", DATA_DIR, "--as-of", ASOF, "--top", "3", "--out", str(out),
          "--presets", "value_dividend,momentum_trend"], capsys)
    assert re.findall(r"^## `([^`]+)` — ", out.read_text(encoding="utf-8"), flags=re.M) == [
        "value_dividend", "momentum_trend"]
    payload = _run(["report", "--data-dir", DATA_DIR, "--as-of", ASOF,
                    "--presets", "value_dividend, momentum_trend"], capsys, "json")
    assert list(payload["presets"]) == ["value_dividend", "momentum_trend"]


def test_validate_prices_in_costs_and_refuses_limit_up_entries(tmp_path, capsys):
    base = ["validate", "momentum_trend", "--data-dir", DATA_DIR, *WINDOW]
    plain = _run(base, capsys, "json")
    priced = _run([*base, "--cost-bps", "15", "--skip-limit-up"], capsys, "json")
    assert plain["cost_bps"] == 0 and plain["net"] == {} and plain["skipped"] == {}
    assert priced["cost_bps"] == 15.0
    # the screen is the same whatever the book pays or refuses: same members, same horizons
    assert priced["horizons"] == plain["horizons"]
    assert [row["members"] for row in priced["per_date"]] == \
        [row["members"] for row in plain["per_date"]]
    # the guard refused a real, small share of the entries (1 of 90 over this
    # window's three rebalances on the 2026-11 store; 2 of 240 over January-September)
    assert 1 <= priced["skipped"]["skipped"] and priced["skipped"]["entries"] >= 60
    assert 0 < priced["skipped"]["share"] < 0.05
    # the net twin is the run's OWN gross twin less its costs -- the gross twin here
    # excludes the refused entries too, so the plain run is not the baseline
    gross, net = priced["stats"]["portfolio"], priced["stats"]["portfolio_net"]
    assert net["ann_return"] < gross["ann_return"] and net["sharpe"] < gross["sharpe"]
    assert net["days"] == gross["days"] and net["max_drawdown"] <= gross["max_drawdown"] + 1e-12
    drag = priced["net"]["cost_drag_ann"]
    assert drag == pytest.approx(gross["ann_return"] - net["ann_return"]) and 0 < drag < 0.10
    assert priced["net"]["excess_ann"] < priced["spread"]["excess_ann"]
    assert "portfolio_net" in priced["curve"] and "cost" in priced["per_date"][0]
    text = _run([*base, "--cost-bps", "15", "--skip-limit-up"], capsys)
    for needle in ("15 bps cost per side, limit-up entries skipped", "portfolio_net",
                   "excess_net", "costs        15 bps per side", "limit-up     skipped"):
        assert needle in text, needle
    table = tmp_path / "c.csv"
    _run(["compare", "value_dividend", "momentum_trend", "--data-dir", DATA_DIR, *WINDOW,
          "--benchmarks", "", "--cost-bps", "15", "--out", str(table)], capsys)
    compared = pd.read_csv(table, index_col=0)
    for metric in ("ann_return", "sharpe", "cum_return"):
        assert compared.loc[f"{metric}_net", "momentum_trend"] < \
            compared.loc[metric, "momentum_trend"]
    assert np.isnan(compared.loc["ann_return_net", "universe"])     # the references stay gross


def test_watch_reports_the_rules_that_fire(capsys):
    base = ["watch", "--data-dir", DATA_DIR, "--as-of", ASOF,
            "--symbols", "SH600519,SZ000858,SH600036", "--preset", "value_dividend"]
    payload = _run(base, capsys, "json")
    assert payload["symbols"] == 3 and payload["preset"] == "value_dividend"
    assert {row["symbol"] for row in payload["rows"]} == {"SH600519", "SZ000858", "SH600036"}
    assert [rule["name"] for rule in payload["rules"]] == [
        "st", "pledge", "trend_break", "liquidity", "suspended", "preset_exit"]
    assert payload["alerts"] == len(payload["detail"]) > 0
    assert sum(payload["by_severity"].values()) == payload["alerts"]
    assert all(alert["rule"] in {rule["name"] for rule in payload["rules"]}
               for alert in payload["detail"])
    # none of the three sits in the preset's list, and each is told which rules it fails
    exits = [alert for alert in payload["detail"] if alert["rule"] == "preset_exit"]
    assert {alert["symbol"] for alert in exits} == {"SH600519", "SZ000858", "SH600036"}
    assert all(alert["message"].startswith("out of value_dividend: fails ")
               for alert in exits)
    assert all(row["name"] for row in payload["rows"])            # 贵州茅台, 五粮液, 招商银行
    text = _run(base, capsys)
    assert text.startswith(f"watch: 3 symbols as of {ASOF} (preset: value_dividend) -- ")
    assert "rules in force: st, pledge, trend_break, liquidity, suspended, preset_exit" in text
    assert re.search(r"^\s+MEDIUM\s+preset_exit\s+out of value_dividend: fails ", text,
                     flags=re.M)
    assert main([*base, "--fail-on-alert"]) == 2                  # what a cron job keys on
    capsys.readouterr()
    # without --preset there is no preset-exit alert, and the code follows the count
    loose = base[:-2]                                            # ... without --preset
    plain = _run(loose, capsys, "json")
    assert plain["preset"] is None and "preset_exit" not in {rule["name"] for rule in plain["rules"]}
    assert all(alert["rule"] != "preset_exit" for alert in plain["detail"])
    assert main([*loose, "--fail-on-alert"]) == (2 if plain["alerts"] else 0)


def test_watch_reads_a_watchlist_file(tmp_path, capsys):
    book = tmp_path / "book.yaml"
    book.write_text("preset: value_dividend\nsymbols: [SH600519, SZ000858]\n"
                    "rules:\n  - {name: dear, expr: \"pe_ttm > 15\", severity: low}\n"
                    "disable: [pledge, suspended]\nthresholds: {liquidity: 5e8}\n",
                    encoding="utf-8")
    payload = _run(["watch", "--data-dir", DATA_DIR, "--as-of", ASOF, "--watchlist", str(book)],
                   capsys, "json")
    assert [rule["name"] for rule in payload["rules"]] == [
        "st", "trend_break", "liquidity", "dear", "preset_exit"]
    assert [rule["source"] for rule in payload["rules"]] == ["built-in"] * 3 + ["custom", "preset"]
    dear = [alert for alert in payload["detail"] if alert["rule"] == "dear"]
    assert len(dear) == 2 and all(alert["severity"] == "low" for alert in dear)
    assert "pe_ttm " in dear[0]["message"]                          # what the rule read


def test_html_reports_are_self_contained(tmp_path, capsys):
    from test_v04_product import _check_markup

    pages = {}
    for name, argv in (
            ("report", ["report", "--data-dir", DATA_DIR, "--as-of", ASOF, "--top", "5"]),
            ("validate", ["validate", "momentum_trend", "--data-dir", DATA_DIR, *WINDOW,
                          "--cost-bps", "15", "--skip-limit-up"]),
            ("compare", ["compare", "value_dividend", "momentum_trend", "low_vol_defensive",
                         "--data-dir", DATA_DIR, *WINDOW, "--benchmarks", "", "--cost-bps",
                         "15"])):
        pages[name] = tmp_path / f"{name}.html"
        assert main([*argv, "--format", "html", "--out", str(pages[name])]) == 0
        assert f"wrote HTML report to {pages[name]}" in capsys.readouterr().out
    body = {name: path.read_text(encoding="utf-8") for name, path in pages.items()}
    found = {name: _check_markup(text, name) for name, text in body.items()}
    assert {name: (found[name]["plots"], found[name]["blobs"]) for name in found} == {
        "report": (5, 0), "validate": (2, 2), "compare": (2, 2)}
    report = body["report"]
    for name in PRESETS:
        assert f"<code>{name}</code> — " in report
    assert 'class="badge new"' in report and 'class="badge out"' in report
    assert 'class="pill"' in report and "Industry concentration" in report
    assert re.search(r"[\u4e00-\u9fff]{2,}", report)      # CJK names survive the round trip
    validate = body["validate"]
    assert "15 bps cost per side, limit-up entries skipped" in validate
    assert '<path class="line series-1 net" ' in validate and "Drawdown from the running peak" in validate
    assert "Annualized return (net)" in body["compare"]
    assert '<path class="line series-3 dash-3" ' in body["compare"]
    for text in body.values():
        assert "<!doctype html>" in text and "Generated by qf-screen" in text


def test_a_user_preset_runs_by_name(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "presets"
    scaffold = directory / "my_screen.yaml"
    assert main(["init-preset", "my_screen", "--from", "momentum_trend", "--out",
                 str(scaffold)]) == 0
    assert f"wrote {scaffold}" in capsys.readouterr().out
    by_path = ["run", str(scaffold), "--data-dir", DATA_DIR, "--as-of", ASOF, "--format", "csv"]
    assert "my_screen: 30 members as of" in _run(by_path, capsys)            # by path
    monkeypatch.setenv("QF_SCREEN_PRESETS_DIR", str(directory))
    listing = _run(["list"], capsys)                              # ... and by name
    assert f"(top_n 30; user: {scaffold})" in listing
    assert "(top_n 30; built-in)" in listing
    by_name = by_path[:1] + ["my_screen"] + by_path[2:]
    assert "my_screen: 30 members as of" in _run(by_name, capsys)
    card = _run(["inspect", "SH600519", "--data-dir", DATA_DIR, "--as-of", ASOF], capsys, "json")
    assert "my_screen" in {entry["preset"] for entry in card["presets"]}
    report = tmp_path / "r.md"
    _run(["report", "--data-dir", DATA_DIR, "--as-of", ASOF, "--presets", "my_screen",
          "--out", str(report)], capsys)
    assert "## `my_screen` — 30 members" in report.read_text(encoding="utf-8")


def test_history_round_trip(tmp_path, capsys):
    from qf_screen import cache_ops

    archive = tmp_path / "archive"
    for date in ("2026-11-23", ASOF):
        assert main(["run", "momentum_trend", "--data-dir", DATA_DIR, "--as-of", date,
                     "--save-history", str(archive), "--out", str(tmp_path / "m.csv")]) == 0
        assert f"history: 30 rows for momentum_trend as of {date} -> " in capsys.readouterr().err
    base = ["history", "momentum_trend", "--data-dir", DATA_DIR, "--history-dir", str(archive)]
    payload = _run(base, capsys, "json")
    assert payload["archived_sessions"] == 2 and payload["window"] == ["2026-11-23", ASOF]
    assert payload["asof"] == ASOF and payload["note"] == ""
    assert payload["members_per_session"] == {"2026-11-23": 30, ASOF: 30}
    members = payload["members"]
    assert [row["rank"] for row in members] == [str(rank) for rank in range(1, 31)]
    assert all(row["name"] for row in members)
    assert {row["streak"] for row in members} <= {"1", "2"} and {row["present"] for row in members} <= {"1", "2"}
    stayed = sum(row["streak"] == "2" for row in members)
    assert payload["new_today"] == 30 - stayed and len(payload["exited"]) == 30 - stayed
    current = {row["symbol"] for row in members}
    for row in payload["exited"]:                        # in the list yesterday, gone today
        assert row["last_seen"] == "2026-11-23" and row["run"] == "1"
        assert 1 <= int(row["last_rank"]) <= 30 and row["symbol"] not in current
    text = _run([*base, "--days", "1"], capsys)          # one session: every member is new
    assert text.startswith(f"momentum_trend: 30 members as of {ASOF} · 30 new today")
    assert "counting the last 1 (" + ASOF + ".." + ASOF + ")" in text
    assert f"momentum_trend: 30 members as of {ASOF} · {payload['new_today']} new today" \
        in _run(base, capsys)
    # the cache's own collector on the real cache -- plans only, nothing removed
    stale = cache_ops.cache_prune(DATA_DIR, stale=True, dry_run=True)
    assert all(row["reason"] == "stale" for row in stale["remove"])
    daily = [row for row in cache_ops.cache_status(DATA_DIR)["stores"] if row["kind"] == "store"]
    assert daily and all(row["fresh"] for row in daily)
    assert not [row for row in stale["remove"] if row["kind"] == "store"]
    budget = cache_ops.cache_prune(DATA_DIR, max_gb=0.25, dry_run=True)
    assert budget["after_bytes"] <= 0.25 * 1024 ** 3 < budget["total_bytes"]
    assert budget["counts"]["lru"] == len(budget["remove"])
    assert cache_ops.cache_prune(DATA_DIR, older_than=365, dry_run=True)["remove"] == []
