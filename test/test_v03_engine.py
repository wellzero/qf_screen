"""v0.3 engine tests: the funnel's coverage column, the ``na_pass`` notes,
the ``max_per_industry`` cap, ``diff``, ``inspect``, the validation's
portfolio path / turnover / benchmarks, ``compare`` and the daily briefing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import conftest as cf
from qf_screen.ui.briefing import (
    daily_report, diff_markdown, diff_payload, diff_text, inspect_payload,
)
from qf_screen.engine import (
    BENCHMARK_UNIVERSE, UNCATEGORIZED, ScreenDiff, ScreenEngine, ScreenSpec, StockCard,
    UniverseSpec, ValidationReport,
)
from qf_screen.data import CNDataStore
from qf_screen.presets import PresetError
from qf_screen.rules import FeaturePanel

pytestmark = pytest.mark.unit

END = str(cf.END.date())
OPEN = dict(boards=("all",), exclude_st=False, min_listed_days=None)

#: four extra lxg names so an industry cap has something to bite on
EXTRA = {f"SH60010{i}": {"layout": "a", "start": 6.0 + i, "drift": 0.0012}
         for i in range(1, 5)}
INDUSTRIES = {
    "SH600001": ("银行", "国有大行", "国有大行"),
    "SH600101": ("银行", "股份制银行", "股份制银行"),
    "SZ000002": ("煤炭", "动力煤", "动力煤"),
    "SH600102": ("煤炭", "焦炭", "焦炭"),
    "SH600005": ("食品饮料", "白酒", "白酒"),
    "BJ430004": ("食品饮料", "乳品", "乳品"),
    "SH600103": ("电子", "半导体", "半导体"),
    "SH600104": ("电子", "消费电子", "消费电子"),
}                                      # SH600003 (ak-only) stays unclassified


def _tree(root, industries=None, extra=True):
    cf._company_list(root)
    specs = {**cf.SYMBOLS, **(EXTRA if extra else {})}
    for symbol, spec in specs.items():
        cf._symbol_files(root, symbol, spec)
    for symbol, levels in (industries or {}).items():
        cf._write_csv(root / f"cn/equity/{symbol}/lxg/industries.csv", pd.DataFrame({
            "areaCode": ["cn"] * 3, "source": ["sw_2021"] * 3, "name": list(levels)}))
    return root


def _index_file(root, code="SH000300", daily=0.0005):
    """A CSI-300 style benchmark: ``index/<DIR>/lxg/daily.csv``."""
    cf._write_csv(root / f"cn/index/{code}/lxg/daily.csv", pd.DataFrame({
        "date": cf.CALENDAR.strftime("%Y-%m-%d"),
        "close": 100.0 * np.cumprod(np.full(len(cf.CALENDAR), 1.0 + daily))}))


CHEAP = ScreenSpec(
    name="cheap", filters=[{"name": "cheap", "expr": "pe_ttm > 0"}],
    rank=[{"expr": "pct_rank(earnings_yield)", "weight": 1.0}], top_n=5,
    universe=UniverseSpec(**OPEN))
MOVER = ScreenSpec(
    name="mover", filters=[{"name": "hot", "expr": "ret_21d > 0.02"}],
    rank=[{"expr": "pct_rank(ret_21d)", "weight": 1.0}], top_n=2,
    universe=UniverseSpec(**OPEN))


# ------------------------------------------------------ funnel coverage
def test_funnel_counts_the_names_a_rule_could_not_judge(store):
    """SH600003 has no lxg statements: a balance rule cannot judge it.  It
    FAILS the strict rule and PASSES one with na_pass -- and the funnel
    says how many such names there are either way."""
    balance = {"name": "balance", "expr": "debt_assets < 0.6"}
    strict = ScreenSpec(name="s", filters=[dict(balance)], universe=UniverseSpec(**OPEN))
    relaxed = ScreenSpec(name="r", filters=[{**balance, "na_pass": True}],
                         universe=UniverseSpec(**OPEN))
    engine = ScreenEngine(store)
    strict_result = engine.run(strict)
    funnel = dict(zip(strict_result.funnel["rule"],
                      strict_result.funnel.to_dict(orient="records")))
    assert funnel["balance"]["unknown"] == 1 and funnel["balance"]["na_pass"] is False
    assert funnel["balance"]["passing"] == 4                    # SH600003 fails
    panel = FeaturePanel(store)
    values = panel.cross_section("debt_assets", len(panel.calendar) - 1)
    assert int((~np.isfinite(values)).sum()) == 1               # the one unjudged name
    assert "SH600003" not in set(strict_result.members)
    # a rule whose every feature each name has leaves nothing unjudged
    judged = engine.run(ScreenSpec(name="j", filters=[{"name": "cheap", "expr": "pe_ttm > 0"}],
                                   universe=UniverseSpec(**OPEN)))
    assert set(judged.funnel["unknown"]) == {0}

    relaxed_result = engine.run(relaxed)
    funnel = dict(zip(relaxed_result.funnel["rule"],
                      relaxed_result.funnel.to_dict(orient="records")))
    assert funnel["balance"]["passing"] == 5 and funnel["balance"]["na_pass"] is True
    info = relaxed_result.explain("SH600003")
    assert info["rules"]["balance"] is True and info["unknown"] == ["balance"]
    assert any("balance: 1 names passed WITHOUT data" in note
               for note in relaxed_result.notes())
    assert engine.run(strict).notes() == []                     # nothing hidden


def test_explain_reports_rank_and_unknown_rules(store):
    engine = ScreenEngine(store)
    result = engine.run(CHEAP, asof=END)
    info = result.explain(result.members[0])
    assert info["member"] is True and info["rank"] == 1 and "score" in info
    assert "unknown" in info and "rule_exprs" in info


# ------------------------------------------------------ industry cap
def test_rank_members_applies_the_cap_while_filling_the_list():
    """Walk the ranking; skip a name whose industry is full; the next-best
    name takes the slot, so the list stays top_n long."""
    symbols = pd.Index(list("ABCDEFGH"))
    scores = np.array([.9, .8, .7, .6, .5, .4, .3, .2])
    industry = np.array(["x", "x", "x", "y", "y", "z", "", "x"], dtype=object)
    capped: list = []
    spec = ScreenSpec(name="t", filters=[], top_n=4, max_per_industry=2)
    members = ScreenEngine._rank_members(spec, np.ones(8, bool), scores, symbols,
                                         industry, capped)
    assert list(members) == list("ABDE") and capped == ["C"]
    # cap 1: A (x), D (y), F (z), then the UNCLASSIFIED G is never capped
    spec = ScreenSpec(name="t", filters=[], top_n=4, max_per_industry=1)
    capped = []
    assert list(ScreenEngine._rank_members(spec, np.ones(8, bool), scores, symbols,
                                           industry, capped)) == list("ADFG")
    assert capped == ["B", "C"]                         # both displaced by deeper names
    # a name in two industries uses both quotas
    industry = np.array(["x", "x/y", "y", "", "", "", "", ""], dtype=object)
    spec = ScreenSpec(name="t", filters=[], top_n=8, max_per_industry=1)
    assert list(ScreenEngine._rank_members(spec, np.ones(8, bool), scores, symbols,
                                           industry)) == list("ACDEFGH")   # only B is skipped
    # no cap, or no tags: the plain ranked list
    spec = ScreenSpec(name="t", filters=[], top_n=3)
    assert list(ScreenEngine._rank_members(spec, np.ones(8, bool), scores, symbols,
                                           industry)) == list("ABC")
    assert list(ScreenEngine._rank_members(spec, np.ones(8, bool), scores, symbols)) \
        == list("ABC")
    # a missing score cannot rank
    scores[0] = np.nan
    assert list(ScreenEngine._rank_members(spec, np.ones(8, bool), scores, symbols)) \
        == list("BCD")


def test_max_per_industry_is_validated(store_dir, tmp_path):
    with pytest.raises(ValueError):
        ScreenSpec(name="t", filters=[], max_per_industry=0)
    with pytest.raises(ValueError):
        ScreenSpec(name="t", filters=[], max_per_industry="3")
    bad = tmp_path / "cap.yaml"
    bad.write_text("name: cap\nfilters: [{name: a, expr: 'pe_ttm > 0'}]\n"
                   "max_per_industry: 0\n", encoding="utf-8")
    with pytest.raises(PresetError):
        ScreenSpec.from_yaml(str(bad))
    bad.write_text("name: cap\nfilters: [{name: a, expr: 'pe_ttm > 0'}]\n"
                   "max_per_industry: two\n", encoding="utf-8")
    with pytest.raises(PresetError):
        ScreenSpec.from_yaml(str(bad))
    good = tmp_path / "ok.yaml"
    good.write_text("name: ok\nfilters: [{name: a, expr: 'pe_ttm > 0'}]\n"
                    "top_n: 4\nmax_per_industry: 2\n", encoding="utf-8")
    assert ScreenSpec.from_yaml(str(good)).max_per_industry == 2


def _reference(symbols, scores, industry, cap, top):
    """The documented selection, written out independently of the engine."""
    order = [int(i) for i in np.argsort(-scores, kind="stable") if np.isfinite(scores[i])]
    counts: dict = {}
    out = []
    for i in order:
        tags = [t for t in str(industry[i]).split("/") if t]
        if any(counts.get(t, 0) >= cap for t in tags):
            continue
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
        out.append(symbols[i])
        if len(out) == top:
            break
    return out


def test_industry_cap_in_run_and_schedule(tmp_path):
    root = _tree(tmp_path / "tree", INDUSTRIES)
    store = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False)
    engine = ScreenEngine(store)
    capped_spec = ScreenSpec(
        name="capped", filters=[{"name": "cheap", "expr": "pe_ttm > 0"}],
        rank=[{"expr": "pct_rank(earnings_yield)", "weight": 1.0}], top_n=5,
        max_per_industry=1, universe=UniverseSpec(**OPEN))
    plain = ScreenSpec(**{**capped_spec.__dict__, "name": "plain",
                          "max_per_industry": None})
    result = engine.run(capped_spec, asof=END)
    reference = engine.run(plain, asof=END)
    # the cheapest five names here repeat two industries (食品饮料, 银行), so
    # the cap displaces two of them and deeper names fill the list
    assert len(result.members) == 5 == len(reference.members)
    # every industry appears at most once, and the unclassified name is free
    tags = dict(zip(engine.panel.symbols, engine._industry_tags()))
    mix = [tags[s] or UNCATEGORIZED for s in result.members]
    assert max(mix.count(tag) for tag in set(mix) if tag != UNCATEGORIZED) == 1
    scores = engine._evaluate(capped_spec, engine.panel.position_of(result.asof))[2]
    assert list(result.members) == _reference(engine.panel.symbols, scores,
                                              engine._industry_tags(), 1, 5)
    displaced = set(reference.members) - set(result.members)
    assert set(result.capped) == displaced and len(displaced) > 0
    assert any("max_per_industry 1: " in note and ", ".join(sorted(displaced)) in note
               for note in result.notes())
    assert reference.capped.size == 0 and not [n for n in reference.notes()
                                               if "max_per_industry" in n]
    # the same cap on a historical schedule, date by date
    schedule, _funnel = engine.schedule(capped_spec, start=str(cf.CALENDAR[100].date()),
                                        end=str(cf.CALENDAR[-1].date()), freq="M")
    assert not schedule.empty
    for _date, rows in schedule.groupby("date"):
        tags_here = [tags[s] or UNCATEGORIZED for s in rows["symbol"]]
        known = [tag for tag in tags_here if tag != UNCATEGORIZED]
        assert len(known) == len(set(known))
        assert len(rows) == 5                          # 4 industries + the unclassified
    # the raw-pool export applies neither the truncation nor the cap
    pooled, _funnel = engine.schedule(capped_spec, start=str(cf.CALENDAR[-1].date()),
                                      end=str(cf.CALENDAR[-1].date()), apply_top_n=False)
    assert len(pooled) > len(schedule[schedule["date"] == schedule["date"].max()])


def test_industry_mix_counts_members_per_tag(tmp_path):
    root = _tree(tmp_path / "tree", INDUSTRIES)
    engine = ScreenEngine(CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False))
    result = engine.run(ScreenSpec(
        name="all", filters=[{"name": "cheap", "expr": "pe_ttm > 0"}], top_n=20,
        universe=UniverseSpec(**OPEN)), asof=END)
    mix = result.industry_mix()
    assert int(mix.sum()) == len(result.members) == 9
    assert mix["银行"] == 2 and mix[UNCATEGORIZED] == 1 and mix.is_monotonic_decreasing
    assert engine.run(ScreenSpec(name="none", filters=[{"name": "x", "expr": "pe_ttm < 0"}],
                                 universe=UniverseSpec(**OPEN))).industry_mix().empty


# ------------------------------------------------------------------ diff
def test_diff_entered_exited_movers_and_retention(store):
    engine = ScreenEngine(store)
    diff = engine.diff(MOVER, asof=END)
    assert isinstance(diff, ScreenDiff) and diff.prev == engine.panel.calendar[-2]
    assert diff.asof.date() == cf.END.date()
    current, previous = set(diff.current.members), set(diff.previous.members)
    assert set(diff.entered["symbol"]) == current - previous
    assert set(diff.exited["symbol"]) == previous - current
    assert diff.retained == len(current & previous)
    movers = diff.movers
    assert set(movers["symbol"]) <= current & previous
    assert (movers["change"] == movers["rank_prev"] - movers["rank_now"]).all()
    assert movers["change"].is_monotonic_decreasing                 # gains first
    assert "rank" in diff.entered.columns and "rank" in diff.exited.columns
    assert "entered" in diff.summary() and "retained" in diff.summary()
    payload = diff_payload(diff)
    assert payload["retained"] == diff.retained and payload["ranked"] is True
    assert "rank movers" in diff_markdown(diff)
    # --prev picks the earlier snapshot
    earlier = engine.diff(MOVER, asof=END, prev=str(cf.CALENDAR[-10].date()))
    assert earlier.prev == cf.CALENDAR[-10]
    with pytest.raises(ValueError):
        engine.diff(MOVER, asof=END, prev=END)                  # not earlier
    with pytest.raises(ValueError):
        engine.diff(MOVER, asof=str(cf.CALENDAR[0].date()))      # no session before


def test_diff_of_an_unranked_screen_has_no_movers(store):
    engine = ScreenEngine(store)
    spec = ScreenSpec(name="flat", filters=[{"name": "hot", "expr": "ret_21d > 0.02"}],
                      top_n=2, universe=UniverseSpec(**OPEN))
    diff = engine.diff(spec, asof=END)
    assert diff.ranked is False and diff.movers.empty
    assert "rank" not in diff.entered.columns and "no ranking" in diff_text(diff)


# --------------------------------------------------------------- inspect
def test_inspect_one_name_through_the_presets(store):
    engine = ScreenEngine(store)
    card = engine.inspect("SH600001", asof=END,
                          presets=["value_dividend", "momentum_trend"])
    assert isinstance(card, StockCard)
    assert card.info == {"symbol": "SH600001", "name": "公司600001", "board": "沪市主板",
                         "industry": "食品饮料"}
    assert [c["preset"] for c in card.presets] == ["value_dividend", "momentum_trend"]
    for entry in card.presets:
        result = engine.run(ScreenSpec.from_yaml(entry["preset"]), asof=END)
        assert entry["member"] == ("SH600001" in set(result.members))
        if entry["member"]:
            assert entry["rank"] == list(result.members).index("SH600001") + 1
            assert entry["failed"] == []
        else:
            assert entry["rank"] is None and entry["failed"]
            failed_rules = [rule for rule, ok in result.explain("SH600001")["rules"].items()
                            if not ok]
            assert [f["rule"] for f in entry["failed"]] == failed_rules
            for failure in entry["failed"]:
                assert failure["expr"] == result.explain("SH600001")["rule_exprs"][
                    failure["rule"]]
    stats = card.stats
    assert {"family", "feature", "value", "pct_rank", "covered"} <= set(stats.columns)
    assert {"valuation", "quality", "momentum", "alt"} <= set(stats["family"])
    row = stats[stats["feature"] == "pe_ttm"].iloc[0]
    pos = engine.panel.position_of(cf.END)
    values = engine.panel.cross_section("pe_ttm", pos)
    assert row["value"] == pytest.approx(values[engine.panel.symbols.get_loc("SH600001")])
    assert row["pct_rank"] == pytest.approx((values < row["value"]).mean())
    # statement features are missing for the ak-only name and show no percentile
    card3 = engine.inspect("SH600003", asof=END, presets=["value_dividend"])
    roe = card3.stats[card3.stats["feature"] == "roe"].iloc[0]
    assert np.isnan(roe["value"]) and np.isnan(roe["pct_rank"]) and roe["covered"] == 4
    payload = inspect_payload(card)
    assert payload["passed"] == card.passed and "presets" in payload
    with pytest.raises(ValueError):
        engine.inspect("SZ399001", asof=END)


# -------------------------------------------------------------- validate
def test_validate_reports_the_portfolio_path_with_benchmarks(tmp_path):
    root = _tree(tmp_path / "tree", extra=False)
    _index_file(root)
    engine = ScreenEngine(CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False))
    spec = ScreenSpec(
        name="v", filters=[{"name": "cheap", "expr": "pe_ttm > 0"}],
        rank=[{"expr": "pct_rank(earnings_yield)", "weight": 1.0}], top_n=2,
        universe=UniverseSpec(**OPEN))
    report = engine.validate(spec, start="2026-02-01", end=END, horizons=(5,), freq="M")
    assert isinstance(report, ValidationReport) and report.horizons[5]["dates"] >= 3
    assert set(report.stats) == {"portfolio", "universe", "csi300"}
    for stats in report.stats.values():
        assert {"cum_return", "ann_return", "ann_vol", "sharpe", "max_drawdown", "days"} \
            <= set(stats)
        assert -1.0 <= stats["max_drawdown"] <= 0 and stats["days"] > 20
    # the benchmark is a known series: +0.05 % a session
    days = int(report.stats["csi300"]["days"])
    assert report.stats["csi300"]["cum_return"] == pytest.approx(1.0005 ** days - 1, rel=1e-9)
    assert report.stats["csi300"]["ann_vol"] == pytest.approx(0.0, abs=1e-9)
    # the portfolio's first period, re-done by hand: equal weight at the
    # first rebalance, held (not re-weighted) to the next one
    per_date = report.per_date
    dates = list(per_date["date"])
    calendar = engine.panel.calendar
    close = engine.store.wide("close").astype(float).ffill()
    schedule, _funnel = engine.schedule(spec, start="2026-02-01", end=END, freq="M")
    first = sorted(schedule["date"].unique())[0]
    members = schedule.loc[schedule["date"] == first, "symbol"].tolist()
    t0, t1 = int(calendar.searchsorted(dates[0])), int(calendar.searchsorted(dates[1]))
    hand = float((close.iloc[t1][members] / close.iloc[t0][members] - 1.0).mean())
    assert per_date["portfolio"].iloc[0] == pytest.approx(hand, rel=1e-9)
    assert per_date["members"].iloc[0] == len(members) == 2
    assert np.isnan(per_date["turnover"].iloc[0])                # nothing before the first
    assert per_date["turnover"].notna().sum() == report.turnover["rebalances"] \
        == len(per_date) - 1
    assert 0.0 <= report.turnover["mean"] <= 1.0 and report.turnover["members_mean"] == 2
    # the curve ties the statistics together
    curve = report.curve
    assert list(curve.columns) == ["portfolio", "universe", "csi300"]
    assert len(curve) > 0 and curve.index[0] > dates[0]
    assert curve["portfolio"].iloc[-1] - 1.0 == pytest.approx(
        report.stats["portfolio"]["cum_return"])
    # each rebalance row carries the value AT that date's close (1.0 before
    # the first period); the curve runs on to the end of the last one
    for date, value in zip(per_date["date"], per_date["portfolio_cum"]):
        assert value == pytest.approx(float(curve["portfolio"].get(date, 1.0)))
    assert per_date["portfolio_cum"].iloc[0] == 1.0 and per_date["universe_cum"].iloc[0] == 1.0
    assert report.spread["excess_cum"] == pytest.approx(
        curve["portfolio"].iloc[-1] - curve["universe"].iloc[-1])
    assert "excess" in report.summary() and "turnover" in report.summary()
    # the whole passing pool, not the ranked top-N
    pooled = engine.validate(spec, start="2026-02-01", end=END, horizons=(5,), freq="M",
                             apply_top_n=False)
    assert pooled.per_date["members"].iloc[0] > 2                # every passer


def test_validate_notes_a_missing_benchmark_and_runs_without(tmp_path):
    root = _tree(tmp_path / "tree", extra=False)                 # no index file here
    engine = ScreenEngine(CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False))
    spec = ScreenSpec(name="v", filters=[{"name": "cheap", "expr": "pe_ttm > 0"}], top_n=3,
                      universe=UniverseSpec(**OPEN))
    report = engine.validate(spec, start="2026-02-01", end=END, horizons=(5,), freq="M")
    assert set(report.stats) == {"portfolio", "universe"}
    assert any("csi300" in note and "no lxg index file" in note for note in report.notes)
    bare = engine.validate(spec, start="2026-02-01", end=END, horizons=(5,), freq="M",
                           benchmarks=())
    assert set(bare.stats) == {"portfolio", "universe"} and not [
        n for n in bare.notes if "index file" in n]
    # a window too short for the horizons is reported, not crashed on
    short = engine.validate(spec, start=str(cf.CALENDAR[-3].date()), end=END, freq="M")
    assert short.stats == {} and any("widen --end" in note for note in short.notes)


def test_compare_puts_presets_side_by_side(tmp_path):
    root = _tree(tmp_path / "tree", extra=False)
    _index_file(root)
    engine = ScreenEngine(CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False))
    loose = ScreenSpec(name="loose", filters=[{"name": "cheap", "expr": "pe_ttm > 0"}],
                       rank=[{"expr": "pct_rank(earnings_yield)", "weight": 1.0}], top_n=3,
                       universe=UniverseSpec(**OPEN))
    tight = ScreenSpec(name="tight", filters=[{"name": "cheap", "expr": "pe_ttm > 0 and pb < 3"}],
                       rank=[{"expr": "pct_rank(earnings_yield)", "weight": 1.0}], top_n=1,
                       universe=UniverseSpec(**OPEN))
    # a preset on a NARROWER universe: its own baseline is that universe
    main_only = ScreenSpec(name="main_only",
                           filters=[{"name": "cheap", "expr": "pe_ttm > 0"}], top_n=3,
                           universe=UniverseSpec(boards=("main",), exclude_st=False,
                                                 min_listed_days=None))
    table, reports = engine.compare([loose, tight, main_only], start="2026-02-01", end=END,
                                    horizons=(5,), freq="M")
    assert list(table.columns) == ["loose", "tight", "main_only", "universe", "csi300"]
    assert {"members", "turnover", "ann_return", "sharpe", "max_drawdown", "excess_ann",
            "information_ratio", "spread_5d", "hit_5d"} <= set(table.index)
    assert table.loc["members", "loose"] == 3 and table.loc["members", "tight"] == 1
    # the reference column is ONE whole-market benchmark, not the first
    # preset's candidate pool: presets on different boards share it
    market = engine.validate(BENCHMARK_UNIVERSE, start="2026-02-01", end=END,
                             horizons=(5,), freq="M", apply_top_n=False)
    assert table.loc["ann_return", "universe"] == pytest.approx(
        market.stats["portfolio"]["ann_return"])
    assert table.loc["ann_return", "universe"] != pytest.approx(
        reports["main_only"].stats["universe"]["ann_return"])   # the main-board pool
    # every number in a row is measured over the same sessions
    for preset in ("loose", "tight"):
        assert reports[preset].stats["portfolio"]["days"] == \
            reports["loose"].stats["universe"]["days"]
        assert table.loc["ann_return", preset] == pytest.approx(
            reports[preset].stats["portfolio"]["ann_return"])
    assert table.loc["ann_return", "csi300"] == pytest.approx(
        reports["loose"].stats["csi300"]["ann_return"])


# --------------------------------------------------------------- report
def test_daily_report_covers_every_preset(store):
    from qf_screen.presets import list_presets

    engine = ScreenEngine(store)
    specs = [ScreenSpec.from_yaml(name) for name in list_presets()]
    markdown, data = daily_report(engine, specs, asof=END, top=2)
    assert markdown.startswith("# qf-screen daily briefing — 2026-06-30")
    assert "## Overview" in markdown
    assert ("## Data warnings" in markdown) == bool(data["warnings"])
    for spec in specs:
        assert f"## `{spec.name}` — " in markdown and f"| `{spec.name}` |" in markdown
    assert set(data["presets"]) == {spec.name for spec in specs}
    entry = data["presets"]["value_dividend"]
    assert {"members", "industry_mix", "entered", "exited", "notes"} <= set(entry)
    assert sum(entry["industry_mix"].values()) == len(entry["members"]) > 0
    # no earlier session: the overview says so instead of failing
    markdown, data = daily_report(engine, specs[:1], asof=str(cf.CALENDAR[0].date()))
    assert "## Overview" in markdown and set(data["presets"]) == {specs[0].name}


def test_inspect_explains_a_name_that_only_misses_the_top_n(store, tmp_path):
    """Every rule passed and still not a member: the card says where the
    name ranks among the whole passing pool instead of an empty FAIL."""
    from qf_screen.ui.briefing import inspect_text

    preset = tmp_path / "narrow.yaml"
    preset.write_text(
        "name: narrow\n"
        "universe: {boards: [all], exclude_st: false, min_listed_days: 0}\n"
        "filters:\n"
        "  - {name: cheap, expr: \"pe_ttm > 0\"}\n"
        "rank:\n"
        "  - {expr: \"pct_rank(earnings_yield)\", weight: 1.0}\n"
        "top_n: 1\n", encoding="utf-8")
    engine = ScreenEngine(store)
    result = engine.run(ScreenSpec.from_yaml(str(preset)), asof=END)
    left_out = next(s for s in engine.panel.symbols if s not in set(result.members))
    card = engine.inspect(left_out, asof=END, presets=[str(preset)])
    entry = card.presets[0]
    from dataclasses import replace

    pool = list(engine.run(replace(result.spec, top_n=None), asof=END).members)
    assert entry["member"] is False and entry["failed"] == []
    assert entry["passes_rules"] is True and entry["pool"] == len(pool) >= 3
    assert entry["pool_rank"] == pool.index(left_out) + 1 and entry["pool_rank"] > 1
    text = inspect_text(card)
    assert "RANK  narrow" in text and "passes every rule" in text
    assert "FAIL" not in text and card.passed == []
    # the member itself reads PASS with its rank
    member = engine.inspect(result.members[0], asof=END, presets=[str(preset)])
    assert member.passed == ["narrow"] and member.presets[0]["rank"] == 1
