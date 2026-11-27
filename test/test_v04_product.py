"""v0.4 product tests: the execution-realistic validation, user presets and
the watchlist monitor, the self-contained HTML reports, the snapshot
archive and the webhooks, and the cache garbage collector.

Hermetic: every command runs on the synthetic store (``--data-dir``), the
webhook HTTP call is replaced by a recorder, and the user-level config
(``conftest``) points into ``tmp_path``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import conftest as cf
from qf_screen import cache_ops, notify
from qf_screen.analytics import limit_up_mask, simulate
from qf_screen.cli import EXIT_ALERT, EXIT_WEBHOOK, main
from qf_screen.data import CNDataStore, build_universe
from qf_screen.data.table_cache import (
    GC_REASONS, cache_clear, cache_gc_plan, cache_inventory, cache_remove, entry_last_use,
    write_cached_table,
)
from qf_screen.engine import ScreenEngine, ScreenSpec, UniverseSpec

pytestmark = pytest.mark.unit

END = str(cf.END.date())
OPEN = dict(boards=("all",), exclude_st=False, min_listed_days=None)


def _tree(root):
    cf._company_list(root)
    for symbol, spec in cf.SYMBOLS.items():
        cf._symbol_files(root, symbol, spec)
    return root


def _common(store_dir, extra=()):
    return ["--data-dir", str(store_dir), "--workers", "1", *extra]


def _engine(tmp_path):
    root = _tree(tmp_path / "tree")
    store = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False)
    return ScreenEngine(store)


CHEAP = ScreenSpec(
    name="cheap", filters=[{"name": "cheap", "expr": "pe_ttm > 0"}],
    rank=[{"expr": "pct_rank(earnings_yield)", "weight": 1.0}], top_n=2,
    universe=UniverseSpec(**OPEN))


# ============================ Todo 1: fix simulate & the CLI =============
def test_report_accepts_comma_separated_presets(store_dir, tmp_path, capsys):
    """``--presets`` took ``nargs="+"`` only, so ``report --presets a,b``
    failed on a preset literally named ``momentum_trend,value_dividend``."""
    out = tmp_path / "report.md"
    assert main(["report", *_common(store_dir), "--as-of", END, "--out", str(out),
                 "--presets", "value_dividend,momentum_trend"]) == 0
    body = out.read_text(encoding="utf-8")
    sections = re.findall(r"^## `([^`]+)` — ", body, flags=re.M)
    assert sections == ["value_dividend", "momentum_trend"]
    capsys.readouterr()                                   # the "wrote report to" line
    assert "| `quality_growth` |" not in body
    # spaces, commas and a repeat all mean the same list
    for spellings in (["value_dividend", "momentum_trend"],
                      ["value_dividend", "momentum_trend,value_dividend"],
                      [" value_dividend , momentum_trend "]):
        assert main(["report", *_common(store_dir), "--as-of", END, "--format", "json",
                     "--presets", *spellings]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert list(payload["presets"]) == ["value_dividend", "momentum_trend"]


def test_comma_separated_names_on_inspect_and_cache_warm(store_dir, capsys):
    assert main(["inspect", "SH600001", *_common(store_dir), "--as-of", END,
                 "--presets", "value_dividend,momentum_trend", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [card["preset"] for card in payload["presets"]] == ["value_dividend",
                                                               "momentum_trend"]
    assert main(["cache", "warm", *_common(store_dir), "--presets",
                 "value_dividend,momentum_trend"]) == 0
    text = capsys.readouterr().out
    assert "value_dividend" in text and "momentum_trend" in text \
        and "quality_growth" not in text
    with pytest.raises(SystemExit):                      # a bare separator is not a list
        main(["report", *_common(store_dir), "--presets", ","])


# ------------------------------------------------------- simulate(): NaN
def test_simulate_carries_the_last_price_through_a_gap():
    """A member that stops trading mid-period used to turn ``NaN`` the
    cross-sectional mean — and with it every later value of the path."""
    prices = np.array([[10.0, 10.0],
                       [11.0, np.nan],       # B is suspended / delisted
                       [12.0, np.nan],
                       [13.0, np.nan],
                       [14.0, np.nan]])
    path = simulate(prices, [(0, np.array([0, 1]))], [4])
    assert np.isfinite(path.values).all()
    assert np.isfinite(path.periods[["period_return", "cost"]].to_numpy()).all()
    # B is held at its last close (10): the book is (A's growth + 1) / 2
    np.testing.assert_allclose(path.values, [(1.1 + 1) / 2, (1.2 + 1) / 2, (1.3 + 1) / 2,
                                             (1.4 + 1) / 2])
    assert list(path.positions) == [1, 2, 3, 4]
    # the caller's forward-filled table and a raw one give the same path
    filled = pd.DataFrame(prices).ffill().to_numpy()
    np.testing.assert_allclose(simulate(filled, [(0, np.array([0, 1]))], [4]).values,
                               path.values)
    # an exit row past the end of the table costs no more than the rows there are
    assert list(simulate(prices, [(0, np.array([0, 1]))], [99]).positions) == [1, 2, 3, 4]
    # ... and a gap INSIDE the period, with the name trading again after it
    resumed = np.array([[10.0, 10.0], [11.0, np.nan], [12.0, 10.4], [13.0, 10.4]])
    np.testing.assert_allclose(
        simulate(resumed, [(0, np.array([0, 1]))], [3]).values,
        [(1.1 + 1) / 2, (1.2 + 1.04) / 2, (1.3 + 1.04) / 2])


def test_simulate_drops_the_unpriced_and_sits_in_cash_when_nothing_is_left():
    prices = np.array([[10.0, np.nan, 10.0],
                       [11.0, np.nan, 11.0],
                       [12.0, np.nan, 12.0]])
    mixed = simulate(prices, [(0, np.array([0, 1, 2]))], [2])
    assert mixed.periods["members"].tolist() == [2]       # no entry price: not held
    np.testing.assert_allclose(mixed.values, [1.1, 1.2])
    # every name unpriced: a period in cash, not a hole in the path
    cash = simulate(prices, [(0, np.array([1]))], [2])
    assert list(cash.positions) == [1, 2] and cash.values.tolist() == [1.0, 1.0]
    assert cash.periods["members"].tolist() == [0] and cash.periods["period_return"].tolist() == [0.0]
    assert cash.returns().tolist() == [0.0, 0.0]
    assert simulate(prices, [], []).empty


def test_simulate_charges_each_side_of_every_trade():
    prices = np.array([[10.0, 10.0],
                       [11.0, 9.0],
                       [12.0, 8.0],
                       [12.0, 7.0],
                       [12.0, 6.0]])
    holdings, exits = [(0, np.array([0])), (2, np.array([1]))], [2, 4]
    gross = simulate(prices, holdings, exits)
    net = simulate(prices, holdings, exits, cost_bps=1000.0)      # 10 % a side
    assert net.cost_bps == 1000.0 and gross.cost_bps == 0.0
    assert (gross.periods["cost"] == 0.0).all()
    # opening the book pays one side (no row shows it: the chain starts below 1.0);
    # the full replacement at row 2 pays a sell AND a buy; liquidating at the end
    # pays one more side.  The holdings' own return stays the gross one.
    np.testing.assert_allclose(net.values, [0.9 * 1.1, 0.9 * 1.2 * 0.8, 0.9 * 1.2 * 0.8 * 0.875,
                                            0.9 * 1.2 * 0.8 * 0.75 * 0.9])
    np.testing.assert_allclose(net.periods["cost"], [0.1, 0.2])
    np.testing.assert_allclose(net.periods["period_return"], gross.periods["period_return"])
    np.testing.assert_allclose(net.periods["turnover"], [np.nan, 1.0])
    np.testing.assert_allclose(gross.values, [1.1, 1.2, 1.05, 0.9])
    np.testing.assert_allclose(simulate(prices, holdings, exits, cost_bps=0).values,
                               gross.values)


def test_simulate_cost_follows_the_share_of_the_list_traded():
    prices = np.full((6, 3), 10.0)                       # flat: only the costs move the path
    prices[:, 0] = [10, 11, 12, 12, 12, 12]
    # [A, B] -> [B, C]: half the old list is sold, half the new one bought
    path = simulate(prices, [(0, np.array([0, 1])), (2, np.array([1, 2]))], [2, 5],
                    cost_bps=1000.0)
    np.testing.assert_allclose(path.periods["cost"], [0.1, 0.1])     # 0.1 x (1/2 + 1/2)
    # ... into an empty list (a full liquidation), and out of it again
    path = simulate(prices, [(0, np.array([0])), (2, np.array([], dtype=int)),
                             (4, np.array([1]))], [2, 4, 5], cost_bps=1000.0)
    np.testing.assert_allclose(path.periods["cost"], [0.1, 0.1, 0.1])
    np.testing.assert_allclose(path.periods["turnover"], [np.nan, 1.0, 1.0])
    assert path.periods["members"].tolist() == [1, 0, 1]
    assert path.values[1] == path.values[2]                   # the cash rows are flat
    # a book that ends in cash pays no liquidation
    cash_end = simulate(prices, [(0, np.array([0])), (2, np.array([], dtype=int))],
                        [2, 4], cost_bps=1000.0)
    assert cash_end.values[-1] == pytest.approx(0.9 * 1.2 * 0.9)   # open, sell all, nothing left


# ------------------------------------------------------- the limit-up guard
def test_limit_up_mask_reads_the_close_against_the_previous_traded_one():
    raw = np.array([
        [10.00, 10.00, 10.00, 2.04],
        [11.00, 12.00, 15.73, 2.24],      # +10 %, +20 %, +30 %, +9.80 % (the 0.01 round)
        [11.10, 11.90, 15.73, 2.44],      # +0.9 %, -0.8 %, 0, +8.9 % (off the limit)
        [np.nan, np.nan, np.nan, np.nan],  # all suspended
        [11.10, 11.90, 17.30, 2.44]])     # flat vs the previous TRADED close; C +10 %
    mask = limit_up_mask(raw, ["sz_main", "chinext", "bj", "sh_main"])
    assert mask.tolist() == [[False] * 4, [True] * 4, [False] * 4, [False] * 4,
                             [False, False, False, False]]
    # the same +9.9 % day is a limit-up on the main board and none on ChiNext (20 %)
    day = np.array([[10.0, 10.0], [10.99, 10.99]])
    assert limit_up_mask(day, ["sz_main", "chinext"]).tolist() == [[False, False],
                                                                   [True, False]]
    assert limit_up_mask(np.array([[10.0]]), ["sz_main"]).tolist() == [[False]]  # one row
    assert limit_up_mask(np.empty((0, 2)), ["sz_main", "star"]).shape == (0, 2)
    assert limit_up_mask(np.array([[10.0], [11.0]]), ["somewhere_new"]).tolist()[1] == [True]


def _bump_raw_close(engine, symbol, date, factor):
    """Rewrite one raw close in memory: the name closes at ``factor`` times
    its previous close on ``date`` (the adjusted series is left alone)."""
    store = engine.store
    table = store.wide("close_raw")
    row, column = table.index.get_loc(pd.Timestamp(date)), table.columns.get_loc(symbol)
    table.iat[row, column] = float(table.iat[row - 1, column]) * factor


def test_validate_skip_limit_up_refuses_the_entry_and_holds_cash(tmp_path):
    engine = _engine(tmp_path)
    spec = ScreenSpec(name="one", filters=[{"name": "cheap", "expr": "pe_ttm > 0"}],
                      rank=[{"expr": "pct_rank(earnings_yield)", "weight": 1.0}], top_n=1,
                      universe=UniverseSpec(**OPEN))
    window = dict(start="2026-02-01", end=END, horizons=(5,), freq="M")
    schedule, _funnel = engine.schedule(spec, start=window["start"], end=END, freq="M")
    first = schedule["date"].min()
    blocked = schedule.loc[schedule["date"] == first, "symbol"].iloc[0]
    _bump_raw_close(engine, blocked, first, 1.10)         # locked at +10 % on entry day
    plain = engine.validate(spec, **window)
    guarded = engine.validate(spec, **window, skip_limit_up=True)
    assert plain.skipped == {} and "skipped" not in plain.per_date
    assert guarded.skipped == {"entries": 4.0, "skipped": 1.0, "share": 0.25}
    assert guarded.per_date["skipped"].tolist() == [1, 0, 0, 0]
    # the first period: nothing could be bought, so the book sat in cash
    first_row = guarded.per_date.iloc[0]
    assert first_row["portfolio"] == 0.0 and first_row["members"] == 1
    assert plain.per_date["portfolio"].iloc[0] != 0.0
    assert guarded.curve["portfolio"].iloc[0] == 1.0
    # the later periods are the plain run's, shifted onto the flat start
    np.testing.assert_allclose(guarded.per_date["portfolio"].iloc[1:],
                               plain.per_date["portfolio"].iloc[1:])
    # the horizon block measures the screen and is untouched by the guard
    assert guarded.horizons == plain.horizons
    assert "limit-up     skipped 1 of 4 entries (25.0%)" in guarded.summary()
    assert "limit-up" not in plain.summary()


def test_validate_skip_limit_up_keeps_the_other_names(tmp_path):
    engine = _engine(tmp_path)
    window = dict(start="2026-02-01", end=END, horizons=(5,), freq="M")
    schedule, _funnel = engine.schedule(CHEAP, start=window["start"], end=END, freq="M")
    dates = sorted(schedule["date"].unique())
    members = schedule.loc[schedule["date"] == dates[1], "symbol"].tolist()
    assert len(members) == 2
    _bump_raw_close(engine, members[0], dates[1], 1.10)
    guarded = engine.validate(CHEAP, **window, skip_limit_up=True)
    plain = engine.validate(CHEAP, **window)
    calendar, close = engine.panel.calendar, engine.store.wide("close").astype(float)
    t0, t1 = (int(calendar.searchsorted(d)) for d in dates[1:3])
    kept = float(close.iloc[t1][members[1]] / close.iloc[t0][members[1]] - 1.0)
    both = float((close.iloc[t1][members] / close.iloc[t0][members] - 1.0).mean())
    assert guarded.per_date["skipped"].tolist() == [0, 1, 0, 0]
    assert guarded.per_date["portfolio"].iloc[1] == pytest.approx(kept, rel=1e-9)
    assert plain.per_date["portfolio"].iloc[1] == pytest.approx(both, rel=1e-9)
    assert guarded.per_date["portfolio"].iloc[1] != pytest.approx(both)


# --------------------------------------------------------- transaction costs
def test_validate_reports_the_net_book_next_to_the_gross_one(tmp_path):
    engine = _engine(tmp_path)
    window = dict(start="2026-02-01", end=END, horizons=(5,), freq="M")
    plain = engine.validate(CHEAP, **window)
    net = engine.validate(CHEAP, **window, cost_bps=15.0)
    assert plain.cost_bps == 0.0 and plain.net == {} and "portfolio_net" not in plain.stats
    assert "cost" not in plain.per_date and list(plain.curve.columns) == ["portfolio", "universe"]
    assert net.cost_bps == 15.0 and net.horizons == plain.horizons     # the screen, gross
    assert list(net.curve.columns) == ["portfolio", "universe", "portfolio_net"]
    gross, after = net.stats["portfolio"], net.stats["portfolio_net"]
    assert gross == plain.stats["portfolio"] and net.stats["universe"] == plain.stats["universe"]
    assert after["cum_return"] < gross["cum_return"] and after["ann_return"] < gross["ann_return"]
    assert after["sharpe"] < gross["sharpe"]
    assert after["max_drawdown"] <= gross["max_drawdown"] + 1e-12   # never shallower
    assert after["days"] == gross["days"]
    assert net.curve["portfolio_net"].iloc[-1] - 1.0 == pytest.approx(after["cum_return"])
    assert net.net["cost_drag_ann"] == pytest.approx(gross["ann_return"] - after["ann_return"])
    assert net.net["cost_drag_ann"] > 0 and net.net["excess_ann"] < net.spread["excess_ann"]
    # what each rebalance paid: one side to open, then a sell and a buy per name replaced
    cost = net.per_date["cost"].to_numpy()
    assert cost[0] == pytest.approx(15e-4)
    np.testing.assert_allclose(cost[1:], 15e-4 * 2 * net.per_date["turnover"].iloc[1:])
    for date, value in zip(net.per_date["date"], net.per_date["portfolio_net_cum"]):
        assert value == pytest.approx(float(net.curve["portfolio_net"].get(date, 1.0)))
    assert "portfolio_net" in net.summary() and "excess_net" in net.summary() \
        and "costs        15 bps per side" in net.summary()
    assert "portfolio_net" not in plain.summary()


def test_compare_lists_the_net_rows_under_their_gross_twins(tmp_path):
    engine = _engine(tmp_path)
    wide = ScreenSpec(name="wide", filters=[{"name": "cheap", "expr": "pe_ttm > 0"}],
                      rank=[{"expr": "pct_rank(earnings_yield)", "weight": 1.0}], top_n=3,
                      universe=UniverseSpec(**OPEN))
    window = dict(start="2026-02-01", end=END, horizons=(5,), freq="M")
    table, reports = engine.compare([CHEAP, wide], **window, cost_bps=15.0)
    metrics = list(table.index)
    for metric in ("ann_return", "sharpe", "max_drawdown", "cum_return", "excess_ann",
                   "information_ratio"):
        assert metrics.index(metric) + 1 == metrics.index(f"{metric}_net"), metric
    for spec in (CHEAP, wide):
        report = reports[spec.name]
        assert table.loc["ann_return_net", spec.name] == pytest.approx(
            report.stats["portfolio_net"]["ann_return"])
        assert table.loc["excess_ann_net", spec.name] == pytest.approx(report.net["excess_ann"])
        assert table.loc["ann_return_net", spec.name] < table.loc["ann_return", spec.name]
    for reference in ("universe",):                   # the baselines stay gross
        assert np.isnan(table.loc["ann_return_net", reference])
    gross, _reports = engine.compare([CHEAP, wide], **window)
    assert not [metric for metric in gross.index if str(metric).endswith("_net")]
    pd.testing.assert_series_equal(table.loc[gross.index[0]], gross.loc[gross.index[0]])


def test_validate_and_compare_cli_flags(tmp_path, capsys):
    root = _tree(tmp_path / "tree")
    base = _common(root)
    window = ["--start", "2026-02-01", "--end", END, "--horizons", "5", "--benchmarks", ""]
    assert main(["validate", "momentum_trend", *base, *window, "--cost-bps", "15",
                 "--skip-limit-up"]) == 0
    text = capsys.readouterr().out
    for needle in ("15 bps cost per side", "limit-up entries skipped", "portfolio_net",
                   "excess_net", "costs        15 bps", "limit-up     skipped"):
        assert needle in text, needle
    out = tmp_path / "v.json"
    assert main(["validate", "momentum_trend", *base, *window, "--format", "json",
                 "--out", str(out), "--cost-bps", "15", "--skip-limit-up"]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["cost_bps"] == 15.0 and set(payload["skipped"]) == {"entries", "skipped",
                                                                        "share"}
    assert "portfolio_net" in payload["stats"] and "cost_drag_ann" in payload["net"]
    assert "portfolio_net" in payload["curve"] and payload["per_date"][0]["skipped"] == "0"
    assert main(["compare", "value_dividend", "momentum_trend", *base, *window,
                 "--cost-bps", "15"]) == 0
    text = capsys.readouterr().out
    assert "15 bps cost per side" in text and "ann_return_net" in text \
        and "sharpe_net" in text and "*_net" in text
    assert main(["compare", "value_dividend", "momentum_trend", *base, *window,
                 "--format", "md", "--out", str(tmp_path / "c.md"), "--cost-bps", "15"]) == 0
    assert "ann_return_net" in (tmp_path / "c.md").read_text(encoding="utf-8")
    for bad in ("-1", "abc", "10000"):
        with pytest.raises(SystemExit):
            main(["validate", "momentum_trend", *base, *window, "--cost-bps", bad])


# ==================== Todo 2: user presets & the watchlist monitor ==========
LOOSE_YAML = ("name: {name}\n"
              "universe: {{boards: [all], exclude_st: false, min_listed_days: 0}}\n"
              "filters:\n"
              "  - {{name: cheap, expr: \"pe_ttm > 0\"}}\n"
              "rank:\n"
              "  - {{expr: \"pct_rank(earnings_yield)\", weight: 1.0}}\n"
              "top_n: {top}\n")


def _user_presets():
    from qf_screen.presets import user_preset_dir

    user_preset_dir().mkdir(parents=True, exist_ok=True)
    return user_preset_dir()


def test_user_presets_extend_and_override_the_built_ins(store_dir, tmp_path, capsys):
    """Every ``*.yaml`` in ``$QF_SCREEN_PRESETS_DIR`` is a preset named by
    its file stem; one named like a built-in replaces it."""
    from qf_screen.presets import (
        PRESET_DIR, built_in_presets, list_presets, preset_origin, preset_path,
    )

    directory = _user_presets()
    mine = directory / "my_screen.yaml"
    mine.write_text(LOOSE_YAML.format(name="my_screen", top=2), encoding="utf-8")
    override = directory / "value_dividend.yaml"
    override.write_text(LOOSE_YAML.format(name="value_dividend", top=1), encoding="utf-8")
    (directory / "notes.txt").write_text("not a preset", encoding="utf-8")
    assert set(list_presets()) == set(built_in_presets()) | {"my_screen"}
    assert preset_path("my_screen") == mine
    assert preset_path("value_dividend") == override            # the user file wins
    assert preset_path("momentum_trend") == PRESET_DIR / "momentum_trend.yaml"
    assert preset_path(str(mine)) == mine                       # a path is still a path
    assert preset_origin("momentum_trend")["kind"] == "built-in"
    assert preset_origin("my_screen") == {"kind": "user", "path": mine}
    assert preset_origin("value_dividend") == {"kind": "override", "path": override}
    assert preset_origin("no_such_preset") is None
    spec = ScreenSpec.from_yaml("value_dividend")
    assert spec.top_n == 1 and spec.source == str(override)
    with pytest.raises(SystemExit) as exc:
        main(["run", "no_such_preset", *_common(store_dir), "--as-of", END])
    assert "my_screen" in str(exc.value) and str(directory) in str(exc.value)

    # every command that takes a preset name resolves it the same way
    base = _common(store_dir) + ["--as-of", END]
    window = ["--start", "2026-03-01", "--end", END]
    assert main(["run", "my_screen", *base, "--format", "csv"]) == 0
    assert "my_screen: 2 members as of" in capsys.readouterr().out
    assert main(["run", "value_dividend", *base, "--format", "csv"]) == 0
    assert "value_dividend: 1 members as of" in capsys.readouterr().out   # the override
    assert main(["diff", "my_screen", *base]) == 0 and "my_screen:" in capsys.readouterr().out
    assert main(["inspect", "SH600001", *base, "--format", "json"]) == 0
    assert "my_screen" in {card["preset"] for card in json.loads(capsys.readouterr().out)["presets"]}
    assert main(["report", *base, "--format", "json"]) == 0
    assert "my_screen" in json.loads(capsys.readouterr().out)["presets"]
    assert main(["pit", "my_screen", *_common(store_dir), *window]) == 0
    assert main(["validate", "my_screen", *_common(store_dir), *window, "--horizons", "5"]) == 0
    assert main(["compare", "my_screen", "momentum_trend", *_common(store_dir), *window,
                 "--horizons", "5"]) == 0
    assert main(["cache", "warm", *_common(store_dir), "--presets", "my_screen"]) == 0
    assert "my_screen" in capsys.readouterr().out

    # the listing says where each one comes from
    assert main(["list"]) == 0
    text = capsys.readouterr().out
    assert "(top_n 2; user: " + str(mine) + ")" in text
    assert "(top_n 1; user preset, REPLACES the built-in: " + str(override) + ")" in text
    assert "(top_n 30; built-in)" in text and str(directory) in text


def test_a_broken_user_preset_is_listed_with_its_reason(store_dir, capsys):
    directory = _user_presets()
    good = directory / "good.yaml"
    good.write_text(LOOSE_YAML.format(name="good", top=2), encoding="utf-8")
    bad = directory / "bad.yaml"
    bad.write_text("name: bad\nfilters:\n  - {name: a, expr: \"pe_ttm > 0\"}\nrank:\n"
                   "  - {weigth: 2}\n", encoding="utf-8")
    assert main(["list"]) == 1                          # shown, and flagged
    text = capsys.readouterr().out
    assert re.search(r"^bad\s+DOES NOT LOAD: .*rank\[0\]", text, flags=re.M), text
    assert "good" in text and "momentum_trend" in text  # the rest is still listed
    with pytest.raises(SystemExit) as exc:
        main(["run", "bad", *_common(store_dir), "--as-of", END])
    assert "rank[0]" in str(exc.value) and str(bad) in str(exc.value)


def test_preset_loader_rejects_what_would_silently_no_op(tmp_path):
    from qf_screen.presets import PresetError, read_preset

    good = ("name: t\nuniverse: {boards: [main], exclude_st: true, min_listed_days: 0}\n"
            "filters:\n  - {name: a, expr: \"pe_ttm > 0\", description: why}\n"
            "rank:\n  - {expr: \"pct_rank(earnings_yield)\", weight: 0.5}\ntop_n: 0\n")
    path = tmp_path / "t.yaml"

    def load(body):
        path.write_text(body, encoding="utf-8")
        return read_preset(path)

    assert load(good)["top_n"] == 0                    # the shipped contract still holds
    for body, needle in (
            (good + "extra: 1\n", "unknown key(s) ['extra'] in the preset"),
            (good.replace("min_listed_days: 0", "min_listed_days: 0, st_bandd: true"),
             "unknown key(s) ['st_bandd'] in universe"),
            (good.replace("boards: [main]", "boards: [mainstreet]"), "unknown board"),
            (good.replace("description: why", "description: why, na_pas: true"),
             "unknown key(s) ['na_pas'] in filters[0]"),
            (good.replace("description: why", "na_pass: \"please\""),
             "na_pass must be true or false"),
            (good.replace("min_listed_days: 0", "min_listed_days: \"a year\""),
             "min_listed_days must be 0 or a positive integer"),
            (good.replace("boards: [main]", "boards: main"), "boards must be a list"),
            (good.replace("weight: 0.5", "weigth: 0.5"),
             "unknown key(s) ['weigth'] in rank[0]"),
            (good.replace("weight: 0.5", "weight: 0"), "weight must be a positive number"),
            (good.replace("top_n: 0", "top_n: -3"), "top_n must be 0 or a positive integer"),
            (good.replace("top_n: 0", "top_n: 2\nmax_per_industry: x"),
             "max_per_industry must be a positive integer"),
            ("name: t\nfilters:\n  - {name: a, expr: \"pe_ttm > 0\"}\nrank: [{weight: 1}]\n",
             "rank[0] must be a mapping with an 'expr' string"),
            ("name: t\nfilters: []\n", "'filters' must be a non-empty list"),
            ("- a\n- b\n", "must be a mapping, got list"),
            ("name: t\nfilters: [\n", "unreadable YAML")):
        with pytest.raises(PresetError) as exc:
            load(body)
        assert needle in str(exc.value), (body, needle, str(exc.value))
    # every built-in preset loads under the stricter rules (including the
    # family-folder members, one directory deep)
    from qf_screen.presets import built_in_presets, preset_path, preset_text

    for name in built_in_presets():
        shipped = preset_path(name)
        doc = read_preset(shipped)
        copy = tmp_path / f"copy_{shipped.stem}.yaml"
        copy.write_text(preset_text("copy_of_it", doc), encoding="utf-8")
        again = read_preset(copy)
        assert again["filters"] == doc["filters"] and again["rank"] == doc["rank"]
        assert again["universe"] == doc["universe"]
        assert again["top_n"] == doc.get("top_n")
        assert again.get("max_per_industry") == doc.get("max_per_industry")
        assert " ".join(str(again["description"]).split()) \
            == " ".join(str(doc.get("description", "")).split())


def test_init_preset_scaffolds_a_documented_screen(store_dir, capsys):
    from qf_screen.presets import PRESET_DIR, list_presets

    directory = _user_presets()
    assert main(["init-preset", "my_screen"]) == 0
    text = capsys.readouterr().out
    path = directory / "my_screen.yaml"
    assert f"wrote {path}" in text and "qf-screen run my_screen --top 10" in text
    body = path.read_text(encoding="utf-8")
    assert body.startswith("# qf_screen preset -- ONE screen")
    for key in ("#   qf-screen run my_screen --top 10 --explain SH600519",
                "#   qf-screen validate my_screen", "name: my_screen", "description: >",
                "universe:", "boards: [main]", "exclude_st: true", "min_listed_days: 365",
                "# st_band: true", "# symbols:", "filters:", "amount_ma20 >= 1e8",
                "na_pass: true", "rank:", "pct_rank(earnings_yield)", "weight: 1.0",
                "top_n: 30", "# max_per_industry: 5"):
        assert key in body, key
    spec = ScreenSpec.from_yaml("my_screen")            # discovered by name ...
    assert spec.name == "my_screen" and spec.top_n == 30 and spec.universe.boards == ("main",)
    assert spec.filters[0]["name"] == "traded"          # ... and the hygiene block is prepended
    assert "my_screen" in list_presets()
    assert main(["run", "my_screen", *_common(store_dir), "--as-of", END]) == 0
    assert "my_screen:" in capsys.readouterr().out

    # never overwritten behind the user's back
    path.write_text("name: my_screen\n# my edits\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["init-preset", "my_screen"])
    assert "--force" in str(exc.value) and "my edits" in path.read_text(encoding="utf-8")
    assert main(["init-preset", "my_screen.yaml", "--force"]) == 0      # a suffix is dropped
    assert "name: my_screen\n" in path.read_text(encoding="utf-8")
    capsys.readouterr()

    # --from copies another preset's screen under the new name
    assert main(["init-preset", "fast_trend", "--from", "momentum_trend"]) == 0
    copy = ScreenSpec.from_yaml(str(directory / "fast_trend.yaml"))
    original = ScreenSpec.from_yaml(str(PRESET_DIR / "momentum_trend.yaml"))
    assert copy.name == "fast_trend" and copy.source == str(directory / "fast_trend.yaml")
    assert [f["expr"] for f in copy.filters] == [f["expr"] for f in original.filters]
    own = lambda spec: [f for f in spec.filters if not f.get("auto")]   # noqa: E731
    assert own(copy) == own(original)           # the file carries the preset's own rules
    assert [f["name"] for f in copy.filters][:3] == ["traded", "price_floor", "liquidity"]
    assert copy.rank == original.rank and copy.top_n == original.top_n == 30
    assert copy.universe == original.universe
    assert " ".join(copy.description.split()) == " ".join(original.description.split())
    assert main(["run", "fast_trend", *_common(store_dir), "--as-of", END]) == 0
    assert "fast_trend:" in capsys.readouterr().out

    # --out writes elsewhere: runnable by path, not discovered by name
    elsewhere = store_dir.parent / "elsewhere" / "byo.yaml"
    assert main(["init-preset", "byo", "--out", str(elsewhere)]) == 0
    assert elsewhere.exists() and "byo" not in list_presets()
    assert ScreenSpec.from_yaml(str(elsewhere)).name == "byo"

    # a built-in's name is an override, and the command says so
    assert main(["init-preset", "value_dividend", "--out", str(store_dir.parent / "vd.yaml")]) == 0
    assert "REPLACES the built-in value_dividend" in capsys.readouterr().out
    for bad in ("a/b", "..", "two words", "x,y"):
        with pytest.raises(SystemExit) as exc:
            main(["init-preset", bad, "--out", str(store_dir.parent / "x.yaml")])
        assert "not a preset name" in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        main(["init-preset", "ok", "--out", str(PRESET_DIR / "ok.yaml")])
    assert "part of the package" in str(exc.value)


# ------------------------------------------------------------- watch
def _risk_tree(tmp_path, pledge=0.35, names_asof="2026-06-01"):
    """The synthetic store with one planted fact per risk rule: the
    company list dated INSIDE the calendar (so the ST flag is in force),
    and ``SH600001``'s pledge ratio where the rule can see it."""
    root = _tree(tmp_path / "tree")
    cf._write_csv(root / "cn/equity/SH600001/lxg/pledge.csv", pd.DataFrame({
        "date": cf.CALENDAR[-10:].strftime("%Y-%m-%d"), "pledgeRatio": [pledge] * 10}))
    listing = root / "cn/lxg/company_list.csv"
    stamp = pd.Timestamp(names_asof).timestamp()
    os.utime(listing, (stamp, stamp))
    return root


