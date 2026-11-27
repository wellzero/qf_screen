"""v0.3 cache-layer tests: the flat parquet codec, the universe census
cache, the store fingerprint and cache, the store-keyed price-feature
cache, the cache inventory, the portfolio arithmetic behind ``validate``,
and the formatting helpers.  Each test pins the property that makes a
cache safe to trust — a hit is the same data a miss would produce, and a
changed source is never served stale.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

import conftest as cf
import qf_screen
from qf_screen import cache_ops
from qf_screen.analytics import perf, simulate, spread_stats
from qf_screen.data import BASE_FIELDS, CNDataStore, build_universe
from qf_screen.data import merged_store as merged_store_module
from qf_screen.data.ak_provider import LiveAkClient
from qf_screen.data.merged_store import source_fingerprint
from qf_screen.data.table_cache import (
    cache_clear, cache_inventory, cache_summary, cached_wide_table, fingerprint, kind_of,
    read_cached_table, read_wide, window_summary, write_cached_table, write_wide,
)
from qf_screen.ui.render import fmt_value, human_bytes, industry_line, markdown_table
from qf_screen.rules import FeaturePanel

pytestmark = pytest.mark.unit


def _tree(root):
    cf._company_list(root)
    for symbol, spec in cf.SYMBOLS.items():
        cf._symbol_files(root, symbol, spec)
    return root


def _frame(rows=40, cols=7, dtype=np.float64, seed=0):
    values = np.random.default_rng(seed).uniform(5, 50, (rows, cols)).astype(dtype)
    values[values < 8] = np.nan
    index = pd.DatetimeIndex(pd.bdate_range("2026-01-01", periods=rows).to_numpy(),
                             name="date")
    return pd.DataFrame(values, index=index, columns=[f"S{i:03d}" for i in range(cols)])


# ------------------------------------------------------------ codec
@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_wide_codec_round_trip_keeps_values_dtype_and_axes(tmp_path, dtype):
    frame = _frame(dtype=dtype)
    frame.iloc[0, 0] = np.inf
    frame.iloc[1, 1] = -np.inf
    path = tmp_path / "t.parquet"
    write_wide(path, frame)
    back = read_wide(path, index=frame.index, columns=frame.columns)
    assert back is not None
    pd.testing.assert_frame_equal(back, frame)
    assert back.dtypes.iloc[0] == dtype and back.index.name == "date"
    assert back.to_numpy().flags.writeable             # callers mutate in tests
    # without the expected axes the file's own come back
    pd.testing.assert_frame_equal(read_wide(path), frame)


def test_read_wide_rejects_misaligned_and_foreign_files(tmp_path):
    frame = _frame()
    path = tmp_path / "t.parquet"
    write_wide(path, frame)
    assert read_wide(path, index=frame.index[:-1]) is None            # other calendar
    assert read_wide(path, columns=list(frame.columns[:-1])) is None  # other symbols
    assert read_wide(path, columns=list(frame.columns[::-1])) is None
    old = tmp_path / "old.parquet"                        # a v0.2 layout file
    frame.to_parquet(old)
    assert read_wide(old) is None
    broken = tmp_path / "broken.parquet"
    broken.write_bytes(path.read_bytes()[:200])           # a truncated write
    assert read_wide(broken) is None
    assert read_wide(tmp_path / "absent.parquet") is None
    with pytest.raises(ValueError):
        write_wide(tmp_path / "empty.parquet", frame.iloc[:0])


def test_sidecar_gates_the_read_and_replays_the_warnings(tmp_path):
    frame, path = _frame(), tmp_path / "t.parquet"
    write_cached_table(path, "fp1", frame, warnings=["w1", "w2"])
    meta = tmp_path / "t.parquet.meta.txt"
    assert meta.read_text(encoding="utf-8").splitlines()[0].startswith("v4:fp1")
    table, replay = read_cached_table(path, "fp1")
    assert replay == ["w1", "w2"]
    pd.testing.assert_frame_equal(table, frame)
    assert read_cached_table(path, "fp2") is None           # another fingerprint
    meta.write_text("v3:fp1\n", encoding="utf-8")
    assert read_cached_table(path, "fp1") is None           # another sidecar version


def test_cached_wide_table_builds_once_or_never_caches(tmp_path):
    calls = []

    def build():
        calls.append(1)
        return _frame(seed=len(calls))

    directory = tmp_path / "cache"
    first = cached_wide_table(directory, "k", "fp", build)
    again = cached_wide_table(directory, "k", "fp", build)
    assert len(calls) == 1
    pd.testing.assert_frame_equal(again, first)              # served from the file
    assert (directory / "k.parquet").exists()
    plain = cached_wide_table(directory, "k2", "fp", build, use_cache=False)
    cached_wide_table(directory, "k2", "fp", build, use_cache=False)
    assert len(calls) == 3                                 # built every time
    assert not (directory / "k2.parquet").exists()         # and written never
    assert plain is not None


def test_fingerprint_is_the_count_and_the_sum_of_mtimes(tmp_path):
    files = [tmp_path / "a.csv", tmp_path / "b.csv"]
    for path in files:
        path.write_text("x")
    os.utime(files[0], (1_000, 1_000))
    os.utime(files[1], (2_000, 2_000))
    billion = 1_000_000_000
    assert fingerprint(files) == f"2:{3_000 * billion}"
    os.utime(files[1], (3_000, 3_000))
    assert fingerprint(files) == f"2:{4_000 * billion}"
    # a rewrite that leaves a file OLDER than the newest one still moves it
    # (a newest-mtime census would not have noticed this one)
    os.utime(files[0], (500, 500))
    assert fingerprint(files) == f"2:{3_500 * billion}"
    assert fingerprint(files + [tmp_path / "missing.csv", None]) == f"2:{3_500 * billion}"
    assert fingerprint([], extra="e") == "0:0:e"


# ---------------------------------------------------------- inventory
TAIL, HIST = "tail600_abc", "hist2026-01-01-2026-02-01_abc"
#: the current version's tag on a merged-store cache directory
VERSION = "v" + qf_screen.__version__.replace(".", "")
TAIL_STORE = f"store_tail600_abc_{VERSION}"
HIST_STORE = f"store_hist2026-01-01-2026-02-01_abc_{VERSION}"


def _fill(root):
    """A cache directory with one entry of every kind (derived tables under
    ``tables/<window>/``)."""
    cache = root / "cn/qf_screen_cache"
    store = cache / TAIL_STORE
    store.mkdir(parents=True)
    for name, size in (("meta.json", 10), ("provenance.parquet", 100),
                       ("wide_close.parquet", 500)):
        (store / name).write_bytes(b"x" * size)
    history = cache / HIST_STORE
    history.mkdir()
    (history / "meta.json").write_bytes(b"x" * 5)
    write_cached_table(cache / "universe_cn.parquet", "fp", _frame())
    (cache / "universe_cn.meta.json").write_text("{}")
    daily, window = cache / "tables" / TAIL, cache / "tables" / HIST
    write_cached_table(daily / "feature_max21.parquet", "fp", _frame())
    write_cached_table(daily / "alt_step_pledge_ratio.parquet", "fp", _frame())
    write_cached_table(daily / "finance_step_roe.parquet", "fp", _frame())
    write_cached_table(daily / "div_yield_ttm_events.parquet", "fp", _frame())
    write_cached_table(daily / "factor_Alpha101__alpha1.parquet", "fp", _frame())
    (daily / "feature_max21.parquet.tmp").write_bytes(b"x" * 3)     # interrupted write
    write_cached_table(window / "feature_max21.parquet", "fp", _frame())
    (cache / "finance_payload_abc.parquet").write_bytes(b"x" * 7)   # window-independent
    (cache / "finance_payload_abc.meta.txt").write_text("1:1")
    # what a v0.2 install left behind: a store cache of another version and
    # derived tables at the top level -- never read again
    (cache / "store_tail600_abc_v010").mkdir()
    (cache / "store_tail600_abc_v010" / "meta.json").write_bytes(b"x" * 5)
    write_cached_table(cache / "finance_step_old.parquet", "fp", _frame())
    live = root / "cn/ak_live"
    live.mkdir()
    (live / "spot.csv").write_bytes(b"x" * 30)
    (live / "spot.meta.json").write_text("{}")
    return cache


def test_inventory_groups_a_table_with_its_sidecar(tmp_path):
    _fill(tmp_path)
    inventory = cache_inventory(tmp_path, "cn")
    rows = {(r["kind"], r["name"]): r for r in inventory}
    assert set(rows) == {
        ("store", TAIL_STORE), ("history", HIST_STORE),
        ("universe", "universe_cn"),
        ("feature", f"{TAIL}/feature_max21"), ("alt", f"{TAIL}/alt_step_pledge_ratio"),
        ("statement", f"{TAIL}/finance_step_roe"), ("dividend", f"{TAIL}/div_yield_ttm_events"),
        ("factor", f"{TAIL}/factor_Alpha101__alpha1"),
        ("tmp", f"{TAIL}/feature_max21.parquet.tmp"),
        ("feature", f"{HIST}/feature_max21"),                      # the other window
        ("statement", "finance_payload_abc"), ("live", "live:spot"),
        ("obsolete", "store_tail600_abc_v010"),                    # another version
        ("obsolete", "finance_step_old")}                          # a top-level table
    assert rows["store", TAIL_STORE]["files"] == 3
    assert rows["store", TAIL_STORE]["bytes"] == 610
    assert rows["feature", f"{TAIL}/feature_max21"]["files"] == 2   # table + sidecar
    assert rows["live", "live:spot"]["bytes"] == 32
    summary = {row["kind"]: row for row in cache_summary(inventory)}
    assert summary["statement"]["entries"] == 2 and summary["statement"]["files"] == 4
    assert summary["feature"]["entries"] == 2 and summary["store"]["bytes"] == 610
    windows = {row["window"]: row for row in window_summary(inventory)}
    assert set(windows) == {TAIL, HIST}
    assert windows[TAIL]["entries"] == 6 and windows[HIST]["entries"] == 1
    assert windows[TAIL]["bytes"] == sum(r["bytes"] for (kind, name), r in rows.items()
                                         if name.startswith(TAIL + "/"))
    assert kind_of("store_hist2026-01-01-2026-02-01_abc_v1") == "history"
    assert kind_of("wide_close") == "other" and kind_of("feature_x") == "feature"


def test_cache_clear_removes_only_the_asked_kinds(tmp_path):
    cache = _fill(tmp_path)
    tables = cache / "tables"
    removed, freed = cache_clear(tmp_path, "cn", ["feature"])
    assert removed == 2 and freed > 0                            # the table in both windows
    assert not (tables / TAIL / "feature_max21.parquet").exists()
    assert (tables / TAIL / "feature_max21.parquet.tmp").exists()  # tmp is its own kind
    assert not (tables / HIST).exists()                           # left empty: removed
    assert (tables / TAIL / "alt_step_pledge_ratio.parquet").exists()
    assert (cache / TAIL_STORE).is_dir() and (cache / "universe_cn.parquet").exists()
    removed, freed = cache_clear(tmp_path, "cn", ["store", "history"])
    assert removed == 2
    assert not (cache / TAIL_STORE).exists() and not (cache / HIST_STORE).exists()
    assert (cache / "store_tail600_abc_v010").is_dir()        # another version: later
    removed, freed = cache_clear(tmp_path, "cn", ["live"])
    assert removed == 1 and list((tmp_path / "cn/ak_live").iterdir()) == []
    removed, freed = cache_clear(tmp_path, "cn", ["obsolete"])  # what an upgrade leaves
    assert removed == 2 and not (cache / "store_tail600_abc_v010").exists() \
        and not (cache / "finance_step_old.parquet").exists()
    assert (cache / "finance_payload_abc.parquet").exists()  # still read: not obsolete
    removed, freed = cache_clear(tmp_path, "cn", ["all"])
    assert removed == 7 and list(cache.iterdir()) == []       # the rest of the 14 entries
    assert cache_clear(tmp_path / "nowhere", "cn", ["all"]) == (0, 0)


def _names(universe):
    return {s: universe.get(s, "name") for s in ("SH600001", "SZ000002")}


def test_universe_cache_is_the_built_census(tmp_path):
    root = _tree(tmp_path / "tree")
    built = build_universe(root, use_cache=False)
    first = build_universe(root)                              # a miss: build + write
    second = build_universe(root)                             # a hit
    pd.testing.assert_frame_equal(second.meta, built.meta)
    pd.testing.assert_frame_equal(first.meta, built.meta)
    assert second.dirs == built.dirs == first.dirs
    assert second.names_asof == built.names_asof and second.root == built.root
    assert second.st_named == built.st_named == {"SH600005"}
    assert second.get("SH600001", "industry_l1") == "食品饮料"
    assert second.get("SH600003", "industry_l1", "missing") == "missing"   # None/NaN
    assert (root / "cn/qf_screen_cache/universe_cn.parquet").exists()


def test_universe_cache_follows_its_three_inputs(tmp_path):
    root = _tree(tmp_path / "tree")
    assert _names(build_universe(root))["SH600001"] == "公司600001"

    listing = root / "cn/lxg/company_list.csv"                # 1. the company list
    frame = pd.read_csv(listing, dtype=str, encoding="utf-8-sig")
    frame.loc[0, "name"] = "改名公司"
    cf._write_csv(listing, frame)
    _bump(listing)
    assert _names(build_universe(root))["SH600001"] == "改名公司"

    industries = root / "cn/equity/SZ000002/lxg/industries.csv"   # 2. a classification
    cf._write_csv(industries, pd.DataFrame({
        "areaCode": ["cn"] * 3, "source": ["sw_2021"] * 3, "name": ["银行", "国有大行", "x"]}))
    _bump(industries)
    universe = build_universe(root)
    assert universe.get("SZ000002", "industry_l1") == "银行"
    assert universe.get("SH600001", "name") == "改名公司"       # and the list is kept

    (root / "cn/equity/SH600999").mkdir()                       # 3. a new directory
    assert "SH600999" in build_universe(root)
    (root / "cn/equity/SH600999/lxg").mkdir(parents=True)       # ... that gets classified
    cf._write_csv(root / "cn/equity/SH600999/lxg/industries.csv", pd.DataFrame({
        "areaCode": ["cn"] * 3, "source": ["sw_2021"] * 3, "name": ["汽车", "整车", "x"]}))
    universe = build_universe(root)
    assert universe.get("SH600999", "industry_l1") == "汽车"
    # a corrupt sidecar is a miss, not an error
    (root / "cn/qf_screen_cache/universe_cn.meta.json").write_text("{not json")
    assert build_universe(root).get("SH600999", "industry_l1") == "汽车"


def _bump(path, seconds=5):
    stamp = os.stat(path).st_mtime + seconds
    os.utime(path, (stamp, stamp))


def test_universe_no_cache_writes_nothing(tmp_path):
    root = _tree(tmp_path / "tree")
    build_universe(root, use_cache=False)
    assert not (root / "cn/qf_screen_cache").exists()


# ------------------------------------------------------ store + features
def test_store_fingerprint_is_a_pathlib_census_of_the_source_files(tmp_path):
    root = _tree(tmp_path / "tree")
    store = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False)
    assert store.fingerprint is None                           # --no-cache keys nothing
    store = CNDataStore(root, warmup_rows=cf.SESSIONS)
    count, total = 0, 0
    for symbol in store.symbols:                               # pathlib arithmetic
        for name in store.universe.dir_names(symbol):
            for provider, filename in CNDataStore._CACHE_SOURCES:
                path = store.lxg.root / name / provider / filename
                if path.exists():
                    count += 1
                    total += path.stat().st_mtime_ns
    assert store.fingerprint == f"{count}:{total}"
    assert count > 5                                           # every alias and file
    assert source_fingerprint(store.lxg.root, ["nobody"], lambda s: (),
                              CNDataStore._CACHE_SOURCES) == "0:0"
    assert store.fingerprint is store.fingerprint              # computed once
    # any rewritten source moves it, even one left older than the rest
    source = root / "cn/equity/SZ000002/ak/basic.csv"
    stamp = os.stat(root / "cn/lxg/company_list.csv").st_mtime - 3600
    os.utime(source, (stamp, stamp))
    assert CNDataStore(root, warmup_rows=cf.SESSIONS).fingerprint != store.fingerprint


def test_store_cache_hit_is_a_merge_in_disguise(tmp_path, monkeypatch):
    root = _tree(tmp_path / "tree")
    CNDataStore(root, warmup_rows=cf.SESSIONS)                 # miss: merge + write
    plain = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False)

    def _boom(*_args, **_kwargs):
        raise AssertionError("a cache hit must not merge a single file")

    monkeypatch.setattr(merged_store_module, "merge_symbol", _boom)
    hit = CNDataStore(root, warmup_rows=cf.SESSIONS)
    assert len(hit.provenance) == len(plain.provenance) == 5
    for field in BASE_FIELDS:
        pd.testing.assert_frame_equal(hit.wide(field), plain.wide(field))
    pd.testing.assert_frame_equal(hit.provenance, plain.provenance)
    assert list(hit.calendar) == list(plain.calendar)
    assert hit.last_complete_session == plain.last_complete_session
    assert hit.wide("close").to_numpy().flags.writeable         # tests poke the tables
    # the cache directory is keyed on the symbol list and the version
    assert hit._store_cache_dir().name.startswith("store_tail320_") \
        and hit._store_cache_dir().name.endswith("_v" + __import__("qf_screen").__version__.replace(".", ""))


def test_price_feature_cache_serves_the_recomputed_table(tmp_path):
    root = _tree(tmp_path / "tree")
    panel = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS))
    for name in ("ma_ratio_20", "max21", "amount_ma20"):
        pd.testing.assert_frame_equal(
            panel.table(name),
            FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False)).table(name))
        assert (panel.store.table_cache_dir / f"feature_{name}.parquet").exists()
    assert not (panel.store.table_cache_dir / "feature_pe_ttm.parquet").exists()   # not cached


def test_price_feature_cache_follows_a_source_change(tmp_path):
    root = _tree(tmp_path / "tree")
    store = CNDataStore(root, warmup_rows=cf.SESSIONS)
    before = FeaturePanel(store).table("ma_ratio_20")
    sidecar = store.table_cache_dir / "feature_ma_ratio_20.parquet.meta.txt"
    written = sidecar.read_text(encoding="utf-8")
    source = root / "cn/equity/SH600003/ak/basic.csv"           # the ak-only name
    frame = pd.read_csv(source)
    frame.loc[frame.index[-10:], "close"] *= 1.5               # a new shape, not a scale
    frame.to_csv(source, index=False, encoding="utf-8-sig")
    _bump(source)
    after = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS)).table("ma_ratio_20")
    plain = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False))
    pd.testing.assert_frame_equal(after, plain.table("ma_ratio_20"))
    assert not np.allclose(after["SH600003"].dropna(), before["SH600003"].dropna())
    assert sidecar.read_text(encoding="utf-8") != written       # re-keyed
    assert plain.store.fingerprint is None                      # --no-cache keys nothing
    assert not [n for n in os.listdir(store.table_cache_dir) if n.startswith("feature_pe")]


class _Spot:
    """akshare stand-in with a spot snapshot only."""

    def stock_zh_a_spot_em(self):
        return pd.DataFrame({
            "代码": ["600001"], "名称": ["公司一"], "最新价": [11.0], "涨跌幅": [1.0],
            "成交额": [1e8], "换手率": [1.5], "市盈率-动态": [12.0], "市净率": [2.0],
            "总市值": [1.1e10], "流通市值": [6e9], "昨收": [10.9]})


def test_price_feature_cache_steps_aside_for_a_live_row(tmp_path):
    root = _tree(tmp_path / "tree")
    store = CNDataStore(root, warmup_rows=cf.SESSIONS)
    cache = store.table_cache_dir
    FeaturePanel(store).table("ma_ratio_60")
    daily = (cache / "feature_ma_ratio_60.parquet.meta.txt").read_text(encoding="utf-8")
    store.live = LiveAkClient(root / "cn/ak_live", refresh=True, ttl_spot=60, ak=_Spot())
    store.attach_live_spot()
    panel = FeaturePanel(store)
    live_table = panel.table("ma_ratio_60")
    assert live_table.index[-1] == store.live_date             # computed for the live row
    assert (cache / "feature_ma_ratio_60.parquet.meta.txt").read_text(encoding="utf-8") == daily
    panel.table("vol_20d")
    assert not (cache / "feature_vol_20d.parquet").exists()    # nothing written either


def test_each_window_keeps_its_own_derived_tables(tmp_path, monkeypatch):
    """A derived table is aligned to ONE store window.  With a single file
    per table a validation window overwrote the daily snapshot's tables and
    the next morning's ``run`` rebuilt them (12 s on the real store)."""
    import qf_screen.rules.fundamental as fundamental

    root = _tree(tmp_path / "tree")
    daily = CNDataStore(root, warmup_rows=cf.SESSIONS)
    daily_panel = FeaturePanel(daily)
    daily_panel.table("roe")
    written = (daily.table_cache_dir / "finance_step_roe.parquet").read_bytes()
    stamp = (daily.table_cache_dir / "finance_step_roe.parquet.meta.txt").read_text(
        encoding="utf-8")
    window = CNDataStore(root, start="2026-02-01", end=cf.END)
    assert window.table_cache_dir != daily.table_cache_dir
    assert window.table_cache_dir.parent == daily.table_cache_dir.parent
    assert window.window_key.startswith("hist2026-02-01-2026-06-30_")
    plain = FeaturePanel(CNDataStore(root, start="2026-02-01", end=cf.END, use_cache=False))
    pd.testing.assert_frame_equal(FeaturePanel(window).table("roe"), plain.table("roe"))

    def _boom(panel):
        raise AssertionError("the daily window's table must still be a cache hit")

    monkeypatch.setattr(fundamental, "_build_finance_steps", _boom)
    again = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS)).table("roe")
    pd.testing.assert_frame_equal(again, daily_panel.table("roe"))
    assert (daily.table_cache_dir / "finance_step_roe.parquet").read_bytes() == written
    assert (daily.table_cache_dir / "finance_step_roe.parquet.meta.txt").read_text(
        encoding="utf-8") == stamp


