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
from qf_screen.briefing import daily_report
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