def _expected(engine, thresholds):
    """The documented rules, written out independently of the monitor."""
    panel, pos = engine.panel, len(engine.panel.calendar) - 1
    values = {name: panel.cross_section(name, pos) for name in (
        "st_flag", "pledge_ratio", "ma_ratio_60", "ma_ratio_200", "amount_ma20", "close_raw")}

    def rules(symbol):
        at = panel.symbols.get_loc(symbol)
        value = {name: column[at] for name, column in values.items()}
        fired = set()
        if value["st_flag"]:
            fired.add("st")
        if value["pledge_ratio"] >= thresholds["pledge"]:
            fired.add("pledge")
        if value["ma_ratio_60"] < 1 or value["ma_ratio_200"] < 1:
            fired.add("trend_break")
        if value["amount_ma20"] < thresholds["liquidity"]:
            fired.add("liquidity")
        if not value["close_raw"] > 0:
            fired.add("suspended")
        return fired
    return rules


def _risk_engine(tmp_path):
    return ScreenEngine(CNDataStore(_risk_tree(tmp_path), warmup_rows=cf.SESSIONS,
                                    use_cache=False))


WATCHED = ["SH600003", "SH600001", "600005", "SZ000002", "BJ430004", "NOPE1"]


def test_watch_fires_each_risk_rule_and_orders_the_names(tmp_path):
    from qf_screen.ui.watch import THRESHOLDS, watch

    engine = _risk_engine(tmp_path)
    assert str(engine.store.universe.names_asof.date()) == "2026-06-01"
    reference = _expected(engine, THRESHOLDS)
    report = watch(engine, WATCHED, asof=END)
    assert report.asof == cf.END and report.preset is None
    fired = {symbol: set(group["rule"]) for symbol, group in report.alerts.groupby("symbol")}
    for symbol in ("SH600001", "SH600005", "SZ000002", "BJ430004", "SH600003"):
        assert fired.get(symbol, set()) == reference(symbol), symbol
    assert fired["NOPE1"] == {"unknown_symbol"}                 # 600005 -> SH600005
    assert fired["SH600005"] == {"st", "trend_break"}           # a high and a medium alert
    assert fired["SH600001"] == {"pledge"} and "BJ430004" not in fired
    assert report.count == len(report.alerts) == int(report.rows["alerts"].sum())
    expected_medium = sum("trend_break" in reference(symbol) for symbol in engine.panel.symbols)
    assert report.by_severity() == {"high": 3, "medium": expected_medium, "low": 0}
    assert expected_medium >= 2
    # urgent first: every high before every medium before the quiet name; within a
    # severity the name with more alerts first, then by symbol
    rank = {"high": 0, "medium": 1, "-": 2}
    keys = [(rank[row.severity], -row.alerts, row.symbol) for row in report.rows.itertuples()]
    assert keys == sorted(keys) and keys[0][:2] == (0, -2)
    assert list(report.rows["symbol"]) == ["SH600005", "NOPE1", "SH600001", "SH600003",
                                            "SZ000002", "BJ430004"]
    assert [rule["name"] for rule in report.rules] == ["st", "pledge", "trend_break",
                                                       "liquidity", "suspended"]
    assert all(rule["source"] == "built-in" for rule in report.rules)
    messages = {(row.symbol, row.rule): row.message for row in report.alerts.itertuples()}
    assert messages["SH600001", "pledge"] == "pledge ratio 35.0% at or above 30%"
    assert messages["SH600005", "st"] == "ST/*ST risk-warning name in the company-list snapshot"
    assert re.fullmatch(r"close below its (60|200)-day( or (60|200)-day)? mean "
                        r"\(ma_ratio_\d+ [\d.]+%(, ma_ratio_\d+ [\d.]+%)?\)",
                        messages["SH600005", "trend_break"])
    assert messages["NOPE1", "unknown_symbol"].startswith("not in the loaded universe")
    quiet = report.rows[report.rows["symbol"] == "BJ430004"].iloc[0]
    assert quiet["alerts"] == 0 and quiet["severity"] == "-" and quiet["detail"] == ""
    assert quiet["board"] == "北交所" and quiet["name"] == "公司430004"
    assert quiet["industry"] == "食品饮料" and quiet["close_raw"] > 0 and quiet["ret_21d"] > 0
    # the alerts frame carries what each rule read
    assert list(report.alerts.columns) == ["symbol", "rule", "severity", "message", "expr"]
    pledge = report.alerts.set_index(["symbol", "rule"]).loc["SH600001", "pledge"]
    assert pledge["expr"] == "pledge_ratio >= 0.3" and pledge["severity"] == "high"
    # a missing value never alerts: the ak-only name has no pledge file at all
    assert np.isnan(engine.panel.cross_section(
        "pledge_ratio", len(engine.panel.calendar) - 1)[engine.panel.symbols.get_loc("SH600003")])