def test_cache_ops_status_and_warm(tmp_path):
    root = _tree(tmp_path / "tree")
    warmed = cache_ops.cache_warm(root, presets=["value_dividend"], max_workers=1)
    assert set(warmed["presets"]) == {"value_dividend"}
    assert "members" in warmed["presets"]["value_dividend"]
    assert set(warmed["seconds"]) == {"store", "presets", "total"}
    status = cache_ops.cache_status(root)
    kinds = {row["kind"] for row in status["kinds"]}
    assert {"store", "universe", "feature", "statement", "dividend"} <= kinds
    assert [row["window"] for row in status["windows"]] == [warmed_store_window(root)]
    tail = next(row for row in status["stores"] if row["kind"] == "store")
    assert tail["fresh"] is True and tail["symbols"] == 5 and tail["sessions"] == 320
    assert tail["window"] == f"{cf.CALENDAR[0].date()}..{cf.END.date()}"
    assert status["source_fingerprint"] == tail["fingerprint"]
    _bump(root / "cn/equity/SZ000002/ak/basic.csv")
    assert cache_ops.cache_status(root)["stores"][0]["fresh"] is False
    assert cache_ops.cache_status(root, check_fresh=False)["source_fingerprint"] is None
    history = cache_ops.cache_warm(root, presets=["momentum_trend"], max_workers=1,
                                   start="2026-03-01", end=cf.END)
    assert "rows" in history["presets"]["momentum_trend"]
    status = cache_ops.cache_status(root)
    assert "history" in {row["kind"] for row in status["kinds"]}
    assert {row["window"] for row in status["windows"]} == {
        warmed_store_window(root),
        "hist2026-03-01-2026-06-30_"
        + CNDataStore.symbols_key(list(build_universe(root).symbols))}
    assert status["windows"][0]["bytes"] == max(r["bytes"] for r in status["windows"])
    json.dumps(status, default=str)                             # reportable


