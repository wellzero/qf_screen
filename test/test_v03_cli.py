"""v0.3 CLI tests: the four bugs of the plan (``--top`` ignored, no ad-hoc
run, ``--format markdown --out`` dropped, ``--industry`` needing ``--live``)
and the new commands — ``diff``, ``inspect``, ``report``, ``compare``,
``cache`` — plus the overrides ``pit`` / ``validate`` now accept.

Hermetic: every command runs on the synthetic store (``--data-dir``), the
``--live`` paths on an injected fake akshare module.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

import conftest as cf
from qf_screen.cli import _industry_symbols, _local_industry_tags, _load_spec, main
from qf_screen.data import CNDataStore
from qf_screen.data.ak_provider import LiveAkClient

pytestmark = pytest.mark.unit

END = str(cf.END.date())


def _common(store_dir, extra=()):
    return ["--data-dir", str(store_dir), "--workers", "1", *extra]


def _tree(root):
    """A fresh synthetic store (the session fixture is shared; some tests
    need to touch its files)."""
    cf._company_list(root)
    for symbol, spec in cf.SYMBOLS.items():
        cf._symbol_files(root, symbol, spec)
    return root


# ------------------------------------------------------- bug 1: --top
LOOSE = ("name: loose\n"
         "universe: {boards: [all], exclude_st: false, min_listed_days: 0}\n"
         "filters:\n"
         "  - {name: cheap, expr: \"pe_ttm > 0\"}\n"
         "rank:\n"
         "  - {expr: \"pct_rank(earnings_yield)\", weight: 1.0}\n"
         "top_n: 1\n")
MOVER = ("name: mover\n"
         "universe: {boards: [all], exclude_st: false, min_listed_days: 0}\n"
         "filters:\n"
         "  - {name: hot, expr: \"ret_21d > 0.02\"}\n"
         "rank:\n"
         "  - {expr: \"pct_rank(ret_21d)\", weight: 1.0}\n"
         "top_n: 2\n")


def _preset(tmp_path, body, name):
    path = tmp_path / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _rows(text: str) -> list:
    return [line for line in text.splitlines() if line[:2] in ("SH", "SZ", "BJ")]


def test_top_replaces_the_preset_top_n(tmp_path, capsys):
    """``--top N`` used to be read under a name the parser never set
    (``args.top_n``), so it was silently ignored: a ``top_n: 1`` preset
    stayed at one member however large a ``--top`` it was given."""
    root = _tree(tmp_path / "tree")
    preset = _preset(tmp_path, LOOSE, "loose")
    base = _common(root) + ["--as-of", END, "--format", "csv"]

    assert main(["run", preset, *base]) == 0                 # the preset's own 1
    text = capsys.readouterr().out
    assert len(_rows(text)) == 1 and "loose: 1 members as of" in text

    assert main(["run", preset, *base, "--top", "3"]) == 0   # up from 1
    text = capsys.readouterr().out
    assert len(_rows(text)) == 3 and "loose: 3 members as of" in text

    assert main(["run", preset, *base, "--top", "0"]) == 0   # 0 = no limit
    text = capsys.readouterr().out
    shown = len(_rows(text))
    assert shown >= 3 and f"loose: {shown} members as of" in text


def test_top_summary_matches_the_table(store_dir, capsys):
    """``--top 1`` on a multi-member preset: the summary counts what is
    shown (it used to print the untruncated count)."""
    base = ["run", "momentum_trend", *_common(store_dir), "--as-of", END]
    assert main([*base, "--top", "1"]) == 0
    text = capsys.readouterr().out
    assert "momentum_trend: 1 members as of" in text
    assert "members (1):" in text


def test_load_spec_applies_top_and_max_per_industry(store_dir):
    import argparse

    store = CNDataStore(store_dir, warmup_rows=cf.SESSIONS, use_cache=False)
    args = argparse.Namespace(preset="momentum_trend", where=None, rank=None, boards=None,
                              industry=None, concept=None, top=50, max_per_industry=None,
                              live=False)
    spec = _load_spec(args, store)
    assert spec.top_n == 50                                  # the preset says 30
    args.top, args.max_per_industry = 0, 3
    spec = _load_spec(args, store)
    assert spec.top_n is None and spec.max_per_industry == 3
    args.top, args.max_per_industry = None, None
    assert _load_spec(args, store).top_n == 30               # untouched


# ------------------------------------------------- bug 2: ad-hoc run
def test_adhoc_run_needs_no_preset(store_dir, capsys):
    assert main(["run", *_common(store_dir), "--as-of", END,
                 "--where", "market_cap_bn > 0", "--where", "close_raw < 100",
                 "--rank", "pct_rank(earnings_yield)", "--top", "2"]) == 0
    text = capsys.readouterr().out
    assert "adhoc: 2 members as of" in text
    # the required hygiene block is the base, the --where filters follow it
    funnel = text.split("funnel:")[1].split("industry mix:")[0]
    for rule in ("traded", "price_floor", "liquidity", "where1", "where2"):
        assert rule in funnel, funnel
    frame = [line for line in text.splitlines() if line[:2] in ("SH", "SZ", "BJ")]
    assert len(frame) == 2


def test_adhoc_run_without_filters_is_an_error(store_dir):
    with pytest.raises(SystemExit) as exc:
        main(["run", *_common(store_dir)])
    assert "--where" in str(exc.value)


def test_adhoc_spec_is_transparent():
    from qf_screen.engine import ScreenSpec
    from qf_screen.presets import REQUIRED_HYGIENE

    spec = ScreenSpec.adhoc(where=["pe_ttm > 0"], rank="pct_rank(earnings_yield)")
    assert [f["name"] for f in spec.filters] == [rule for rule, _ in REQUIRED_HYGIENE] \
        + ["where1"]
    assert spec.universe.boards == ("all",) and spec.universe.exclude_st
    assert spec.universe.min_listed_days == 365 and spec.rank[0]["expr"] == \
        "pct_rank(earnings_yield)"


# --------------------------------------- bug 3: --format markdown --out
@pytest.mark.parametrize("fmt", ["markdown", "md"])
def test_markdown_out_writes_the_document(store_dir, tmp_path, capsys, fmt):
    out = tmp_path / "members.md"
    assert main(["run", "value_dividend", *_common(store_dir), "--as-of", END,
                 "--format", fmt, "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert f"wrote markdown to {out}" in text
    body = out.read_text(encoding="utf-8")
    assert body.startswith("## `value_dividend` — ")
    assert "### funnel" in body and "### members" in body
    assert "**industry mix:**" in body and "| symbol |" in body
    # a --format csv --out still writes the member CSV
    csv = tmp_path / "members.csv"
    assert main(["run", "value_dividend", *_common(store_dir), "--as-of", END,
                 "--out", str(csv)]) == 0
    assert set(pd.read_csv(csv).columns) >= {"symbol", "score"}


def test_run_json_payload_carries_mix_and_notes(store_dir, capsys):
    assert main(["run", "value_dividend", *_common(store_dir), "--as-of", END,
                 "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"members", "funnel", "industry_mix", "capped", "notes"} <= set(payload)
    assert sum(payload["industry_mix"].values()) == len(payload["members"])
    assert {"unknown", "na_pass"} <= set(payload["funnel"][0])


# ------------------------------------------- bug 4: offline --industry
def _symbols_row(text: str) -> int:
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("symbols"))
    return int(line.split()[1])


def test_industry_filters_offline_on_the_local_classification(store_dir, capsys):
    """Every lxg symbol of the fixture is classified 食品饮料 / 白酒 / 白酒;
    SH600003 (ak-only) has no ``industries.csv``."""
    args = ["run", "momentum_trend", *_common(store_dir), "--as-of", END,
            "--industry", "白酒"]                       # a second-level name
    assert main(args) == 0
    text = capsys.readouterr().out
    assert _symbols_row(text) == 4                       # the four lxg names
    assert "industry mix: 白酒 100%" not in text          # the tag shown is the L1
    assert "industry mix: 食品饮料" in text
    assert main(["run", "momentum_trend", *_common(store_dir), "--as-of", END,
                 "--industry", "食品饮"]) == 0          # substring match
    assert _symbols_row(capsys.readouterr().out) == 4


def test_industry_miss_names_the_near_misses(store_dir):
    with pytest.raises(SystemExit) as exc:
        main(["run", "momentum_trend", *_common(store_dir), "--as-of", END,
              "--industry", "银行"])
    message = str(exc.value)
    assert "no industry matches '银行'" in message and "--live" in message


def test_industry_and_boards_intersect_in_the_funnel(store_dir, capsys):
    assert main(["run", "momentum_trend", *_common(store_dir), "--as-of", END,
                 "--boards", "bj", "--industry", "白酒"]) == 0
    text = capsys.readouterr().out
    # two funnel rows, one intersection: 4 names carry the industry, 1 is on
    # the Beijing board, and BJ430004 is both
    rows = {ln.split()[0]: ln.split() for ln in text.splitlines()
            if ln.strip().split() and ln.strip().split()[0] in ("boards", "symbols")}
    assert rows["boards"][1] == "1" and rows["symbols"][1] == "4"
    assert rows["symbols"][2] == "1"                     # cumulative after both


class _FakeLive:
    """akshare stand-in: a spot snapshot and two industry boards."""

    def __init__(self):
        self.board_calls = []

    def stock_zh_a_spot_em(self):
        return pd.DataFrame({
            "代码": ["600001", "000002"], "名称": ["公司一", "公司二"],
            "最新价": [11.0, 13.0], "涨跌幅": [1.0, -0.5], "成交额": [1e8, 2e8],
            "换手率": [1.5, 2.0], "市盈率-动态": [12.0, 15.0], "市净率": [2.0, 3.0],
            "总市值": [1.1e10, 1.3e10], "流通市值": [6e9, 8e9], "昨收": [10.9, 13.1]})

    def stock_board_industry_name_em(self):
        return pd.DataFrame({"板块名称": ["酿酒行业", "电力设备"], "板块代码": ["BK1", "BK2"]})

    def stock_board_industry_cons_em(self, symbol=None):
        self.board_calls.append(symbol)
        if symbol == "酿酒行业":
            return pd.DataFrame({"代码": ["600003"], "名称": ["公司三"]})
        return pd.DataFrame({"代码": ["600001"], "名称": ["公司一"]})


def _live_store(root, cache):
    store = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=cache)
    store.live = LiveAkClient(root / "cn/ak_live", refresh=True, ttl_spot=60,
                              ttl_boards=60, ak=_FakeLive(), pause=0.0)
    return store


def test_live_industry_fallback_fills_the_names_without_a_local_tag(tmp_path):
    """The local 申万 classification covers the lxg names only; under
    --live the 东财 industry boards classify the rest and join the match."""
    root = _tree(tmp_path / "tree")
    store = _live_store(root, cache=False)
    assert "SH600003" not in _local_industry_tags(store)     # ak-only: no lxg file
    # offline: the 东财 board name matches nothing local
    with pytest.raises(SystemExit) as exc:
        _industry_symbols(store, "酿酒行业", live=False)
    assert "--live" in str(exc.value)
    # live: the board member without a local tag is the industry
    assert _industry_symbols(store, "酿酒行业", live=True) == ["SH600003"]
    # live: a local name stays the authority (SH600001 sits in the 电力设备
    # board, which is not what was asked for)
    assert set(_industry_symbols(store, "白酒", live=True)) == {
        "SH600001", "SZ000002", "SH600005", "BJ430004"}
    # the fallback fills the census gap -- and never overwrites a local tag
    assert store.enrich_industry_tags() == 1
    tags = _local_industry_tags(store)
    assert tags["SH600003"] == ("酿酒行业",)
    assert tags["SH600001"] == ("食品饮料", "白酒")            # de-duplicated
    assert _industry_symbols(store, "酿酒行业", live=False) == ["SH600003"]


def test_run_live_industry_end_to_end(tmp_path, monkeypatch, capsys):
    """``run --live --industry 酿酒行业``: the live row is attached, the
    东财 boards are read, and the screen is restricted to the names they
    classify."""
    import qf_screen.data.merged_store as merged_store

    root = _tree(tmp_path / "tree")
    monkeypatch.setattr(
        merged_store, "LiveAkClient",
        lambda *args, ak=None, **kwargs: LiveAkClient(    # the store passes ak=
            *args, ak=_FakeLive(), pause=0.0, **kwargs))
    assert main(["run", "momentum_trend", *_common(root), "--live",
                 "--industry", "酿酒行业", "--top", "3"]) == 0
    assert _symbols_row(capsys.readouterr().out) == 1
    assert (root / "cn/ak_live/industry_members.csv").exists()   # disk-cached


# -------------------------------------- overrides on pit and validate
def test_pit_accepts_the_screen_overrides(tmp_path):
    out, pooled = tmp_path / "sched.csv", tmp_path / "pooled.csv"
    root = _tree(tmp_path / "tree")
    preset = _preset(tmp_path, LOOSE.replace("top_n: 1", "top_n: 30"), "wide")
    flags = ["--start", "2026-03-01", "--end", "2026-06-30", "--freq", "M",
             "--where", "close_raw < 1e9", "--rank", "pct_rank(pb)",
             "--boards", "main"]
    assert main(["pit", preset, *_common(root), *flags, "--top", "2", "--out", str(out)]) == 0
    frame = pd.read_csv(out)
    assert not frame.empty
    assert (frame.groupby("date")["symbol"].nunique() <= 2).all()    # --top won
    assert not frame["symbol"].str.startswith("BJ").any()            # --boards won
    assert main(["pit", preset, *_common(root), *flags, "--all-passers",
                 "--out", str(pooled)]) == 0
    every = pd.read_csv(pooled)
    assert len(every) >= len(frame) and set(frame["symbol"]) <= set(every["symbol"])


def test_validate_accepts_the_screen_overrides(tmp_path, capsys):
    root = _tree(tmp_path / "tree")
    preset = _preset(tmp_path, LOOSE, "loose")
    assert main(["validate", preset, *_common(root),
                 "--start", "2026-03-01", "--end", "2026-06-30", "--horizons", "5",
                 "--where", "close_raw > 0", "--rank", "pct_rank(earnings_yield)",
                 "--boards", "main", "--top", "1", "--all-passers",
                 "--benchmarks", ""]) == 0
    text = capsys.readouterr().out
    assert "5d" in text and "portfolio" in text and "turnover" in text
    assert "every passer" in text and "csi300" not in text   # benchmarks off


def test_validate_json_and_curve(tmp_path):
    out, curve = tmp_path / "v.json", tmp_path / "curve.csv"
    root = _tree(tmp_path / "tree")
    preset = _preset(tmp_path, LOOSE.replace("top_n: 1", "top_n: 30"), "wide")
    assert main(["validate", preset, *_common(root),
                 "--start", "2026-03-01", "--end", "2026-06-30", "--horizons", "5",
                 "--format", "json", "--out", str(out), "--curve", str(curve)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert {"horizons", "stats", "spread", "turnover", "notes", "curve"} <= set(payload)
    assert set(payload["stats"]) == {"portfolio", "universe"}    # no index file here
    table = pd.read_csv(curve, index_col=0, parse_dates=True)
    assert list(table.columns) == ["portfolio", "universe"] and len(table) > 0


# ---------------------------------------------------------------- diff
def test_diff_cli_text_json_markdown(tmp_path, capsys):
    root = _tree(tmp_path / "tree")
    preset = _preset(tmp_path, MOVER, "mover")
    base = ["diff", preset, *_common(root), "--as-of", END]
    assert main(base) == 0
    text = capsys.readouterr().out
    assert "mover: 2 members as of" in text or "mover: " in text
    assert "entered (" in text and "exited (" in text and "rank movers (" in text
    assert main([*base, "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"entered", "exited", "movers", "retained", "prev", "ranked"} <= set(payload)
    assert payload["ranked"] is True and payload["prev"] < payload["asof"]
    assert main([*base, "--format", "md", "--out", str(tmp_path / "d.md")]) == 0
    body = (tmp_path / "d.md").read_text(encoding="utf-8")
    assert body.startswith("## `mover` — changes as of") and "**entered" in body


# ------------------------------------------------------------- inspect
def test_inspect_cli(store_dir, capsys):
    base = ["inspect", "SH600001", *_common(store_dir), "--as-of", END]
    assert main(base) == 0
    text = capsys.readouterr().out
    assert "SH600001 公司600001" in text and "passes" in text
    assert "feature scorecard" in text and "pe_ttm" in text
    assert main([*base, "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"presets", "stats", "passed", "asof"} <= set(payload)
    from qf_screen.presets import built_in_presets
    assert len(payload["presets"]) == len(built_in_presets())  # every built-in
    assert main([*base, "--presets", "value_dividend", "--format", "markdown"]) == 0
    assert "# `SH600001`" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        main(["inspect", "NOPE123", *_common(store_dir)])
    assert "not in the loaded universe" in str(exc.value)


# -------------------------------------------------------------- report
def test_report_cli_writes_a_markdown_briefing(store_dir, tmp_path):
    out = tmp_path / "report.md"
    assert main(["report", *_common(store_dir), "--as-of", END, "--out", str(out)]) == 0
    body = out.read_text(encoding="utf-8")
    assert body.startswith("# qf-screen daily briefing — 2026-06-30")
    assert "## Overview" in body and "| preset | members | entered | exited |" in body
    for name in ("value_dividend", "momentum_trend", "quality_growth",
                 "low_vol_defensive", "smart_money_ashare"):
        assert f"## `{name}` — " in body
    assert "**industry mix:**" in body and "top picks:" in body


def test_report_cli_json(store_dir, capsys):
    assert main(["report", *_common(store_dir), "--as-of", END, "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    from qf_screen.presets import built_in_presets
    assert payload["asof"] == END and len(payload["presets"]) == len(built_in_presets())
    first = next(iter(payload["presets"].values()))
    assert {"members", "industry_mix", "entered", "exited", "notes"} <= set(first)


# ------------------------------------------------------------- compare
def test_compare_cli(tmp_path, capsys):
    root = _tree(tmp_path / "tree")
    first = _preset(tmp_path, LOOSE.replace("name: loose", "name: first").replace(
        "top_n: 1", "top_n: 30"), "first")
    second = _preset(tmp_path, LOOSE.replace("name: loose", "name: second").replace(
        'expr: "pe_ttm > 0"', 'expr: "pe_ttm > 5"'), "second")
    base = ["compare", first, second, *_common(root), "--start", "2026-03-01",
            "--end", "2026-06-30", "--horizons", "5", "--benchmarks", ""]
    out = tmp_path / "c.csv"
    assert main([*base, "--out", str(out)]) == 0
    text = capsys.readouterr().out
    for metric in ("ann_return", "sharpe", "max_drawdown", "turnover", "spread_5d",
                   "information_ratio"):
        assert metric in text, metric
    assert "first" in text and "second" in text and "universe" in text
    table = pd.read_csv(out, index_col=0)
    assert list(table.columns) == ["first", "second", "universe"]
    assert main([*base, "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"table", "notes", "per_date"} <= set(payload) and set(payload["table"]) == {
        "first", "second", "universe"}


# --------------------------------------------------------------- cache
def test_cache_cli_status_clear_warm(tmp_path, capsys):
    root = _tree(tmp_path / "tree")
    assert main(["cache", "warm", *_common(root), "--presets", "value_dividend"]) == 0
    assert "cache warmed" in capsys.readouterr().out
    cache = root / "cn/qf_screen_cache"
    names = {p.name for p in cache.rglob("*")}
    assert any(n.startswith("store_tail") for n in names) \
        and "universe_cn.parquet" in names \
        and any(n.startswith("feature_") for n in names) \
        and any(n.startswith("finance_step_") for n in names)

    assert main(["cache", "status", *_common(root), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    kinds = {row["kind"] for row in status["kinds"]}
    assert {"store", "universe", "feature", "statement"} <= kinds
    tail = next(row for row in status["stores"] if row["kind"] == "store")
    assert tail["fresh"] is True and tail["symbols"] == 5

    assert main(["cache", "clear", *_common(root), "--what", "feature", "--dry-run"]) == 0
    assert "would remove" in capsys.readouterr().out
    assert any(p.name.startswith("feature_") for p in cache.rglob("*"))   # untouched
    assert main(["cache", "clear", *_common(root), "--what", "feature"]) == 0
    assert "removed" in capsys.readouterr().out
    left = {p.name for p in cache.rglob("*")}
    assert not any(n.startswith("feature_") for n in left)
    assert any(n.startswith("store_tail") for n in left) \
        and "universe_cn.parquet" in left                        # the rest stays

    assert main(["cache", "status", *_common(root)]) == 0           # the text view
    text = capsys.readouterr().out
    assert "qf_screen cache:" in text and "fresh" in text
    # a changed source file turns the store cache stale
    source = root / "cn/equity/SZ000002/ak/basic.csv"
    stamp = source.stat().st_mtime + 5
    import os
    os.utime(source, (stamp, stamp))
    assert main(["cache", "status", *_common(root)]) == 0
    assert "STALE" in capsys.readouterr().out

    assert main(["cache", "clear", *_common(root)]) == 0            # everything
    assert list(cache.iterdir()) == []