def test_watch_flags_a_name_with_no_close_on_the_decision_session(tmp_path):
    """A suspended holding is the one case where ABSENT data must alert:
    its moving averages freeze and its liquidity dries up without ever
    crossing a threshold, so every comparison-based rule stays quiet."""
    from qf_screen.ui.watch import watch

    engine = _risk_engine(tmp_path)
    before = watch(engine, ["BJ430004"], asof=END)
    assert before.count == 0                                  # a healthy name
    table = engine.panel.table("close_raw")            # the copy the rules read
    table.iat[len(table) - 1, table.columns.get_loc("BJ430004")] = np.nan
    report = watch(engine, ["BJ430004"], asof=END)
    assert [(row.rule, row.severity) for row in report.alerts.itertuples()] == [
        ("suspended", "high")]
    assert report.alerts.message.iloc[0] == ("no close on the as-of session (suspended, "
                                             "halted or delisted)")
    assert report.rows["severity"].iloc[0] == "high" and np.isnan(report.rows["close_raw"].iloc[0])
    assert "close below" not in " ".join(report.alerts.message)   # frozen MAs: no false trend alert


def test_watch_thresholds_disable_and_custom_rules(tmp_path):
    from qf_screen.ui.watch import WatchRule, watch, watch_markdown, watch_payload, watch_text

    engine = ScreenEngine(CNDataStore(_risk_tree(tmp_path), warmup_rows=cf.SESSIONS,
                                      use_cache=False))
    watched = ["SH600001", "SH600003", "SH600005", "SZ000002", "BJ430004"]
    fired = lambda report: {symbol: set(group["rule"])     # noqa: E731
                            for symbol, group in report.alerts.groupby("symbol")}
    default = watch(engine, watched)
    tighter = {"pledge": 0.40, "liquidity": 1e8}
    report = watch(engine, watched, thresholds=tighter)
    assert fired(report) == {symbol: rules for symbol, rules in _expected_all(engine, tighter).items()
                             if rules}
    assert "pledge" not in fired(report).get("SH600001", set())         # 35 % is under 40 %
    assert "liquidity" in fired(report)["SH600005"]                     # 0.7亿 is under 1亿
    assert "liquidity" not in fired(default).get("SH600005", set())
    message = report.alerts.query("symbol == 'SH600005' and rule == 'liquidity'").message.iloc[0]
    assert message == "20d mean traded value 0.7亿, under the 1.0亿 floor"
    assert "pledge_ratio >= 0.4" in [rule["expr"] for rule in report.rules]
    # switch built-ins off; add a custom rule (DSL, true = alert)
    panel, pos = engine.panel, len(engine.panel.calendar) - 1
    ratio = panel.cross_section("high252_ratio", pos)
    at = {symbol: panel.symbols.get_loc(symbol) for symbol in watched}
    # a level the data crosses: below the lowest watched name's ratio ... no; between
    # the lowest and the next one, so exactly one name trips it
    ordered = sorted(float(ratio[at[symbol]]) for symbol in watched)
    level = (ordered[0] + ordered[1]) / 2
    fallen = WatchRule("fallen", f"high252_ratio < {level:.4f}", "well below its 52w high",
                       "high")
    report = watch(engine, watched, rules=[fallen], disable=["pledge", "liquidity"])
    tripped = [symbol for symbol in watched if ratio[at[symbol]] < level]
    names = [rule["name"] for rule in report.rules]
    assert names == ["st", "trend_break", "suspended", "fallen"]
    assert [rule["source"] for rule in report.rules] == ["built-in"] * 3 + ["custom"]
    assert tripped == [min(watched, key=lambda symbol: float(ratio[at[symbol]]))]
    for symbol in watched:
        assert (("fallen" in fired(report).get(symbol, set())) == (symbol in tripped)), symbol
        assert "pledge" not in fired(report).get(symbol, set())
        assert "liquidity" not in fired(report).get(symbol, set())
    # the views
    text = watch_text(report)
    assert text.startswith(f"watch: 5 symbols as of {END} -- ")
    assert "rules in force: st, trend_break, suspended, fallen" in text
    assert re.search(r"^    HIGH\s+fallen\s+well below its 52w high "
                     r"\(high252_ratio [\d.]+%\)$", text, flags=re.M), text
    assert "    ok" in text and "SH600005  *ST宏达  沪市主板 · 食品饮料  close " in text
    markdown = watch_markdown(report)
    assert markdown.startswith(f"## watch — {END} · 5 symbols")
    assert "| symbol | name | industry | close | 21d | alerts | detail |" in markdown
    assert "| `SH600005` | *ST宏达 | 食品饮料 |" in markdown
    payload = watch_payload(report)
    assert {"asof", "symbols", "alerts", "by_severity", "rules", "rows", "detail", "warnings"} \
        <= set(payload)
    assert payload["symbols"] == 5 and payload["alerts"] == report.count
    assert payload["preset"] is None and len(payload["rows"]) == 5
    json.dumps(payload)                                                # reportable
    for keyword, bad, needle in (
            ("disable", ["nope", "st"], "cannot disable ['nope']"),
            ("disable", "pledge", "cannot disable ['p', 'l', 'e', 'd', 'g', 'e']"),
            ("rules", [WatchRule("x", "pe_ttm", "d")], "rule 'x': 'pe_ttm' must be boolean"),
            ("rules", [WatchRule("y", "pe > 0", "d")], "rule 'y' references unknown feature(s)"),
            ("rules", [WatchRule("z", "no_such_feature > 1", "d")], "unknown feature(s)"),
            ("rules", [WatchRule("s", "pe_ttm > 0", "d", "urgent")],
             "rule 's': severity must be one of"),
            ("thresholds", {"nope": 1}, "no built-in rule takes a threshold")):
        with pytest.raises(ValueError) as exc:
            watch(engine, watched, **{keyword: bad})
        assert needle in str(exc.value), (keyword, str(exc.value))
    with pytest.raises(ValueError) as exc:
        watch(engine, [])
    assert "no symbols to watch" in str(exc.value)


def _expected_all(engine, thresholds):
    reference = _expected(engine, thresholds)
    return {symbol: reference(symbol) for symbol in engine.panel.symbols}


def test_watch_preset_exit_names_the_rule_or_the_rank(tmp_path):
    from qf_screen.ui.watch import watch

    engine = _engine(tmp_path)
    every = ScreenSpec(name="every", filters=[{"name": "cheap", "expr": "pe_ttm > 0"}],
                       rank=[{"expr": "pct_rank(earnings_yield)", "weight": 1.0}], top_n=2,
                       universe=UniverseSpec(**OPEN))
    report = watch(engine, list(engine.panel.symbols), preset=every, asof=END)
    assert report.preset == "every" and report.rules[-1] == {
        "name": "preset_exit", "expr": "", "severity": "medium",
        "description": "no longer a member of every", "source": "preset"}
    result = engine.run(every, asof=END)
    exits = report.alerts[report.alerts["rule"] == "preset_exit"]
    assert set(exits["symbol"]) == set(engine.panel.symbols) - set(result.members)
    for row in exits.itertuples():
        assert row.severity == "medium"
        # every name passes the one rule: the truncation alone kept it out
        assert re.fullmatch(r"out of every: passes every rule, ranks [3-5] of 5 passers "
                            r"\(top 2 kept\)", row.message), row.message
    assert "preset_exit" not in set(report.alerts[report.alerts["symbol"].isin(
        result.members)]["rule"])
    # a name the preset's own rules reject is told which ones, with what they read
    report = watch(engine, ["SH600005", "SH600001"], preset="momentum_trend", asof=END)
    exits = report.alerts[(report.alerts["symbol"] == "SH600005")
                          & (report.alerts["rule"] == "preset_exit")]
    message = exits.message.iloc[0]
    assert message.startswith("out of momentum_trend: fails ")
    assert "price_floor (close_raw 1.68)" in message and "liquid (amount_ma20 0.7亿)" in message
    # the industry cap says so too
    capped = ScreenSpec(name="capped", filters=[{"name": "cheap", "expr": "pe_ttm > 0"}],
                        rank=[{"expr": "pct_rank(earnings_yield)", "weight": 1.0}], top_n=3,
                        max_per_industry=1, universe=UniverseSpec(**OPEN))
    run = engine.run(capped, asof=END)
    assert len(run.capped) >= 1                        # every lxg name shares one industry
    report = watch(engine, list(engine.panel.symbols), preset=capped, asof=END)
    displaced = report.alerts[report.alerts["symbol"].isin(run.capped)]
    assert len(displaced) == len(run.capped)
    for row in displaced.itertuples():
        assert "passes every rule, displaced by the max_per_industry 1 cap, ranks " \
            in row.message and " passers (top 3 kept)" in row.message


def test_watchlist_file_parsing(tmp_path):
    from qf_screen.ui.watch import load_watchlist, parse_watchlist

    book = ("# my book\n"
            "name: book\n"
            "preset: value_dividend\n"
            "symbols: [SH600519, SZ000858]\n"
            "rules:\n"
            "  - {name: fallen, expr: \"high252_ratio < 0.60\", severity: high}\n"
            "  - {name: too_hot, expr: \"ret_21d > 0.30\", description: ran too far}\n"
            "disable: [pledge, liquidity]\n"
            "thresholds: {pledge: 0.20}\n")
    listed = parse_watchlist(book, "book.yaml")
    assert listed.symbols == ["SH600519", "SZ000858"] and listed.preset == "value_dividend"
    assert listed.name == "book" and listed.source == "book.yaml"
    assert [rule.name for rule in listed.rules] == ["fallen", "too_hot"]
    assert listed.rules[0].severity == "high" and listed.rules[0].source == "custom"
    assert listed.rules[1].severity == "medium" and listed.rules[1].description == "ran too far"
    assert listed.disable == ("pledge", "liquidity") and listed.thresholds == {"pledge": 0.20}
    # a plain list, one symbol per line, comments and commas -- read as TEXT,
    # so a code keeps its leading zeros
    for text in ("SH600519\n000858, SZ000002  # two more\n\n",
                 "- SH600519\n- '000858'\n",
                 "SH600519 SZ000858,SH600036 # my book",
                 "[SH600519, 000858]"):
        assert parse_watchlist(text).symbols == (["SH600519", "000858", "SZ000002"]
                                                 if "SZ000002" in text else
                                                 ["SH600519"] + (["SZ000858", "SH600036"]
                                                                 if "SH600036" in text
                                                                 else ["000858"])), text
        assert parse_watchlist(text).preset is None and parse_watchlist(text).rules == ()
    assert parse_watchlist("symbols: SH600519, SZ000858\n").symbols == ["SH600519", "SZ000858"]
    path = tmp_path / "book.yaml"
    path.write_text(book, encoding="utf-8")
    assert load_watchlist(path).preset == "value_dividend"
    (tmp_path / "book.txt").write_text("SH600519\nSZ000858\n", encoding="utf-8")
    assert load_watchlist(tmp_path / "book.txt").symbols == ["SH600519", "SZ000858"]
    for body, needle in (
            ("", "no symbols"),
            ("symbols: []\n", "no symbols"),
            ("preset: x\n", "no 'symbols'"),
            ("symbols: [A]\nwatches: 3\n", "unknown key(s) ['watches']"),
            ("symbols: [A]\nrules:\n  - {expr: \"a > 1\"}\n", "a 'name' and an 'expr' string"),
            ("symbols: [A]\nrules:\n  - {name: st, expr: \"a > 1\"}\n",
             "'st' is empty, a built-in rule or used twice"),
            ("symbols: [A]\nrules:\n  - {name: x, expr: \"a > 1\", sevirty: high}\n",
             "unknown key(s) ['sevirty'] in rules[0]"),
            ("symbols: [A]\nrules:\n  - {name: x, expr: \"a > 1\", severity: urgent}\n",
             "severity must be one of high, medium, low"),
            ("symbols: [A]\ndisable: [nope]\n", "cannot disable ['nope']"),
            ("symbols: [A]\nthresholds: {trend: 1}\n", "no built-in rule takes a threshold 'trend'"),
            ("symbols: [A]\nthresholds: {pledge: lots}\n", "thresholds.pledge must be a number"),
            ("symbols: [A]\nthresholds: {liquidity: .inf}\n", "thresholds.liquidity must be a number")):
        with pytest.raises(ValueError) as exc:
            parse_watchlist(body, "w")
        assert needle in str(exc.value), (body, str(exc.value))
    with pytest.raises(ValueError) as exc:
        load_watchlist(tmp_path / "absent.yaml")
    assert "cannot read the watchlist" in str(exc.value)


def test_the_package_root_hands_out_the_same_name_before_and_after_its_module_loads():
    """The root exports its names lazily.  `qf_screen.watch` is a FUNCTION
    -- and, while its module sat at the package root as `watch.py`, also
    the name of that module: the moment anything imported the module (the
    CLI does; so does reading `qf_screen.WatchReport`) the attribute was
    the module, and the documented `qf_screen.watch(engine, ...)` failed
    with "'module' object is not callable"."""
    import importlib

    import qf_screen

    assert set(qf_screen._LAZY) == set(qf_screen.__all__)
    for name, module in qf_screen._LAZY.items():
        own = getattr(importlib.import_module(module), name)
        assert getattr(qf_screen, name) is own, name
    importlib.import_module("qf_screen.ui.watch")       # what `qf-screen watch` does

    assert callable(qf_screen.watch)
    assert qf_screen.watch is qf_screen.ui.watch.watch
    assert qf_screen.WatchReport is qf_screen.ui.watch.WatchReport


def test_no_module_at_the_package_root_is_named_like_an_export():
    """What let the shadowing above happen: a top-level module's name
    becomes an attribute of the package as soon as it is imported, and
    that ordinary attribute wins over the root's lazy lookup."""
    import importlib

    import qf_screen

    root = Path(list(qf_screen.__path__)[0]).resolve()
    stems = {path.stem for path in root.glob("*.py")} - {"__init__"}
    assert stems, root                                   # the right directory
    assert not stems & set(qf_screen._LAZY), sorted(stems & set(qf_screen._LAZY))
    # the pages and the watchlist live under ui/, at their new path
    for module in ("qf_screen.ui.html_report", "qf_screen.ui.watch"):
        expected = root / "ui" / f"{module.rsplit('.', 1)[1]}.py"
        assert Path(importlib.import_module(module).__file__).resolve() == expected, module