def warmed_store_window(root):
    """``cache warm`` builds the default 600-row daily window."""
    return f"tail600_{CNDataStore.symbols_key(list(build_universe(root).symbols))}"


# ------------------------------------------------------------ analytics
def test_simulate_is_equal_weight_at_entry_held_to_the_next_date():
    #                 A     B     C
    prices = np.array([[10.0, 10.0, 10.0],
                       [11.0, 9.0, 10.0],
                       [12.0, 8.0, 10.0],
                       [13.0, 7.0, 10.0],
                       [14.0, 6.0, 10.0]])
    path = simulate(prices, [(0, np.array([0, 1]))], [4])
    assert list(path.positions) == [1, 2, 3, 4]
    np.testing.assert_allclose(path.values, [1.0, 1.0, 1.0, 1.0])   # A's gain = B's loss
    assert path.periods["period_return"].iloc[0] == 0.0
    assert np.isnan(path.periods["turnover"].iloc[0])
    path = simulate(prices, [(0, np.array([0]))], [4])       # A: 10, 11, 12, 13, 14
    np.testing.assert_allclose(path.values, [1.1, 1.2, 1.3, 1.4])
    np.testing.assert_allclose(path.returns(), [0.1, 1.2 / 1.1 - 1, 1.3 / 1.2 - 1,
                                                1.4 / 1.3 - 1])


def test_simulate_chains_periods_and_measures_turnover():
    prices = np.array([[10.0, 10.0],
                       [11.0, 9.0],
                       [12.0, 8.0],
                       [12.0, 7.0],
                       [12.0, 6.0]])
    path = simulate(prices, [(0, np.array([0])), (2, np.array([1]))], [2, 4])
    # period 1: A 10 -> 12 (+20 %); period 2: B 8 -> 6, on top of 1.2
    np.testing.assert_allclose(path.values, [1.1, 1.2, 1.2 * 0.875, 0.9])
    np.testing.assert_allclose(path.periods["period_return"], [0.2, -0.25])
    np.testing.assert_allclose(path.periods["turnover"], [np.nan, 1.0])
    np.testing.assert_allclose(path.returns()[-1], 0.9 / 1.05 - 1.0)


def test_simulate_drops_a_name_without_an_entry_price():
    prices = np.array([[10.0, np.nan],
                       [11.0, np.nan],
                       [12.0, np.nan]])
    path = simulate(prices, [(0, np.array([0, 1]))], [2])
    assert path.periods["members"].iloc[0] == 1
    np.testing.assert_allclose(path.values, [1.1, 1.2])
    empty = simulate(prices, [], [])
    assert empty.empty and empty.returns().size == 0