def test_watch_cli(tmp_path, capsys):
    root = _risk_tree(tmp_path)
    base = ["watch", *_common(root), "--as-of", END]
    with pytest.raises(SystemExit) as exc:
        main(["watch", *_common(root)])
    assert "--symbols" in str(exc.value)

    assert main([*base, "--symbols", "SH600003,SH600005,600001", "--preset", "momentum_trend"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("watch: 3 symbols as of 2026-06-30 (preset: momentum_trend) -- ")
    assert "rules in force: st, pledge, trend_break, liquidity, suspended, preset_exit" in text
    for line in (r"HIGH\s+st\s+ST/\*ST risk-warning name",
                 r"HIGH\s+pledge\s+pledge ratio 35\.0% at or above 30%",
                 r"MEDIUM\s+preset_exit\s+out of momentum_trend: fails "):
        assert re.search(r"^    " + line, text, flags=re.M), (line, text)
    assert "SH600001  公司600001  沪市主板 · 食品饮料  close " in text   # 600001 resolved
    assert "SH600003  公司600003  沪市主板 · 未分类  close " in text   # unclassified

    # names with nothing against them: exit code 0 under --fail-on-alert, each "ok"
    assert main([*base, "--symbols", "BJ430004", "--fail-on-alert"]) == 0
    quiet = capsys.readouterr().out
    assert "watch: 1 symbols as of 2026-06-30 -- no alerts" in quiet and "    ok" in quiet
    assert main([*base, "--symbols", "SH600001,SH600005", "--fail-on-alert"]) == EXIT_ALERT
    capsys.readouterr()

    assert main([*base, "--symbols", "SH600001,SH600005", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["asof"] == END and payload["symbols"] == 2 and payload["alerts"] >= 2
    assert {row["rule"] for row in payload["detail"]} >= {"st", "pledge"}
    assert main([*base, "--symbols", "SH600001", "--format", "markdown"]) == 0
    assert capsys.readouterr().out.startswith(f"## watch — {END} · 1 symbols")
    out = tmp_path / "watch.md"
    assert main([*base, "--symbols", "SH600001", "--format", "md", "--out", str(out)]) == 0
    assert f"wrote watch report to {out}" in capsys.readouterr().out and out.exists()
    assert main([*base, "--symbols", "SH600001", "--format", "json", "--out", str(tmp_path / "w.json")]) == 0

    # a watchlist file carries the preset, the custom rules and the tuning
    book = tmp_path / "book.yaml"
    book.write_text("preset: momentum_trend\nsymbols: [SH600005, SZ000002]\n"
                    "rules:\n  - {name: cheap, expr: \"close_raw < 3\", severity: low}\n"
                    "disable: [pledge]\nthresholds: {liquidity: 1e8}\n", encoding="utf-8")
    assert main(["watch", *_common(root), "--as-of", END, "--watchlist", str(book),
                 "--fail-on-alert"]) == EXIT_ALERT
    text = capsys.readouterr().out
    assert "(preset: momentum_trend) -- " in text
    assert "rules in force: st, trend_break, liquidity, suspended, cheap, preset_exit" in text
    assert re.search(r"^    LOW\s+cheap\s+custom rule \(close_raw 1\.68\)$", text, flags=re.M)
    assert "pledge" not in text.replace("preset_exit", "")
    assert "under the 1.0亿 floor" in text
    # the flag's names join the file's; the flag's --preset wins over the file's
    assert main(["watch", *_common(root), "--as-of", END, "--watchlist", str(book),
                 "--symbols", "SH600003", "--preset", "value_dividend"]) == 0
    text = capsys.readouterr().out
    assert "watch: 3 symbols as of 2026-06-30 (preset: value_dividend)" in text
    plain = tmp_path / "book.txt"
    plain.write_text("# the quiet one\nBJ430004\n", encoding="utf-8")
    assert main(["watch", *_common(root), "--as-of", END, "--watchlist", str(plain),
                 "--fail-on-alert"]) == 0
    assert "watch: 1 symbols as of 2026-06-30 -- no alerts" in capsys.readouterr().out
    for body, needle in (("symbols: [SH600005]\nrules:\n  - {name: x, expr: \"pe_ttm\"}\n",
                          "rule 'x'"),
                         ("symbols: [SH600005]\ndisable: [nope]\n", "cannot disable")):
        bad = tmp_path / "bad.yaml"
        bad.write_text(body, encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            main(["watch", *_common(root), "--as-of", END, "--watchlist", str(bad)])
        assert "qf-screen watch: " in str(exc.value) and needle in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        main([*base, "--symbols", "SH600001", "--preset", "no_such_preset"])
    assert "no_such_preset" in str(exc.value)


# ======================== Todo 3: self-contained HTML reports ==============
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "param", "source", "track", "wbr"}


class _Markup(HTMLParser):
    """Tracks open tags, element ids, the chart-data blobs, and every
    reference to a resource outside the document."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack, self.errors, self.ids, self.blobs, self.outside = [], [], [], [], []
        self._blob = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        for key in ("src", "href", "poster", "data"):
            if re.match(r"(https?:)?//", attrs.get(key) or ""):
                self.outside.append((tag, key, attrs[key]))
        if tag in ("link", "iframe", "img"):
            self.outside.append((tag, attrs.get("rel"), attrs.get("href") or attrs.get("src")))
        if tag == "script" and attrs.get("src"):
            self.outside.append(("script", "src", attrs["src"]))
        if tag in _VOID:
            return
        self.stack.append(tag)
        self._blob = tag == "script" and attrs.get("type") == "application/json"

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID and self.stack and self.stack[-1] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> with {self.stack[-4:]} open")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()
        self._blob = False

    def handle_data(self, data):
        if self._blob:
            self.blobs.append(data)


def _check_markup(document, what="page"):
    """Balanced tags, unique ids, every SVG well-formed XML, every chart-data
    blob valid JSON, and nothing fetched from outside the file.  Returns how
    many charts (``<svg class="plot">``) and data blobs the page carries."""
    markup = _Markup()
    markup.feed(document)
    markup.close()
    assert not markup.errors, (what, markup.errors[:3])
    assert not markup.stack, (what, "unclosed", markup.stack)
    assert not markup.outside, (what, "external resource", markup.outside)
    assert len(markup.ids) == len(set(markup.ids)), (what, "duplicate ids", markup.ids)
    for blob in markup.blobs:
        json.loads(blob)
    svgs = re.findall(r"<svg\b.*?</svg>", document, flags=re.S)
    for svg in svgs:                               # inline SVG: well-formed XML too
        ET.fromstring(svg)
    for pattern in (r"https?://", r"url\(", r"@import", r"<link\b", r"<img\b", r"<iframe\b",
                    r"fetch\(", "XMLHttpRequest", r"\bimport\s"):
        assert not re.search(pattern, document), (what, pattern)
    return {"svg": len(svgs), "plots": document.count('<svg class="plot"'),
            "blobs": len(markup.blobs)}


#: a screen whose membership moves from session to session (v0.3's "mover")
MOVER_YAML = ("name: mover\n"
              "universe: {boards: [all], exclude_st: false, min_listed_days: 0}\n"
              "filters:\n"
              "  - {name: hot, expr: \"ret_21d > 0.02\"}\n"
              "rank:\n"
              "  - {expr: \"pct_rank(ret_21d)\", weight: 1.0}\n"
              "top_n: 2\n")


def _moving_date(engine):
    """A recent session at which ``mover`` gained a name, lost one AND
    shuffled its ranks -- one of each thing the badges and the pills are
    drawn from (2026-05-13 on the synthetic store)."""
    for back in range(1, 60):
        asof = engine.panel.calendar[-back]
        diff = engine.diff(ScreenSpec.from_yaml("mover"), asof=asof)
        if len(diff.entered) and len(diff.exited) and len(diff.movers):
            return str(asof.date()), diff
    raise AssertionError("the mover preset never turned over in the last 60 sessions")


def test_report_html_is_one_self_contained_page_with_badges(store_dir, tmp_path, capsys):
    from qf_screen.ui.html_report import UNCLASSIFIED

    _user_presets().joinpath("mover.yaml").write_text(MOVER_YAML, encoding="utf-8")
    engine = ScreenEngine(CNDataStore(store_dir, warmup_rows=cf.SESSIONS, use_cache=False))
    asof, mover = _moving_date(engine)
    dividend = engine.diff(ScreenSpec.from_yaml("value_dividend"), asof=asof)
    out = tmp_path / "report.html"
    assert main(["report", *_common(store_dir), "--as-of", asof, "--out", str(out),
                 "--presets", "mover,value_dividend", "--format", "html"]) == 0
    assert f"wrote HTML report to {out}" in capsys.readouterr().out
    page = out.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">")
    assert page.rstrip().endswith("</html>")
    assert f"<title>qf-screen daily briefing — {asof}</title>" in page
    assert page.count("<style>") == 1 and page.count("<script>") == 1
    found = _check_markup(page, "report")
    assert found["plots"] == 2 and found["blobs"] == 0       # one bar chart per preset
    assert "<code>mover</code> — " in page and "<code>value_dividend</code> — " in page
    assert "Industry concentration" in page and 'id="overview"' in page and 'id="filter"' in page
    assert f"Entered (+{len(mover.entered)}) vs {mover.prev.date()}" in page
    assert f"Exited (−{len(mover.exited)})" in page
    # every badge and pill on the page is one a diff backs: nothing is invented.
    # A name that entered wears +NEW twice (its change-list line and its member
    # row); a name that exited wears -OUT once; each table's legend line has one
    entered = len(mover.entered) + len(dividend.entered)
    exited = len(mover.exited) + len(dividend.exited)
    assert page.count("joined since the last session") == 2      # one legend line per table
    assert entered >= 1 and page.count('class="badge new"') == 2 * entered + 2
    assert page.count('class="badge out"') == exited > 0
    assert page.count('class="pill"') == len(mover.movers) + len(dividend.movers) > 0
    for values in list(mover.movers.to_dict(orient="records")) + \
            list(dividend.movers.to_dict(orient="records")):
        change = values["rank_prev"] - values["rank_now"]
        pill = (f'<span class="pill">↑+{change}</span>' if change > 0
                else f'<span class="pill">↓−{-change}</span>')
        assert pill in page, pill
    # the change lists name each name with the rank it holds or held
    for values in mover.entered.to_dict(orient="records"):
        assert f'<span class="who">#{values["rank"]} {values["symbol"]}</span>' in page
    for values in mover.exited.to_dict(orient="records"):
        assert f'<span class="who">#{values["rank"]} {values["symbol"]}</span>' in page
    # the industry bars: one hue, and only a name without a classification is grey
    bars = re.findall(r'data-name="([^"]+)" data-value="[^"]*">.*?<path class="bar ([^"]+)"',
                      page, flags=re.S)
    assert bars and all((tone == "ref-1") == (name == UNCLASSIFIED) for name, tone in bars)
    # a member row carries a numeric sort key; every table header sorts
    row = re.search(r"<tr data-order=\"0\">(.*?)</tr>", page, flags=re.S).group(1)
    assert 'data-v="1.0">1</td>' in row
    assert page.count('<button type="button" class="sort" data-col=') >= 6 * 2 + 4 * 2
    assert "Generated by qf-screen" in page and "no external resource is loaded" in page
def test_report_html_marks_symbols_for_a_host_page(store_dir, capsys):
    """Every stock the briefing names carries its code in a ``data-
    symbol`` attribute: inert in the standalone page, there for a host
    (qf-screen-web shows the page in a frame and links each symbol to
    its Inspect view).  The member tables mark the code cell; the change
    lists mark their whole line."""
    assert main(["report", *_common(store_dir), "--as-of", END, "--format", "html",
                 "--top", "1", "--presets", "momentum_trend"]) == 0
    page = capsys.readouterr().out
    marked = re.findall(r'<span data-symbol="([^"]+)">\1</span>', page)
    # the member table's symbol cells are the marked ones (the overview
    # table names presets, not stocks)
    assert marked and all(re.fullmatch(r"(SH|SZ|BJ)\d{6}", code) for code in marked)
    for line in re.findall(r'<li data-symbol="([^"]+)">', page):
        assert re.fullmatch(r"(SH|SZ|BJ)\d{6}", line)
    # the standalone page wires nothing to them: the attribute is inert
    assert "data-symbol" not in (re.search(r"<script>(.*?)</script>", page, re.S) or
                                 re.match(r"", "")).group(1)


def test_report_html_to_stdout_and_user_presets_marked(store_dir, capsys):
    assert main(["report", *_common(store_dir), "--as-of", END, "--format", "html",
                 "--top", "1", "--presets", "momentum_trend"]) == 0
    page = capsys.readouterr().out
    assert _check_markup(page, "stdout report")["plots"] == 1
    assert page.startswith("<!doctype html>") and "Top 1 of 2 members" in page
    assert main(["report", *_common(store_dir), "--as-of", END, "--format", "html",
                 "--presets", "value_dividend"]) == 0            # fewer members than --top
    assert "All 1 members" in capsys.readouterr().out
    _user_presets().joinpath("mine.yaml").write_text(LOOSE_YAML.format(name="mine", top=2),
                                                     encoding="utf-8")
    assert main(["report", *_common(store_dir), "--as-of", END, "--format", "html",
                 "--presets", "mine,value_dividend"]) == 0
    page = capsys.readouterr().out
    assert '<code>mine</code></a> <span class="muted">user</span>' in page
    assert page.count('<span class="muted">user</span>') == 1
    # the first session has no earlier one to compare with: said, not faked
    assert main(["report", *_common(store_dir), "--as-of", str(cf.CALENDAR[0].date()),
                 "--format", "html", "--presets", "mine"]) == 0
    page = capsys.readouterr().out
    _check_markup(page, "first session")
    assert "No earlier session in the panel to compare with" in page
    assert 'class="badge out"' not in page and "Entered (+0)" not in page
    assert "no earlier session" in page                          # ... on the tiles too


def test_validate_and_compare_html_pages(tmp_path, capsys):
    root = _tree(tmp_path / "tree")
    base = _common(root)
    window = ["--start", "2026-02-01", "--end", END, "--horizons", "5,20"]
    report, curve = tmp_path / "v.html", tmp_path / "curve.csv"
    assert main(["validate", "momentum_trend", *base, *window, "--cost-bps", "15",
                 "--skip-limit-up", "--format", "html", "--out", str(report),
                 "--curve", str(curve)]) == 0
    text = capsys.readouterr().out
    assert f"wrote HTML report to {report}" in text and curve.exists()   # --curve still works
    page = report.read_text(encoding="utf-8")
    found = _check_markup(page, "validate")
    assert found["plots"] == 2 and found["blobs"] == 2                  # curve + drawdown
    assert "<title>qf-screen validation — momentum_trend</title>" in page
    for tile in ("Annualized return", "Annualized volatility", "Sharpe ratio", "Max drawdown",
                 "Cumulative return", "Turnover per rebalance"):
        assert f'<div class="tile-label">{tile}</div>' in page, tile
    assert "15 bps cost per side, limit-up entries skipped" in page
    assert re.search(r'<div class="tile-value down">−[\d.]+%</div>', page)   # a loss reads as one
    assert re.search(r'<div class="tile-note">net −[\d.]+%</div>', page)    # the net twin
    assert "Cumulative value: momentum_trend against its pool" in page
    assert "Drawdown from the running peak" in page
    assert '<path class="line series-1" ' in page                       # the book, in slot 1
    assert '<path class="line series-1 net" ' in page                   # ... and its net twin
    assert '<path class="line ref-1" ' in page                          # the pool, in grey
    assert 'data-key="series-1-net"' in page and "momentum_trend (net)" in page
    for title in ("Forward returns by horizon", "Rebalance by rebalance",
                  "Statistics over the same sessions"):
        assert title in page, title
    assert "skipped (limit-up)" in page and "cost paid" in page
    assert "were not bought" in page                                    # the guard's note
    payload = json.loads(re.search(r'<script type="application/json" id="curve-data">(.*?)'
                                   "</script>", page, flags=re.S).group(1))
    assert [item["name"] for item in payload["series"]] == ["momentum_trend", "universe",
                                                            "momentum_trend (net)"]
    assert [item["key"] for item in payload["series"]] == ["series-1", "ref-1", "series-1-net"]
    drawn = pd.read_csv(curve, index_col=0, parse_dates=True)   # the --curve file's own columns
    assert list(drawn.columns) == ["portfolio", "universe", "portfolio_net"]
    assert len(payload["labels"]) == len(drawn) and payload["baseline"] == 1.0
    np.testing.assert_allclose(payload["series"][0]["values"], drawn["portfolio"].to_numpy())
    assert page.count("<tr data-order=") >= 2 * len(drawn) + 8   # the table twins, the rebalances
    assert re.search(r'<text class="end-label" x="[\d.]+" y="[\d.]+">[+−][\d.]+% '
                     r'<tspan class="name">momentum_trend</tspan></text>', page)

    compared = tmp_path / "c.html"
    assert main(["compare", "value_dividend", "momentum_trend", *base, *window,
                 "--benchmarks", "", "--cost-bps", "15", "--format", "html",
                 "--out", str(compared)]) == 0
    page = compared.read_text(encoding="utf-8")
    assert "<title>qf-screen comparison</title>" in page
    assert _check_markup(page, "compare") == {"svg": 8, "plots": 2, "blobs": 2}
    assert '<path class="line series-1" ' in page and '<path class="line series-2 dash-2" ' in page
    assert "value_dividend" in page and "momentum_trend" in page and "Whole market" in page
    assert "Annualized return (net)" in page and "Excess over own pool / yr" in page
    assert "data-restore=\"1\"" in page                    # the metric column restores the order
    assert len(re.findall(r"<tr data-order=", page)) >= 14   # the metric rows + chart twins
    assert main(["compare", "value_dividend", "momentum_trend", *base, *window, "--benchmarks",
                 "", "--format", "html"]) == 0            # to stdout when there is no --out
    _check_markup(capsys.readouterr().out, "compare to stdout")


def test_line_chart_plots_on_its_scale_and_breaks_at_gaps():
    from qf_screen.ui.html_report import line_chart, series

    labels = [f"2026-01-{day:02d}" for day in range(1, 11)]
    rising = series("rising", [1.0, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07, 1.08, 1.09],
                    "series-1")
    gapped = series("gapped", [1.0, 1.0, None, 1.0, 1.0, None, 1.0, 1.0, 1.0, 1.0], "ref-1")
    chart = line_chart("c", "A title", "A subtitle", labels, [rising, gapped])
    found = _check_markup("<!doctype html><html><body>" + chart + "</body></html>", "line chart")
    assert found == {"svg": 3, "plots": 1, "blobs": 1}          # the plot + two legend keys
    drawn = re.findall(r'<path class="line ([^"]+)" d="([^"]+)"', chart)
    assert [classes for classes, _path in drawn] == ["series-1", "ref-1", "ref-1", "ref-1"]
    heights = [float(y) for _x, y in re.findall(r"[ML]([\d.]+),([\d.]+)", drawn[0][1])]
    assert heights == sorted(heights, reverse=True)              # up is up
    assert [path.count("M") for _classes, path in drawn] == [1, 1, 1, 1]   # gaps break the line
    payload = json.loads(re.search(r'<script type="application/json" id="c-data">(.*?)</script>',
                                   chart, flags=re.S).group(1))
    assert payload["labels"] == labels and payload["baseline"] == 1.0
    assert [item["key"] for item in payload["series"]] == ["series-1", "ref-1"]
    assert payload["series"][1]["values"][2] is None and payload["series"][1]["values"][5] is None
    for height, value in zip(heights, payload["series"][0]["values"]):   # the readout's scale
        assert height == pytest.approx(payload["top"] + (payload["hi"] - value)
                                       / (payload["hi"] - payload["lo"]) * payload["plotH"], abs=0.06)
    assert payload["lo"] < 1.0 < payload["hi"]
    assert chart.count("<tr data-order=") == len(labels) == 10         # the table twin
    assert '<rect class="hit"' in chart and 'tabindex="0"' in chart
    assert "<ul class=\"legend\">" in chart
    assert chart.count('<text class="end-label"') == 2                 # two series: both labelled
    ticks = re.findall(r'<line class="gridline" x1="58" y1="([\d.]+)"', chart)
    assert 4 <= len(ticks) <= 7 and len(set(ticks)) == len(ticks)
    tick_labels = re.findall(r'<text x="50" y="[\d.]+" text-anchor="end">([^<]+)</text>', chart)
    moves = [0.0] + [float(label.strip("%+−").replace("−", "-")) for label in tick_labels[1:]]
    assert tick_labels[0] == "0%" and all(label[0] == "+" for label in tick_labels[1:])
    steps = {round(b - a, 6) for a, b in zip(moves, moves[1:])}      # evenly spaced
    assert len(steps) == 1 and 2 in steps                            # ... by a round 2 %
    # one lone session still shows a mark; a series that ends early is padded with gaps,
    # one longer than the axis is refused
    lone = line_chart("l", "t", "s", labels, [series("lone", [1.0, 1.0, None, 1.0, None], "ref-1")])
    marks = re.search(r'<g data-key="ref-1">(.*?)</g>', lone, flags=re.S).group(1)
    assert marks.count("<path") == 1                        # the first two sessions: a line
    assert marks.count('<circle class="dot ref-1" cx="') == 1   # the isolated one: a dot
    early = line_chart("e", "t", "s", labels, [rising, series("ends early", [1.0, 1.1], "ref-1")])
    assert json.loads(re.search(r'id="e-data">(.*?)</script>', early, flags=re.S).group(1)
                     )["series"][1]["values"] == [1.0, 1.1, None, None, None, None, None, None, None,
                                                  None]
    with pytest.raises(ValueError) as exc:
        line_chart("x", "t", "s", labels[:3], [rising])
    assert "10 values for 3 sessions" in str(exc.value)
    # a drawdown hangs from a baseline pinned to the top of the scale
    fall = series("fall", [0.0, -0.02, -0.05, -0.01, -0.10, -0.03], "series-2")
    chart = line_chart("d", "Drawdown", "", ["a", "b", "c", "d", "e", "f"], [fall], baseline=0.0,
                       area=True, anchor_top=True)
    payload = json.loads(re.search(r'id="d-data">(.*?)</script>', chart, flags=re.S).group(1))
    assert payload["hi"] == 0.0 and payload["lo"] < -0.10
    assert 'class="area series-2"' in chart and "<ul class=\"legend\">" not in chart   # one series
    assert chart.count('<text class="end-label"') == 1 and "−3.0%" in chart


def test_line_chart_direct_labels_lead_when_they_would_collide():
    from qf_screen.ui.html_report import SERIES_TONES, line_chart, series

    labels = [str(day) for day in range(6)]
    close = [series("first", [1, 1.1, 1.2, 1.2, 1.2, 1.2], SERIES_TONES[0]),
             series("second", [1, 1.1, 1.2, 1.2, 1.2, 1.201], SERIES_TONES[1])]
    chart = line_chart("c", "t", "s", labels, close)
    assert chart.count('<text class="end-label"') == 2
    assert chart.count('class="leader ') >= 1                 # moved apart, and connected
    assert '+20.0% <tspan class="name">first</tspan>' in chart
    five = [series(f"s{index}", [1 + 0.01 * index * step for step in range(6)], SERIES_TONES[index])
            for index in range(5)]
    chart = line_chart("many", "t", "s", labels, five)
    assert "end-label" not in chart and "leader" not in chart  # past four: legend + readout
    assert chart.count("<li data-key=") == 5
    for index in range(1, 5):                                 # slot 1 solid, the rest patterned
        assert f'class="line series-{index + 1} dash-{index + 1}"' in chart
        assert f'<line class="key series-{index + 1} dash-{index + 1}"' in chart   # the key too
    assert '<path class="line series-1" ' in chart and "dash-1" not in chart
    assert "No data." in line_chart("none", "t", "s", labels, [series("empty", [None] * 6)])
    # colour follows the entity, never its rank: a series keeps its slot wherever it ends
    chart = line_chart("shuffled", "t", "s", labels, [five[3], five[0], five[4]])
    assert re.findall(r'<g data-key="([^"]+)">', chart) == ["series-4", "series-1", "series-5"]


def test_bar_chart_ranks_in_one_hue_and_folds_the_tail():
    from qf_screen.ui.html_report import OTHER, UNCLASSIFIED, bar_chart

    rows = [("银行", 9), ("煤炭", 4), ("电子", 2), ("汽车", 2), ("医药", 1), ("钢铁", 1),
            ("化工", 1), ("建材", 1), ("农业", 1), (UNCLASSIFIED, 5)]
    chart = bar_chart("Industry concentration", "by industry", rows, total=27, limit=5)
    assert _check_markup("<!doctype html><html><body>" + chart + "</body></html>",
                         "bar chart")["plots"] == 1
    assert re.findall(r'data-name="([^"]+)"', chart)[:6] == [
        "银行", "煤炭", "电子", "汽车", OTHER, UNCLASSIFIED]            # largest first, loose last
    assert 'data-name="其他" data-value="5 (19%)"' in chart   # the five 1s fold into 其他
    assert chart.count('class="bar series-1"') == 5 and chart.count('class="bar ref-1"') == 1
    assert chart.count("a4,4 0 0 1 4,4") == 6              # a rounded tip, a square baseline
    assert ">9 (33%)</text>" in chart and chart.count('class="hit"') == 6
    assert chart.count("<tr data-order=") == 10            # the table twin keeps every row
    # a tiny bar is a plain rectangle; a long name is shortened, the tooltip keeps it whole
    sliver = bar_chart("t", "s", [("a-very-long-industry-name-here", 100), ("b", 0.4)],
                       total=100.4)
    assert ">a-ver…</text>" in sliver                                # shortened to six
    assert 'data-name="a-very-long-industry-name-here"' in sliver     # whole, for the tooltip
    assert sliver.count("a4,4 0 0 1 4,4") == 1
    assert re.search(r"M100,[\d.]+h[\d.]+v16h-[\d.]+z", sliver)      # under 4px: square


def test_tables_carry_sort_keys_and_the_page_pins_the_validated_palette():
    from qf_screen.ui.html_report import _CSS, _TOKENS, Cell, data_table, rank_pill, stat_tiles, text

    table = data_table([("name", ""), ("value", "num")],
                       [[text("b"), Cell("12%", "0.12")], [text("a")]], "Caption", sortable=True,
                       searchable=True, restore_first=True, scopes=True)
    assert table.count('<button type="button" class="sort" data-col="') == 2
    assert table.count('data-restore="1"') == 1
    assert 'data-v="0.12"' in table
    assert '<table class="data sortable searchable" data-scopes="1">' in table
    assert '<tr class="fill" hidden>' in table and '<caption>Caption<span class="count"' in table
    assert '<tr class="none"><td colspan="1">(nothing to show)</td></tr>' in data_table(
        [("a", "")], [], "Nothing")
    plain = data_table([("a", ""), ("n", "num")], [["x", 3.5]], "c", sortable=False)
    assert "<button" not in plain and 'data-v="3.5"' in plain and ">3.5</td>" in plain
    assert rank_pill(5) == '<span class="pill">↑+5</span>'
    assert rank_pill(-3) == '<span class="pill">↓−3</span>' and rank_pill(0) == rank_pill(None) == ""
    tiles = stat_tiles([("Up", "+1.0%", "net +0.8%"), ("Flat", "0.0%", ""), ("Down", "−2.0%", "")],
                       tones=["auto", "auto", "auto"])
    assert tiles.count('<div class="tile-value up">+1.0%</div>') == 1
    assert '<div class="tile-value ">0.0%</div>' in tiles
    assert tiles.count('<div class="tile-value down">−2.0%</div>') == 1
    # the palette is the validated set, in its validated order, for both surfaces
    assert [_TOKENS["light"][f"series-{index}"] for index in range(1, 9)] == [
        "#257de0", "#f47230", "#00b894", "#f2a600", "#ee7fac", "#008a17", "#6a4ce2", "#ec4647"]
    assert [_TOKENS["dark"][f"series-{index}"] for index in range(1, 9)] == [
        "#338ced", "#e76624", "#00a888", "#d28e05", "#e24d8b", "#0aac23", "#9887ef", "#eb6263"]
    assert [_TOKENS[mode][f"ref-{index}"] for index in range(1, 4) for mode in ("light",)] == [
        "#666666", "#7a7a7a", "#8c8c8c"]
    for token, value in {**_TOKENS["light"], **_TOKENS["dark"]}.items():
        assert f"--{token}:{value}" in _CSS
    for rule in (".series-1{stroke:var(--series-1)}", "svg .area.series-1{fill:var(--series-1)}",
                 "svg .bar.series-1{fill:var(--series-1)}", ".ref-1{stroke:var(--ref-1)}",
                 ".net{stroke-dasharray:2 2}", ".ref-2{stroke-dasharray:8 4}",
                 ".patterns .dash-2{stroke-dasharray:8 3}",
                 ".patterns .dash-8{stroke-dasharray:4 2 1 2}"):
        assert rule in _CSS, rule
    # every pattern rule sits inside its own scope exactly once (a bare `.dash-3{...}`
    # once dotted a line outside patterns mode)
    for index in range(2, 9):
        assert _CSS.count(f".dash-{index}{{stroke-dasharray") == 3      # patterns, print, forced
        assert f"\n.dash-{index}{{" not in _CSS and "}}.dash-" not in _CSS
    assert "@media print{.dash-2{stroke-dasharray:8 3}.dash-3{" in _CSS
    assert "@media (forced-colors:active){.dash-2{" in _CSS
    assert _CSS.count("@media print{") == 3
    assert "http://" not in _CSS and "https://" not in _CSS
    # the surface follows the OS unless a host embedding the page picks one
    assert ('@media (prefers-color-scheme:dark)'
            '{:root:where(:not([data-theme="light"])){') in _CSS
    assert ':root[data-theme="dark"]{color-scheme:dark;' in _CSS


def test_html_reports_work_in_a_browser(tmp_path):
    """Rendered in headless Chromium: the crosshair and readout, the sort,
    the filter, the legend, the bar tooltips -- and a clean console, in
    light and in dark mode.  What markup checks cannot see (an SVG group
    that ignored ``.hidden``, a CSS rule that escaped its scope) shows up
    here.  Skipped where no Chromium is installed."""
    sync_api = pytest.importorskip("playwright.sync_api")
    manager = sync_api.sync_playwright().start()
    try:
        browser = manager.chromium.launch()
    except Exception as exc:                                  # noqa: BLE001 - no browser here
        manager.stop()
        pytest.skip(f"headless Chromium is not available ({str(exc)[:60]})")
    root = _tree(tmp_path / "tree")
    links = {}
    for name, argv in (("report", ["report", *_common(root), "--as-of", END, "--format", "html"]),
                       ("validate", ["validate", "momentum_trend", *_common(root), "--start",
                                     "2026-02-01", "--end", END, "--horizons", "5", "--benchmarks",
                                     "", "--cost-bps", "15", "--format", "html"])):
        assert main([*argv, "--out", str(tmp_path / f"{name}.html")]) == 0
        links[name] = (tmp_path / f"{name}.html").as_uri()
    OVERVIEW_ROWS = """() => Array.from(document.querySelectorAll('table.sortable')[0]
        .tBodies[0].rows).filter(r => r.classList.length === 0)
        .map(r => r.cells[1].textContent)"""
    MEMBER_ROWS = """() => Array.from(document.querySelectorAll('table.searchable')[0]
        .tBodies[0].rows).filter(r => !r.classList.contains('fill') && !r.classList.contains('none'))
        .map(r => [r.hidden, r.textContent])"""
    CURSOR_HIDDEN = "() => document.querySelector('g.cursor').hasAttribute('hidden')"
    DASH = ("() => getComputedStyle(document.querySelector('%s')).strokeDasharray")
    try:
        for background in ("rgb(249, 249, 249)", "rgb(11, 11, 11)"):
            context = browser.new_context(viewport={"width": 1280, "height": 1000})
            page = context.new_page()
            problems = []
            page.on("console", lambda message: problems.append(message.text)
                    if message.type == "error" else None)
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.emulate_media(color_scheme="dark" if background.endswith("11, 11, 11)") else "light")
            page.goto(links["report"])
            assert page.evaluate("getComputedStyle(document.body).backgroundColor") == background
            # ... and a host embedding the page picks the surface with data-theme
            light = background.endswith("249, 249, 249)")
            page.evaluate(f"document.documentElement.dataset.theme = "
                          f"'{'dark' if light else 'light'}'")
            assert page.evaluate("getComputedStyle(document.body).backgroundColor") == (
                "rgb(11, 11, 11)" if light else "rgb(249, 249, 249)")
            page.evaluate("delete document.documentElement.dataset.theme")
            assert page.evaluate("getComputedStyle(document.body).backgroundColor") == background
            # sorting: a header button orders the rows by its column, either way
            before = [int(value) for value in page.evaluate(OVERVIEW_ROWS)]
            page.click("table.sortable th button.sort[data-col='1']")
            assert [int(value) for value in page.evaluate(OVERVIEW_ROWS)] == sorted(before)
            page.click("table.sortable th button.sort[data-col='1']")
            assert [int(value) for value in page.evaluate(OVERVIEW_ROWS)] == sorted(
                before, reverse=True)
            assert page.evaluate("document.querySelectorAll('th[aria-sort=\"descending\"]')"
                                 ".length") == 1
            # the filter: rows that match stay, the rest hide, the counts follow
            symbol = "SH600001"
            page.fill("#filter", symbol)
            page.wait_for_timeout(40)
            left = page.evaluate(MEMBER_ROWS)
            assert left and not all(hidden for hidden, _text in left)
            assert all((symbol in text) == (not hidden) for hidden, text in left)
            assert "rows shown" in page.inner_text("#filter-status")
            page.fill("#filter", "no-such-name-anywhere")
            page.wait_for_timeout(40)
            assert all(hidden for hidden, _text in page.evaluate(MEMBER_ROWS))
            assert page.evaluate("document.querySelectorAll('tr.fill:not([hidden])').length") >= 1
            page.fill("#filter", "")
            # a bar is its own hit target (scrolled into view first: the
            # 50-preset library pushes the first bar chart below the fold)
            bar = page.query_selector("svg.plot g.bar-row rect.hit")
            bar.scroll_into_view_if_needed()
            box = bar.bounding_box()
            page.mouse.move(box["x"] + 40, box["y"] + 12)
            page.wait_for_timeout(40)
            assert page.evaluate("() => !document.querySelector('.chart-body .tip').hidden")
            assert page.inner_text(".chart-body .tip") != ""

            # a line chart: the crosshair appears, the readout lists every series
            page.goto(links["validate"])
            assert page.evaluate(CURSOR_HIDDEN)
            box = page.query_selector("svg.plot rect.hit").bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] / 2)
            page.wait_for_timeout(40)
            assert not page.evaluate(CURSOR_HIDDEN)
            readout = page.inner_text(".chart-body .tip")
            assert "momentum_trend" in readout and "universe" in readout and "(net)" in readout
            assert page.evaluate("""() => Array.from(document.querySelectorAll('g.cursor circle.dot'))
                .filter(d => d.getAttribute('visibility') === 'visible').length""") >= 2
            # ... and on the keyboard: Home, then a step to the right
            page.focus("svg.plot rect.hit")
            page.keyboard.press("Home")
            first = page.inner_text(".chart-body .tip .tip-date")
            page.keyboard.press("ArrowRight")
            assert page.inner_text(".chart-body .tip .tip-date") > first
            # the legend isolates a series, and a second click lets the others back
            page.click("ul.legend li[data-key='series-1'] button")
            assert page.evaluate("document.querySelectorAll('svg.plot g.series > g.off').length") == 2
            page.click("ul.legend li[data-key='series-1'] button")
            assert page.evaluate("document.querySelectorAll('svg.plot g.series > g.off').length") == 0
            # the net twin dashes, the gross line stays solid, on either surface
            assert page.evaluate(DASH % "path.line.net") != "none"
            assert page.evaluate(DASH % "path.line.series-1") == "none"
            assert problems == []
            context.close()
    finally:
        browser.close()
        manager.stop()


# ==================== Todo 4: archive, exit codes and webhooks =============
class _Recorder:
    """Stands in for :func:`qf_screen.notify.post`: every request is
    recorded, and answered as the test arranged (or refused with ``error``)."""

    def __init__(self, status=200, body='{"ok": true}'):
        self.calls, self.status, self.body, self.error = [], status, body, None

    def __call__(self, url, data, headers, timeout=notify.TIMEOUT):
        self.calls.append({"url": url, "body": json.loads(data), "headers": headers,
                           "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.status, self.body


@pytest.fixture()
def recorder(monkeypatch):
    """A delivered webhook, without the network (conftest trips any other)."""
    tape = _Recorder()
    monkeypatch.setattr(notify, "post", tape)
    return tape


def test_history_dir_reads_the_flag_the_variable_then_the_default(tmp_path, monkeypatch):
    from qf_screen.history import history_dir

    assert history_dir() == Path(os.environ["QF_SCREEN_HISTORY_DIR"])   # set by conftest
    assert history_dir(str(tmp_path / "x")) == tmp_path / "x"
    monkeypatch.delenv("QF_SCREEN_HISTORY_DIR")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert history_dir() == tmp_path / "xdg" / "qf_screen" / "history"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert history_dir() == Path.home() / ".local" / "share" / "qf_screen" / "history"


def test_snapshot_archive_is_idempotent_per_date(tmp_path):
    from qf_screen.history import (
        HistoryError, history_file, history_view, load_history, save_snapshot,
    )

    archive = tmp_path / "archive"
    first = save_snapshot(archive, "p", "2026-11-24", ["A", "B", "D"], [0.9, 0.8, 0.7])
    assert first == {"file": archive / "p.csv", "rows": 3, "replaced": 0, "sessions": 1}
    save_snapshot(archive, "p", pd.Timestamp("2026-11-25"), ["A", "B", "C"], [0.95, 0.85, 0.75])
    again = save_snapshot(archive, "p", "2026-11-25", ["A", "B", "C", "E"],
                          [0.95, 0.85, 0.75, 0.6])                 # the same session, run again
    assert again["replaced"] == 3 and again["sessions"] == 2
    assert not (archive / "p.csv.tmp").exists()                    # swapped in atomically
    save_snapshot(archive, "p", "2026-11-26", ["A", "C"])          # an unranked screen: no scores
    save_snapshot(archive, "other", "2026-11-26", ["Z"])           # one file per preset
    frame = load_history(archive, "p")
    assert list(frame.columns) == ["asof", "symbol", "rank", "score"] and len(frame) == 9
    assert frame["asof"].is_monotonic_increasing and "E" in set(frame["symbol"])
    assert (archive / "p.csv").read_text(encoding="utf-8").splitlines()[0] == "asof,symbol,rank,score"
    assert load_history(archive, "nobody").empty

    view = history_view(frame, "p", days=30, names={"A": "alpha"})
    assert view.asof == "2026-11-26"
    assert view.sessions == ["2026-11-24", "2026-11-25", "2026-11-26"]
    members = view.members.set_index("symbol")
    assert list(members.index) == ["A", "C"]                       # today's list, in rank order
    assert members.loc["A", ["name", "rank", "streak", "present", "stayed", "first_seen"]].tolist() \
        == ["alpha", 1, 3, 3, 1.0, "2026-11-24"]
    assert members.loc["C", ["rank", "streak", "present"]].tolist() == [2, 2, 2]
    assert members.loc["C", "stayed"] == pytest.approx(2 / 3) and np.isnan(members.loc["A", "score"])
    assert members.loc["C", "name"] == "" and view.new_today == 0
    left = view.exited.set_index("symbol")
    assert list(left.index) == ["B", "E", "D"]                     # the latest leavers first
    assert left.loc["B"].tolist() == ["", "2026-11-25", 2, 2]      # 2-session run, was #2
    assert left.loc["E"].tolist() == ["", "2026-11-25", 1, 4]
    assert left.loc["D"].tolist() == ["", "2026-11-24", 1, 3]
    assert view.sizes == {"2026-11-24": 3, "2026-11-25": 4, "2026-11-26": 2}
    assert view.caution == ""
    # a window of two sessions counts presence over those two only
    short = history_view(frame, "p", days=2)
    assert short.sessions == ["2026-11-25", "2026-11-26"]
    assert short.members.set_index("symbol").loc["A", "present"] == 2
    assert short.exited["symbol"].tolist() == ["B", "E"]           # D left before the window
    # a gap breaks a streak; a new name counts as new today
    gappy = pd.DataFrame({"asof": ["d1"] * 2 + ["d2"] * 2 + ["d3"] * 3,
                          "symbol": ["A", "B", "A", "B", "A", "B", "N"],
                          "rank": [1, 2, 1, 2, 2, 1, 3], "score": 0.5})
    view = history_view(gappy, "g")
    assert view.members.set_index("symbol").loc["N", ["streak", "present", "first_seen"]].tolist() \
        == [1, 1, "d3"] and view.new_today == 1
    # the archive is only as good as the runs behind it: said when sizes swing
    uneven = history_view(pd.DataFrame({
        "asof": ["d1"] * 5 + ["d2"] * 12, "symbol": [f"S{i}" for i in range(5)] + [f"S{i}" for i in range(12)],
        "rank": list(range(1, 6)) + list(range(1, 13)), "score": 0.5}), "u")
    assert "5 to 12" in uneven.caution and "smaller --top" in uneven.caution
    for bad in (0, -3):
        with pytest.raises(HistoryError) as exc:
            history_view(frame, "p", days=bad)
        assert "--days must be 1 or more" in str(exc.value)
    # a name the filesystem cannot take literally still gets a file of its own
    digest = hashlib.sha1("my screen/v2".encode()).hexdigest()[:8]
    assert history_file(archive, "my screen/v2").name == f"my_screen_v2-{digest}.csv"
    assert history_file(archive, "a b").name != history_file(archive, "a/b").name
    assert history_file(archive, "..").name.startswith("preset-")
    # what cannot be archived or read is an error, never a silent overwrite
    with pytest.raises(HistoryError) as exc:
        save_snapshot(archive, "p", "2026-11-27", ["A", "A"])
    assert "appears twice" in str(exc.value) and len(load_history(archive, "p")) == 9
    (archive / "broken.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    with pytest.raises(HistoryError) as exc:
        load_history(archive, "broken")
    assert "not a qf-screen archive" in str(exc.value)
    (archive / "garbage.csv").write_bytes(b"\xff\xfe\x00garbage")
    with pytest.raises(HistoryError):
        load_history(archive, "garbage")


def test_run_and_report_archive_snapshots_and_history_reads_them(store_dir, tmp_path, capsys):
    from qf_screen.history import history_dir, load_history

    archive = tmp_path / "archive"
    out = tmp_path / "m.json"
    assert main(["run", "momentum_trend", *_common(store_dir), "--as-of", END, "--format", "json",
                 "--out", str(out), "--save-history", str(archive)]) == 0
    captured = capsys.readouterr()
    assert "history: 2 rows for momentum_trend as of 2026-06-30 -> " in captured.err
    assert str(archive / "momentum_trend.csv") in captured.err and "wrote json" in captured.out
    assert json.loads(out.read_text(encoding="utf-8"))["preset"] == "momentum_trend"
    # no DIR: the default archive ($QF_SCREEN_HISTORY_DIR)
    assert main(["run", "value_dividend", *_common(store_dir), "--as-of", END, "--save-history",
                 "--format", "json", "--out", str(tmp_path / "v.json")]) == 0
    assert (history_dir() / "value_dividend.csv").exists()
    # the report archives every preset it covers, with a full list
    assert main(["report", *_common(store_dir), "--as-of", END, "--top", "1",
                 "--presets", "value_dividend,momentum_trend", "--save-history", str(archive),
                 "--out", str(tmp_path / "r.md")]) == 0
    captured = capsys.readouterr()
    assert "history: 2 rows for momentum_trend as of 2026-06-30 -> " in captured.err
    # the same session as the `run` above: its rows are replaced, and that is said
    assert "(replaced the 2 rows of an earlier run)" in captured.err
    assert main(["report", *_common(store_dir), "--as-of", str(cf.CALENDAR[-2].date()),
                 "--presets", "value_dividend,momentum_trend", "--save-history", str(archive),
                 "--out", str(tmp_path / "r.md")]) == 0
    capsys.readouterr()
    frame = load_history(archive, "momentum_trend")
    assert frame["asof"].unique().tolist() == [str(cf.CALENDAR[-2].date()), END]

    base = ["history", "momentum_trend", "--history-dir", str(archive), *_common(store_dir)]
    assert main(base) == 0
    text = capsys.readouterr().out
    assert text.startswith("momentum_trend: 2 members as of 2026-06-30")
    assert "archive: 2 sessions 2026-06-29..2026-06-30; counting the last 2" in text
    assert re.search(r"^SH600001\s+公司600001\s+1\s+[\d.]+\s+[12]\s+2\s+100%", text, flags=re.M)
    assert "recently exited" not in text                      # nothing left between the two
    assert main([*base, "--days", "1"]) == 0
    assert "counting the last 1 (2026-06-30..2026-06-30)" in capsys.readouterr().out
    assert main([*base, "--format", "markdown"]) == 0
    markdown = capsys.readouterr().out
    assert markdown.startswith("## `momentum_trend` — 2 members as of 2026-06-30")
    assert "| symbol | name | rank | score | streak | present | stayed | first_seen |" in markdown
    written = tmp_path / "h.json"
    assert main([*base, "--format", "json", "--out", str(written)]) == 0
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert {"preset", "asof", "archived_sessions", "window", "members_per_session", "members",
            "exited", "new_today", "note"} <= set(payload)
    assert payload["asof"] == END and payload["archived_sessions"] == 2
    assert payload["members_per_session"] == {str(cf.CALENDAR[-2].date()): 2, END: 2}
    assert [row["symbol"] for row in payload["members"]] == frame[frame["asof"] == END]["symbol"].tolist()
    assert payload["exited"] == [] and payload["note"] == ""
    assert main([*base, "--out", str(tmp_path / "h.csv")]) == 0              # the table view
    assert list(pd.read_csv(tmp_path / "h.csv").columns)[:3] == ["symbol", "name", "rank"]
    # nothing archived: said, with the way out
    with pytest.raises(SystemExit) as exc:
        main(["history", "nobody", "--history-dir", str(archive), *_common(store_dir)])
    assert "no snapshots of 'nobody'" in str(exc.value) and "--save-history" in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        main([*base, "--days", "0"])
    assert "--days must be 1 or more" in str(exc.value)


def test_diff_fail_on_change_exits_2_only_when_the_list_moved(store_dir, capsys):
    _user_presets().joinpath("mover.yaml").write_text(MOVER_YAML, encoding="utf-8")
    engine = ScreenEngine(CNDataStore(store_dir, warmup_rows=cf.SESSIONS, use_cache=False))
    date, diff = _moving_date(engine)
    quiet = ["diff", "value_dividend", *_common(store_dir), "--as-of", END, "--fail-on-change"]
    assert main(quiet) == 0                                   # nothing entered or exited
    moved = ["diff", "mover", *_common(store_dir), "--as-of", date]
    assert main(moved) == 0 and main([*moved, "--fail-on-change"]) == EXIT_ALERT
    assert "entered (+1)" in capsys.readouterr().out          # the ordinary output, either way


DIFF_EVENT = {"event": "diff", "preset": "value_dividend", "asof": "2026-11-25",
              "prev": "2026-11-24", "ranked": True, "retained": 42, "previous_members": 45,
              "entered": [{"rank": "5", "symbol": "SH600377", "name": "宁沪高速"},
                          {"rank": "9", "symbol": "SZ000858", "name": "五粮液"}],
              "exited": [{"rank": "12", "symbol": "SH600461", "name": "洪城环境"}],
              "movers": [{"symbol": "A", "name": "甲", "change": "5"},
                         {"symbol": "B", "name": "乙", "change": "-3"}]}
DIFF_TEXT = ("qf-screen diff: value_dividend — 2026-11-25 vs 2026-11-24\n"
             "entered +2: #5 SH600377 宁沪高速; #9 SZ000858 五粮液\n"
             "exited −1: #12 SH600461 洪城环境\n"
             "retained 42 (93%)\n"
             "rank movers (2): ↑+5 A 甲; ↓−3 B 乙")


def test_webhook_payloads_follow_each_platforms_contract():
    assert notify.build_payload("json", DIFF_EVENT) == DIFF_EVENT     # the command's own payload
    assert notify.message(DIFF_EVENT) == DIFF_TEXT
    for fmt, expected in (("slack", {"text": DIFF_TEXT}),
                          ("dingtalk", {"msgtype": "text", "text": {"content": DIFF_TEXT}}),
                          ("feishu", {"msg_type": "text", "content": {"text": DIFF_TEXT}})):
        assert notify.build_payload(fmt, DIFF_EVENT) == expected, fmt
    # 飞书 签名校验: HMAC-SHA256 keyed by "<timestamp>\n<secret>" over nothing, Base64, seconds
    signed = notify.build_payload("feishu", DIFF_EVENT, secret="s3", now=1_700_000_000)
    expected = base64.b64encode(hmac.new(b"1700000000\ns3", digestmod=hashlib.sha256).digest())
    assert signed["timestamp"] == "1700000000" and signed["sign"] == expected.decode()
    assert signed["content"] == {"text": DIFF_TEXT}
    # 钉钉 加签: HMAC-SHA256 keyed by the secret over "<ms>\n<secret>", Base64, URL-encoded
    stamp = 1_700_000_000_123
    code = hmac.new(b"SEC", f"{stamp}\nSEC".encode(), hashlib.sha256).digest()
    expected = urllib.parse.quote_plus(base64.b64encode(code).decode())
    assert notify.dingtalk_sign(stamp, "SEC") == expected
    address, body, headers = notify.build_request(
        "dingtalk", "https://example.invalid/robot/send?access_token=T", DIFF_EVENT,
        secret="SEC", now=stamp / 1000)
    assert address == ("https://example.invalid/robot/send?access_token=T"
                       f"&timestamp={stamp}&sign={expected}")
    assert json.loads(body) == {"msgtype": "text", "text": {"content": DIFF_TEXT}}
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["User-Agent"].startswith("qf-screen/")
    bare, _body, _headers = notify.build_request("dingtalk", "https://example.invalid/x",
                                                 DIFF_EVENT, secret="SEC", now=stamp / 1000)
    assert "?timestamp=" in bare                                   # no query yet: a "?" is added
    plain, body, _headers = notify.build_request("slack", "  https://example.invalid/hook  ",
                                                 DIFF_EVENT)
    assert plain == "https://example.invalid/hook" and json.loads(body) == {"text": DIFF_TEXT}
    for fmt, what in (("slack", "url"), ("feishu", "format")):
        with pytest.raises(notify.WebhookError) as exc:
            if what == "url":
                notify.build_request(fmt, "  ", DIFF_EVENT)
            else:
                notify.build_request("fax", "https://example.invalid/hook", DIFF_EVENT)
        assert ("no webhook URL" if what == "url" else "unknown webhook format") in str(exc.value)
    with pytest.raises(notify.WebhookError):
        notify.build_payload("fax", DIFF_EVENT)
    with pytest.raises(notify.WebhookError):
        notify.message({"event": "seance"})
    # a 200 can still be a refusal: each platform reports it its own way
    assert "refused" in notify._platform_error("feishu", '{"code": 19021, "msg": "sign match fail"}')
    assert notify._platform_error("feishu", '{"code": 0, "msg": "success"}') is None
    assert "refused" in notify._platform_error("dingtalk", '{"errcode": 310000, "errmsg": "keywords"}')
    assert notify._platform_error("dingtalk", '{"errcode": 0, "errmsg": "ok"}') is None
    assert "refused" in notify._platform_error("slack", '{"ok": false, "error": "no_hook"}')
    assert notify._platform_error("slack", "ok") is None                       # not even JSON
    # long lists are shortened in the text; the JSON payload keeps every name
    many = {**DIFF_EVENT, "entered": [{"symbol": f"S{i}", "name": f"n{i}"} for i in range(15)]}
    assert "… +5 more" in notify.message(many)
    assert len(notify.build_payload("json", many)["entered"]) == 15
    assert "entered: none" in notify.message({**DIFF_EVENT, "entered": [], "movers": []})


def test_webhook_watch_and_report_messages(store_dir):
    engine = ScreenEngine(CNDataStore(store_dir, warmup_rows=cf.SESSIONS, use_cache=False))
    from qf_screen.ui.briefing import daily_report
    from qf_screen.ui.watch import watch, watch_payload

    report = watch(engine, ["SH600001", "SH600005"], preset="momentum_trend", asof=END)
    text = notify.message({"event": "watch", **watch_payload(report)})
    assert text.startswith("qf-screen watch — 2026-06-30 · 2 symbols · ")
    assert "(preset momentum_trend)" in text
    assert "MEDIUM SH600005 *ST宏达: preset_exit — out of momentum_trend: fails" in text
    quiet = watch(engine, ["SH600001"], asof=END)
    text = notify.message({"event": "watch", **watch_payload(quiet)})
    assert "0 alerts" in text and "no alerts: SH600001 公司600001" in text
    _markdown, data = daily_report(engine, [ScreenSpec.from_yaml("value_dividend")], asof=END)
    data["warnings"] = ["w1", "w2"]
    text = notify.message({"event": "report", **data})
    assert text.startswith("qf-screen daily briefing — 2026-06-30\nvalue_dividend: 1 members (+0 / −0)")
    assert text.endswith("2 data warning(s)")


def test_webhook_delivery_never_raises_and_reports_what_came_back(recorder, monkeypatch):
    delivery = notify.send("slack", DIFF_EVENT, url="https://example.invalid/hook")
    assert delivery.ok and delivery.status == 200 and delivery.url == "https://example.invalid/hook"
    sent = recorder.calls[-1]
    assert sent["body"] == {"text": DIFF_TEXT} and sent["timeout"] == notify.TIMEOUT
    for fmt, body, ok in (("feishu", '{"code": 19021, "msg": "sign match fail"}', False),
                          ("feishu", '{"code": 0, "msg": "success"}', True),
                          ("dingtalk", '{"errcode": 0, "errmsg": "ok"}', True),
                          ("slack", '{"ok": false, "error": "no_hook"}', False),
                          ("json", "{}", True)):
        recorder.body = body
        delivery = notify.send(fmt, DIFF_EVENT, url="https://example.invalid/hook")
        assert delivery.ok is ok, (fmt, body)
        assert ("refused" in delivery.detail) is not ok
    recorder.status, recorder.body = 503, "service unavailable"
    delivery = notify.send("slack", DIFF_EVENT, url="https://example.invalid/hook")
    assert not delivery.ok and delivery.status == 503 and delivery.detail == "HTTP 503 service unavailable"
    recorder.status = 200
    for error in (notify.WebhookError("URLError: name or service not known"),
                  urllib.error.URLError("no route"), TimeoutError("timed out")):
        recorder.error = error                 # the last two are not even WebhookError
        delivery = notify.send("slack", DIFF_EVENT, url="https://example.invalid/hook")
        assert not delivery.ok and delivery.status == 0, error
        assert "example.invalid" in delivery.detail and type(error).__name__ in delivery.detail
    recorder.error = None
    # the URL and the secret come from the environment -- but only once a format was asked for
    monkeypatch.setenv(notify.URL_ENV, "https://example.invalid/from-env")
    monkeypatch.setenv(notify.SECRET_ENV, "env-secret")
    assert notify.send("feishu", DIFF_EVENT).ok
    assert recorder.calls[-1]["url"] == "https://example.invalid/from-env"
    assert "sign" in recorder.calls[-1]["body"] and "timestamp" in recorder.calls[-1]["body"]
    assert notify.send("dingtalk", DIFF_EVENT, secret="flag").ok
    assert "sign=" in recorder.calls[-1]["url"]                 # 钉钉 signs the URL
    monkeypatch.delenv(notify.URL_ENV)
    calls = len(recorder.calls)
    delivery = notify.send("slack", DIFF_EVENT)                 # nothing to post to
    assert not delivery.ok and "no webhook URL" in delivery.detail
    assert len(recorder.calls) == calls
    with pytest.raises(notify.WebhookError):
        notify.send("fax", DIFF_EVENT, url="https://example.invalid/hook")


def test_post_is_the_one_network_call(monkeypatch):
    monkeypatch.setattr(notify, "post", notify.post.original)   # the real one, under test
    seen = {}

    class _Answer:
        status = 201

        def __init__(self, body):
            self.body = body

        def read(self, size):
            return self.body[:size]

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def urlopen(request, timeout):
        seen.update(url=request.full_url, data=request.data, method=request.get_method(),
                    headers=dict(request.header_items()), timeout=timeout)
        return _Answer(b'{"ok": true}')

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    assert notify.post("https://example.invalid/x", b"{}", {"A": "b"}, timeout=3) == (201, '{"ok": true}')
    assert seen == {"url": "https://example.invalid/x", "data": b"{}", "method": "POST",
                    "headers": {"A": "b"}, "timeout": 3}
    refused = urllib.error.HTTPError("https://example.invalid/x", 404, "not found", {}, None)
    def refusing(request, timeout):
        raise refused

    monkeypatch.setattr(notify.urllib.request, "urlopen", refusing)
    assert notify.post("https://example.invalid/x", b"{}", {}) == (404, "not found")
    for failure in (urllib.error.URLError("no route"), TimeoutError(), OSError("reset")):
        monkeypatch.setattr(notify.urllib.request, "urlopen",
                            lambda request, timeout, failure=failure: (_ for _ in ()).throw(failure))
        with pytest.raises(notify.WebhookError) as exc:
            notify.post("https://example.invalid/x", b"{}", {})
        assert type(failure).__name__ in str(exc.value)


def test_cli_webhooks_are_opt_in_and_a_failed_push_is_exit_3(store_dir, tmp_path, capsys,
                                                              recorder, monkeypatch):
    monkeypatch.setenv(notify.URL_ENV, "https://example.invalid/hook")
    # the variable on its own pushes nothing -- a profile must not turn every run into a call
    assert main(["diff", "value_dividend", *_common(store_dir), "--as-of", END]) == 0
    assert main(["watch", *_common(store_dir), "--as-of", END, "--symbols", "SH600001"]) == 0
    assert main(["report", *_common(store_dir), "--as-of", END, "--out", str(tmp_path / "r.md")]) == 0
    capsys.readouterr()
    assert recorder.calls == []

    # --webhook-format is the switch
    assert main(["diff", "value_dividend", *_common(store_dir), "--as-of", END,
                 "--webhook-format", "json"]) == 0
    sent = recorder.calls[-1]
    assert sent["url"] == "https://example.invalid/hook"
    assert sent["body"]["event"] == "diff" and sent["body"]["preset"] == "value_dividend"
    assert "generated_at" in sent["body"] and sent["body"]["asof"] == END
    assert {"entered", "exited", "movers", "retained"} <= set(sent["body"])
    assert "entered (" in capsys.readouterr().out                 # the local output, as before
    assert main(["watch", *_common(store_dir), "--as-of", END, "--symbols", "SH600001,SH600005",
                 "--webhook-format", "feishu", "--webhook-url", "https://example.invalid/other"]) == 0
    body = recorder.calls[-1]["body"]
    assert recorder.calls[-1]["url"] == "https://example.invalid/other"
    assert body["msg_type"] == "text" and body["content"]["text"].startswith("qf-screen watch")
    assert "sign" not in body                                   # no secret given
    monkeypatch.setenv(notify.SECRET_ENV, "env-secret")
    assert main(["report", *_common(store_dir), "--as-of", END, "--out", str(tmp_path / "r.md"),
                 "--presets", "value_dividend", "--webhook-format", "slack"]) == 0
    assert recorder.calls[-1]["body"]["text"].startswith("qf-screen daily briefing — 2026-06-30\n"
                                                          "value_dividend: 1 members")
    assert main(["diff", "value_dividend", *_common(store_dir), "--as-of", END, "--webhook-format",
                 "dingtalk"]) == 0                              # the env secret signs the URL
    assert "sign=" in recorder.calls[-1]["url"] and recorder.calls[-1]["body"]["msgtype"] == "text"

    # a push that fails is exit code 3 and a line on stderr; the local output still appears
    _user_presets().joinpath("mover.yaml").write_text(MOVER_YAML, encoding="utf-8")
    engine = ScreenEngine(CNDataStore(store_dir, warmup_rows=cf.SESSIONS, use_cache=False))
    date, _diff = _moving_date(engine)
    recorder.error = urllib.error.URLError("name or service not known")
    moved = ["diff", "mover", *_common(store_dir), "--as-of", date, "--fail-on-change",
             "--webhook-format", "slack"]
    assert main(moved) == EXIT_WEBHOOK                          # undelivered outranks "changed"
    captured = capsys.readouterr()
    assert "webhook (slack) NOT delivered" in captured.err and "entered (+1)" in captured.out
    recorder.error = None
    assert main(moved) == EXIT_ALERT                            # delivered: the alert code stands
    assert "webhook: slack delivered (HTTP 200)" in capsys.readouterr().err
    recorder.body = '{"ok": false, "error": "no_hook"}'         # a 200 that is a refusal
    assert main(moved) == EXIT_WEBHOOK
    recorder.body = '{"ok": true}'
    # no URL anywhere: an error, not a silent skip
    monkeypatch.delenv(notify.URL_ENV)
    calls = len(recorder.calls)
    assert main(["diff", "value_dividend", *_common(store_dir), "--as-of", END,
                 "--webhook-format", "json"]) == EXIT_WEBHOOK
    assert "no webhook URL" in capsys.readouterr().err and len(recorder.calls) == calls


def test_webhook_is_delivered_over_a_real_socket(monkeypatch):
    """:func:`notify.post` end to end -- over loopback, to a server the test
    owns: the request line, the headers and the JSON body arrive as built,
    for every format, and a refusal comes back as one."""
    import http.server
    import threading

    received = []
    answer = {"status": 200, "body": b'{"ok": true}'}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append({
                "path": self.path, "body": json.loads(self.rfile.read(length)),
                "type": self.headers.get("Content-Type"),
                "agent": self.headers.get("User-Agent", "")})
            self.send_response(answer["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(answer["body"])

        def log_message(self, *_args):          # keep the test output clean
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/hook"
    monkeypatch.setattr(notify, "post", notify.post.original)      # the real one, under test
    for variable in ("no_proxy", "NO_PROXY"):                       # never through a proxy
        monkeypatch.setenv(variable, "127.0.0.1,localhost")
    try:
        for fmt in notify.FORMATS:
            delivery = notify.send(fmt, DIFF_EVENT, url=url)
            assert delivery.ok and delivery.status == 200, (fmt, delivery)
            assert delivery.detail == '{"ok": true}'
        assert [item["path"] for item in received] == ["/hook"] * 4
        assert all(item["type"] == "application/json; charset=utf-8" for item in received)
        assert all(item["agent"].startswith("qf-screen/") for item in received)
        assert [item["body"] for item in received] == [
            notify.build_payload(fmt, DIFF_EVENT) for fmt in notify.FORMATS]
        answer.update(status=404, body=b'{"message": "no such hook"}')   # said no
        delivery = notify.send("slack", DIFF_EVENT, url=url)
        assert not delivery.ok and delivery.status == 404
        assert delivery.detail == 'HTTP 404 {"message": "no such hook"}'
        assert len(received) == 5 and notify.post(url, b"{}", {}, timeout=5) == (
            404, '{"message": "no such hook"}')
    finally:
        server.shutdown()
        server.server_close()


# ==================== Todo 5: the cache garbage collector ===================
NOW = 1_800_000_000.0            # a fixed clock for the collector
DAY = 86400.0
#: the default universe's current source census in the cache trees below
CURRENT = ("F1", "abc")


def _version_tag() -> str:
    import qf_screen

    return "v" + qf_screen.__version__.replace(".", "")


def _write_store(directory, fingerprint, symbols=("S001", "S002")):
    """A merged-store cache directory: two wide tables, the provenance and
    a ``meta.json`` naming ``fingerprint`` (``None``: no meta at all)."""
    frame = pd.DataFrame({"S001": [1.0, 2.0], "S002": [3.0, 4.0]},
                         index=pd.DatetimeIndex(pd.bdate_range("2026-01-01", periods=2),
                                                name="date"))
    directory.mkdir(parents=True)
    for name in ("wide_close.parquet", "wide_volume.parquet", "provenance.parquet"):
        write_cached_table(directory / name, "x", frame)
    if fingerprint is not None:
        (directory / "meta.json").write_text(json.dumps({
            "fingerprint": fingerprint, "symbols": list(symbols),
            "calendar": [str(day.date()) for day in frame.index]}), encoding="utf-8")
    return directory


def _extra_caches(cache, key="abc"):
    """Into an existing cache directory: a validation window whose sources
    moved on, one that cannot be read, and one built for other symbols.
    ``key`` is the symbol-set key the default universe's caches carry."""
    version = _version_tag()
    return ({"stale": _write_store(cache / f"store_hist2026-01-01-2026-02-01_{key}_{version}", "F0"),
             "broken": _write_store(cache / f"store_hist2026-03-01-2026-03-31_{key}_{version}",
                                    None),
             "custom": _write_store(cache / f"store_tail600_fff_{version}", "X", ("S009",))})


def _cache_tree(root):
    """A cache directory with one entry of every situation the collector
    tells apart -- and nothing else under ``root`` (no store, so no source
    census).  Returns ``{situation: directory}`` for the merged-store
    caches."""
    cache = root / "cn/qf_screen_cache"
    frame = pd.DataFrame({"S001": [1.0]}, index=pd.DatetimeIndex(
        pd.bdate_range("2026-01-01", periods=1), name="date"))
    made = {"daily": _write_store(cache / f"store_tail600_abc_{_version_tag()}", "F1"),
            "older version's": _write_store(cache / "store_tail600_abc_v010", "F0")}
    made.update(_extra_caches(cache))
    for window, key in (("tail600_abc", "feature_a"), ("tail600_abc", "alt_step_x"),
                        ("hist2026-01-01-2026-02-01_abc", "feature_b"),
                        ("hist2026-05-01-2026-06-30_abc", "feature_c"),
                        ("tail600_fff", "feature_d")):
        write_cached_table(cache / "tables" / window / f"{key}.parquet", "fp", frame)
    write_cached_table(cache / "universe_cn.parquet", "fp", frame)
    (cache / "universe_cn.meta.json").write_text("{}", encoding="utf-8")
    write_cached_table(cache / "finance_payload_abc.parquet", "fp", frame)
    write_cached_table(cache / "finance_step_old.parquet", "fp", frame)   # an old layout's
    (cache / "tables/tail600_abc/feature_a.parquet.tmp").write_bytes(b"x" * 3)
    (root / "cn/ak_live").mkdir(parents=True)
    (root / "cn/ak_live/spot.csv").write_bytes(b"x" * 30)
    return made


def _names(rows):
    return {(row["kind"], str(row["name"])) for row in rows}


def test_cache_inventory_records_when_an_entry_was_last_read(tmp_path):
    made = _cache_tree(tmp_path)
    rows = {(row["kind"], str(row["name"])): row for row in cache_inventory(tmp_path, "cn")}
    assert {"atime", "mtime", "age_days"} <= set(rows["universe", "universe_cn"])
    written, read = NOW - 40 * DAY, NOW - DAY
    for name in ("universe_cn.parquet", "universe_cn.parquet.meta.txt"):
        os.utime(tmp_path / "cn/qf_screen_cache" / name, (read, written))
    os.utime(made["daily"] / "wide_close.parquet", (read, written))
    rows = {(row["kind"], str(row["name"])): row for row in cache_inventory(tmp_path, "cn")}
    assert entry_last_use(rows["universe", "universe_cn"]) == read          # read after writing
    assert entry_last_use(rows["store", made["daily"].name]) == read         # its newest file
    fresh = rows["feature", "tail600_abc/feature_a"]
    assert entry_last_use(fresh) == pytest.approx(pd.Timestamp(fresh["mtime"]).timestamp())
    assert entry_last_use({"mtime": pd.NaT, "atime": pd.NaT}) == 0.0


def test_cache_gc_stale_lets_go_of_what_no_run_will_read(tmp_path):
    made = _cache_tree(tmp_path)
    everything = _names(cache_inventory(tmp_path, "cn"))
    plan = cache_gc_plan(tmp_path, "cn", stale=True, current=CURRENT)
    doomed = _names(plan["remove"])
    detail = {(row["kind"], str(row["name"])): row["detail"] for row in plan["remove"]}
    assert doomed | _names(plan["keep"]) == everything and not doomed & _names(plan["keep"])
    assert set(detail) == {
        ("obsolete", "store_tail600_abc_v010"),
        ("obsolete", "finance_step_old"),
        ("tmp", "tail600_abc/feature_a.parquet.tmp"),
        ("history", made["stale"].name), ("history", made["broken"].name),
        ("feature", "hist2026-01-01-2026-02-01_abc/feature_b")}
    assert detail["obsolete", "store_tail600_abc_v010"] == "an older version's layout -- never read"
    assert detail["history", made["stale"].name] == "a source file changed since it was built"
    assert "incomplete" in detail["history", made["broken"].name]
    assert detail["feature", "hist2026-01-01-2026-02-01_abc/feature_b"] == \
        "its window's store cache is stale"
    assert plan["counts"] == {"stale": 6, "old": 0, "lru": 0}
    assert plan["after_bytes"] == plan["total_bytes"] - sum(
        int(row["bytes"]) for row in plan["remove"])
    # the active window, a cache built for other symbols, a window with no store
    # cache left, the census, the statement payload and the live cache all stay
    kept = _names(plan["keep"])
    assert {("store", made["daily"].name), ("store", made["custom"].name),
            ("feature", "tail600_abc/feature_a"), ("alt", "tail600_abc/alt_step_x"),
            ("feature", "tail600_fff/feature_d"), ("feature", "hist2026-05-01-2026-06-30_abc/feature_c"),
            ("universe", "universe_cn"), ("statement", "finance_payload_abc"),
            ("live", "live:spot")} <= kept
    assert plan["notes"] == ["1 merged-store cache(s) were built for another symbol set and "
                             "were not judged"]
    # the daily window's tables stay although an older version's cache of it is
    # stale: a window is as good as its best cache
    assert ("feature", "tail600_abc/feature_a") in kept
    # a kind narrows what may go; what a window IS does not depend on that
    features = cache_gc_plan(tmp_path, "cn", ["feature"], stale=True, current=CURRENT)
    assert _names(features["remove"]) == {("feature", "hist2026-01-01-2026-02-01_abc/feature_b")}
    assert _names(features["keep"]) == {("feature", "tail600_abc/feature_a"),
                                        ("feature", "tail600_fff/feature_d"),
                                        ("feature", "hist2026-05-01-2026-06-30_abc/feature_c")}
    # without a census nothing is judged stale: only what nothing can read goes
    blind = cache_gc_plan(tmp_path, "cn", stale=True)
    assert _names(blind["remove"]) == {
        ("obsolete", "store_tail600_abc_v010"), ("obsolete", "finance_step_old"),
        ("tmp", "tail600_abc/feature_a.parquet.tmp"), ("history", made["broken"].name)}
    assert any("no current source census" in note for note in blind["notes"])
    # and with no selector at all nothing is picked
    idle = cache_gc_plan(tmp_path, "cn")
    assert idle["remove"] == [] and _names(idle["keep"]) == everything and idle["notes"] == []


def test_cache_remove_takes_the_plan_and_nothing_else(tmp_path):
    made = _cache_tree(tmp_path)
    cache = tmp_path / "cn/qf_screen_cache"
    plan = cache_gc_plan(tmp_path, "cn", stale=True, current=CURRENT)
    doomed = _names(plan["remove"])
    size = sum(int(row["bytes"]) for row in plan["remove"])
    removed, freed = cache_remove(tmp_path, "cn", [(r["kind"], r["name"]) for r in plan["remove"]])
    assert (removed, freed) == (6, size)
    assert _names(cache_inventory(tmp_path, "cn")) == _names(plan["keep"])
    assert made["daily"].exists() and made["custom"].exists()
    assert not made["stale"].exists() and not made["broken"].exists()
    assert not (cache / "store_tail600_abc_v010").exists()
    assert not (cache / "finance_step_old.parquet").exists()
    assert not (cache / "tables/hist2026-01-01-2026-02-01_abc").exists()   # emptied with its tables
    assert (cache / "tables/tail600_abc/feature_a.parquet").exists()
    assert (cache / "tables/tail600_fff/feature_d.parquet").exists()
    assert (cache / "universe_cn.parquet").exists() and (tmp_path / "cn/ak_live/spot.csv").exists()
    assert cache_remove(tmp_path, "cn", list(doomed)) == (0, 0)            # idempotent
    left = cache_inventory(tmp_path, "cn")
    assert cache_clear(tmp_path, "cn", ["all"]) == (len(left), sum(int(r["bytes"]) for r in left))
    assert not list(cache.iterdir()) and not list((tmp_path / "cn/ak_live").iterdir())


def test_cache_gc_older_than_reads_the_last_use_and_max_gb_evicts_in_order(tmp_path):
    made = _cache_tree(tmp_path)
    cache = tmp_path / "cn/qf_screen_cache"

    def files_of(name):
        """Every file of one entry: a store directory's, or a table's with its
        sidecars (``<key>.parquet.meta.txt``, ``universe_cn.meta.json``)."""
        if (cache / name).is_dir():
            return [inner for inner in (cache / name).iterdir() if inner.is_file()]
        if "/" in name:                           # a derived table: tables/<window>/<key>
            folder, key = cache / "tables" / name.split("/", 1)[0], name.split("/", 1)[1]
        else:
            folder, key = cache, name
        return [file for file in folder.iterdir()
                if file.is_file() and (file.name == key or file.name.startswith(key + "."))]

    def set_times(name, written, read):
        for file in files_of(name):
            os.utime(file, (read, written))

    now = time.time() + 2 * DAY                     # two days after everything was written
    set_times("universe_cn", now - 40 * DAY, now - 40 * DAY)       # written and read long ago
    set_times("finance_payload_abc", now - 40 * DAY, now - 2 / 24 * DAY)   # old, read 2 h ago
    set_times("tail600_fff/feature_d", now - 10 * DAY, now - 10 * DAY)
    rows = {(row["kind"], str(row["name"])): row for row in cache_inventory(tmp_path, "cn")}

    def uses(rows_):
        return [entry_last_use(row) for row in rows_]

    plan = cache_gc_plan(tmp_path, "cn", older_than=30, now=now)
    assert _names(plan["remove"]) == {("universe", "universe_cn")}
    assert plan["remove"][0]["detail"] == "not used for 40 days" and plan["notes"] == []
    assert plan["counts"] == {"stale": 0, "old": 1, "lru": 0}
    # an old entry that was READ yesterday is in use: only what nothing read for 5 days goes
    plan = cache_gc_plan(tmp_path, "cn", older_than=5, now=now)
    assert _names(plan["remove"]) == {("universe", "universe_cn"),
                                      ("feature", "tail600_fff/feature_d")}
    assert {row["detail"] for row in plan["remove"]} == {"not used for 40 days",
                                                          "not used for 10 days"}
    # under a day the idle time reads in hours -- and two hours is younger than
    # a day and a half, so the recently read entry is the one that then survives
    detail = {str(row["name"]): row["detail"] for row in cache_gc_plan(
        tmp_path, "cn", older_than=0.01, now=now)["remove"]}
    assert detail["finance_payload_abc"] == "not used for 2 hours"
    assert detail["tail600_abc/feature_a"] == "not used for 2 days"
    day_and_half = cache_gc_plan(tmp_path, "cn", older_than=1.5, now=now)
    assert set(detail) - {"finance_payload_abc"} == {str(row["name"]) for row in day_and_half["remove"]}
    assert ("statement", "finance_payload_abc") in _names(day_and_half["keep"])

    # --max-gb: nothing to do while it fits; otherwise the least recently used go
    total = cache_gc_plan(tmp_path, "cn", now=now)["total_bytes"]
    smallest = min(int(row["bytes"]) for row in rows.values())

    def as_gb(size):                                  # a budget of just over ``size`` bytes
        return (size + 1) / 1024 ** 3
    plan = cache_gc_plan(tmp_path, "cn", max_gb=as_gb(total), now=now)
    assert plan["remove"] == [] and plan["after_bytes"] == total
    plan = cache_gc_plan(tmp_path, "cn", max_gb=(total - smallest - 1) / 1024 ** 3, now=now)
    assert _names(plan["remove"]) == {("universe", "universe_cn")}        # the oldest entry
    assert plan["remove"][0]["reason"] == "lru" and "to fit" in plan["remove"][0]["detail"]
    assert plan["after_bytes"] == total - int(rows["universe", "universe_cn"]["bytes"])
    half = cache_gc_plan(tmp_path, "cn", max_gb=total / 2 / 1024 ** 3, now=now)
    assert 0 < half["after_bytes"] <= total / 2
    assert half["counts"] == {"stale": 0, "old": 0, "lru": len(half["remove"])}
    evicted = [row for row in half["remove"] if row["reason"] == "lru"]
    picked = [str(row["name"]) for row in sorted(evicted, key=lambda row: row["order"])]
    assert picked[:2] == ["universe_cn", "tail600_fff/feature_d"]   # the oldest go first
    assert "finance_payload_abc" not in picked                     # ... the newest read stays
    assert uses(sorted(evicted, key=lambda row: row["order"])) == sorted(uses(evicted))
    assert [row["reason"] for row in half["remove"]] == sorted(
        (row["reason"] for row in half["remove"]), key=GC_REASONS.index)   # listed by reason
    # a budget of about a byte empties the cache, oldest first
    tiny = cache_gc_plan(tmp_path, "cn", max_gb=1e-15, now=now)
    assert _names(tiny["remove"]) == set(rows) and tiny["after_bytes"] == 0
    # the selectors combine: what is stale goes first and is counted once; what
    # has been idle goes next; the budget is then met from what is left
    both = cache_gc_plan(tmp_path, "cn", stale=True, older_than=5, max_gb=1e-15,
                         current=CURRENT, now=now)
    reasons = {(row["kind"], str(row["name"])): row["reason"] for row in both["remove"]}
    assert reasons["obsolete", "store_tail600_abc_v010"] == "stale"        # never "old" or "lru"
    assert reasons["history", made["stale"].name] == "stale"
    assert reasons["universe", "universe_cn"] == "old"
    assert reasons["feature", "tail600_fff/feature_d"] == "old"
    assert reasons["statement", "finance_payload_abc"] == "lru"           # it was only read
    assert both["counts"] == {"stale": 6, "old": 2, "lru": len(everything := rows) - 8}
    assert both["after_bytes"] == 0 and made["daily"].exists()            # a plan deletes nothing
    del everything
    # a kind scopes every selector
    features = cache_gc_plan(tmp_path, "cn", ["feature"], stale=True, older_than=5,
                             max_gb=1e-15, current=CURRENT, now=now)
    assert _names(features["remove"]) == {key for key in rows if key[0] == "feature"}
    assert features["counts"] == {"stale": 1, "old": 1, "lru": 2}   # the alt table is no feature
    assert features["keep"] == [] and len(rows) > 6


def test_cache_prune_on_a_real_store(tmp_path):
    """The synthetic store, cached: a source file changes, and the collector
    lets go of exactly what the next run would have rebuilt anyway."""
    root = _tree(tmp_path / "tree")
    warmed = cache_ops.cache_warm(root, presets=["value_dividend"], max_workers=1)
    window, cache = warmed["window"], root / "cn/qf_screen_cache"
    assert (cache / f"store_{window}_{_version_tag()}").is_dir() and (cache / "tables" / window).is_dir()
    plan = cache_ops.cache_prune(root, stale=True, dry_run=True)
    assert plan["remove"] == [] and plan["notes"] == []           # everything is fresh
    assert plan["total_bytes"] == sum(int(row["bytes"]) for row in cache_inventory(root, "cn"))
    source = root / "cn/equity/SZ000002/ak/basic.csv"
    stamp = source.stat().st_mtime + 5
    os.utime(source, (stamp, stamp))                             # the daily data moved on
    plan = cache_ops.cache_prune(root, stale=True, dry_run=True)
    doomed = _names(plan["remove"])
    assert ("store", f"store_{window}_{_version_tag()}") in doomed
    assert ("feature", f"{window}/feature_amount_ma20") in doomed
    assert ("statement", f"{window}/finance_step_roe") in doomed
    assert ("dividend", f"{window}/div_yield_ttm_events") in doomed
    # ... the store cache and its window's tables, nothing else
    assert all(name.startswith(window + "/") for _kind, name in doomed - {
        ("store", f"store_{window}_{_version_tag()}")})
    assert not [key for key in doomed if key[1] in ("universe_cn",) or key[1].startswith("finance_payload_")]
    done = cache_ops.cache_prune(root, stale=True)
    assert done["removed"] == len(plan["remove"]) and done["freed"] == sum(
        int(row["bytes"]) for row in plan["remove"])
    assert not (cache / "tables" / window).exists() and not (cache / f"store_{window}_{_version_tag()}").exists()
    payload = f"finance_payload_{CNDataStore.symbols_key(list(build_universe(root).symbols))}.parquet"
    assert (cache / "universe_cn.parquet").exists() and (cache / payload).exists()
    assert cache_ops.cache_prune(root, stale=True)["removed"] == 0   # nothing left to call stale
    # what was let go comes back on the next run
    cache_ops.cache_warm(root, presets=["value_dividend"], max_workers=1)
    assert cache_ops.cache_status(root)["stores"][0]["fresh"] is True
    # an older version's cache goes; its window's tables, still valid, stay
    (cache / f"store_{window}_{_version_tag()}").rename(cache / f"store_{window}_v010")
    plan = cache_ops.cache_prune(root, stale=True, dry_run=True)
    assert ("obsolete", f"store_{window}_v010") in _names(plan["remove"])
    assert not [key for key in _names(plan["remove"]) if key[1].startswith(window + "/")]
    cache_ops.cache_prune(root, stale=True)
    assert not (cache / f"store_{window}_v010").exists() and (cache / "tables" / window).is_dir()
    # the census cannot be had: the plan goes ahead without judging, and says so
    bare = tmp_path / "bare"
    _cache_tree(bare)
    plan = cache_ops.cache_prune(bare, stale=True, dry_run=True)
    assert len(plan["remove"]) == 4 and any("no current source census" in note for note in plan["notes"])


def test_cache_clear_cli_prunes(tmp_path, capsys):
    made = _cache_tree(tmp_path)
    cache = tmp_path / "cn/qf_screen_cache"
    base = ["cache", "clear", *_common(tmp_path)]
    assert main([*base, "--dry-run", "--stale"]) == 0
    text = capsys.readouterr().out
    # no store under this tree, so no census: the unreadable go, the rest is not judged
    assert re.search(r"^would remove 4 cache entries \([^)]*\) of \d+ \([^)]* -> [^)]*\): 4 stale under "
                     + re.escape(str(cache)), text)
    assert "store_tail600_abc_v010" in text and "an older version's layout -- never read" in text
    assert "an interrupted write" in text and "incomplete -- its meta or wide tables are missing" in text
    assert "note: no current source census" in text
    assert made["daily"].exists() and made["stale"].exists()       # a dry run deletes nothing
    assert main([*base, "--stale"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("removed 4 cache entries (") and "): 4 stale under " in text
    assert "everything rebuilds on demand" in text
    assert not made["broken"].exists() and made["stale"].exists() and made["daily"].exists()
    assert not (cache / "tables/tail600_abc/feature_a.parquet.tmp").exists()

    # on a real store the census is there: a changed source file makes room
    root = _tree(tmp_path / "real")
    cache_ops.cache_warm(root, presets=["momentum_trend"], max_workers=1)
    real = ["cache", "clear", "--data-dir", str(root), "--workers", "1"]
    source = root / "cn/equity/SH600003/ak/basic.csv"
    stamp = source.stat().st_mtime + 5
    os.utime(source, (stamp, stamp))
    key = CNDataStore.symbols_key(list(build_universe(root).symbols))
    extra = _extra_caches(root / "cn/qf_screen_cache", key)
    assert main([*real, "--dry-run", "--stale", "--what", "store", "--what", "history"]) == 0
    text = capsys.readouterr().out
    assert re.search(r"^would remove 3 cache entries ", text)   # the daily store, the stale
    assert text.count("a source file changed since it was built") == 2   # window, the broken one
    assert text.count("incomplete -- its meta or wide tables are missing") == 1
    assert not [line for line in text.splitlines() if "/feature_" in line or "/finance_step" in line]
    assert "note: 1 merged-store cache(s) were built for another symbol set" in text
    assert main([*real, "--stale", "--older-than", "30", "--max-gb", "0.000001"]) == 0
    text = capsys.readouterr().out
    assert "stale" in text and "not used for 30 days" not in text     # stale is counted once
    assert "least recently used" in text and "everything rebuilds on demand" in text
    assert not list((root / "cn/qf_screen_cache").iterdir())
    assert main([*real, "--stale"]) == 0                             # nothing left: fine
    text = capsys.readouterr().out
    assert text.startswith("removed 0 cache entries (0 B)") and "everything rebuilds" not in text
    # the flags are checked; the clear-by-kind is unchanged
    for flag, value, needle in (("--older-than", "soon", "is not a number of days"),
                                ("--older-than", "-1", "0 or more days"),
                                ("--older-than", "nan", "0 or more days"),
                                ("--max-gb", "0", "greater than 0"),
                                ("--max-gb", "-2", "greater than 0"),
                                ("--max-gb", "lots", "is not a size")):
        with pytest.raises(SystemExit):
            main([*base, flag, value, "--dry-run"])
        assert needle in capsys.readouterr().err
    kinds = tmp_path / "kinds"                                # a fresh tree: its two
    _cache_tree(kinds)                                        # old-layout entries ...
    by_kind = ["cache", "clear", "--data-dir", str(kinds), "--workers", "1", "--what", "obsolete"]
    assert main([*by_kind, "--dry-run"]) == 0
    assert "would remove 2 entries" in capsys.readouterr().out
    assert main(by_kind) == 0                                 # ... and nothing else
    assert "removed 2 cache entries" in capsys.readouterr().out
    assert (kinds / "cn/qf_screen_cache/universe_cn.parquet").exists()
    # status judges the validation windows too, and points at the pruning flags
    assert main(["cache", "status", *_common(root)]) == 0
    assert "prune: qf-screen cache clear --dry-run --stale | --older-than N | --max-gb N" \
        in capsys.readouterr().out
    window = cache_ops.cache_warm(root, presets=["momentum_trend"], max_workers=1)["window"]
    extra = _extra_caches(root / "cn/qf_screen_cache", key)
    assert main(["cache", "status", *_common(root), "--json"]) == 0
    stores = {row["name"]: row for row in json.loads(capsys.readouterr().out)["stores"]}
    assert stores[f"store_{window}_{_version_tag()}"]["fresh"] is True
    assert stores[extra["stale"].name]["fresh"] is False              # a history window, judged
    assert stores[extra["stale"].name]["kind"] == "history"
    assert stores[extra["broken"].name]["fresh"] is False
    assert stores[extra["custom"].name]["fresh"] is None
    assert stores[extra["custom"].name]["note"] == "built for a custom symbol set (not comparable)"
    assert made["daily"].exists()