def test_perf_and_spread_stats():
    stats = perf(pd.Series([0.01, -0.02, 0.01]))
    growth = 1.01 * 0.98 * 1.01
    assert stats["cum_return"] == pytest.approx(growth - 1.0)
    assert stats["ann_return"] == pytest.approx(growth ** (252 / 3) - 1.0)
    values = np.array([0.01, -0.02, 0.01])
    assert stats["ann_vol"] == pytest.approx(values.std(ddof=1) * np.sqrt(252))
    assert stats["sharpe"] == pytest.approx(
        values.mean() * 252 / (values.std(ddof=1) * np.sqrt(252)))
    assert stats["max_drawdown"] == pytest.approx(-0.02)      # 1.01 x 0.98 / 1.01
    assert stats["days"] == 3
    empty = perf([np.nan, np.nan])
    assert empty["days"] == 0 and np.isnan(empty["ann_return"])
    flat = perf([0.0, 0.0, 0.0])
    assert flat["sharpe"] is np.nan or np.isnan(flat["sharpe"])   # no volatility
    spread = spread_stats([0.01, -0.01, 0.01, -0.01])
    assert spread["excess_ann"] == 0.0
    assert spread["tracking_error"] == pytest.approx(
        np.array([0.01, -0.01, 0.01, -0.01]).std(ddof=1) * np.sqrt(252))
    assert spread["information_ratio"] == 0.0                    # no mean excess
    assert np.isnan(spread_stats([0.01])["excess_ann"])          # one observation


# ------------------------------------------------------------ rendering
def test_fmt_value_keeps_counts_plain_and_units_straight():
    assert fmt_value("passing", 3417) == "3,417" and fmt_value("rank", 3) == "3"
    assert fmt_value("pct_rank", 0.8312) == "0.831" and fmt_value("change", -2) == "-2"
    assert fmt_value("turnover_ma20", 1.38) == "1.38%"           # percent points
    assert fmt_value("dyr", 0.0512) == "5.1%"
    assert fmt_value("amount_ma20_mn", 1127.8) == "1,128百万"
    assert fmt_value("ocf_ttm", 931_412_051.0) == "9.3亿"            # a CNY statement flow
    assert fmt_value("revenue_ttm", 1.34e10) == "134.0亿"
    assert fmt_value("pe_ttm", float("nan")) == "-"
    assert fmt_value("roe_best", None) == "-" and fmt_value("name", "贵州茅台") == "贵州茅台"


def test_markdown_table_human_bytes_and_industry_line():
    frame = pd.DataFrame({"symbol": ["A", "B"], "pe_ttm": [22.2, np.nan],
                          "members": [3417, 5], "name": ["甲", None]})
    table = markdown_table(frame)
    assert table.splitlines()[2] == "| A | 22.2 | 3,417 | 甲 |"
    assert table.splitlines()[3] == "| B | - | 5 | - |"
    assert markdown_table(frame.iloc[:0]) == "_(empty)_"
    assert human_bytes(1.2e9) == "1.1 GB" and human_bytes(220e6) == "209.8 MB"
    assert human_bytes(300e3) == "293.0 KB" and human_bytes(512) == "512 B"
    mix = pd.Series({"有色金属": 5, "煤炭": 2, "钢铁": 2, "银行": 1, "白酒": 1, "电子": 1,
                     "未分类": 1})
    assert industry_line(mix) == ("有色金属 5 (38%) · 煤炭 2 (15%) · 钢铁 2 (15%) · "
                                  "银行 1 (8%) · 白酒 1 (8%) · 其他 1 (8%) · 未分类 1 (8%)")
    assert industry_line(mix, top=1) == "有色金属 5 (38%) · 其他 7 (54%) · 未分类 1 (8%)"
    assert industry_line(pd.Series(dtype=int)) == "no members"


def test_importing_the_cli_loads_no_heavy_sibling():
    """``qf_screen.cli`` used to import yfinance (through the quant_free
    symbol helper), bs4 and friends: ~0.1 s of every command.  The helper
    is now bound on first use, which a warm run never reaches.  (pyarrow
    is pandas' own import and is not counted.)"""
    code = ("import sys, qf_screen.cli; "
            "heavy = [m for m in sys.modules if m.split('.')[0] in "
            "('yfinance', 'qf_factor', 'akshare', 'bs4', 'peewee')]; "
            "print(heavy)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.splitlines()[-1]
    assert out == "[]"
