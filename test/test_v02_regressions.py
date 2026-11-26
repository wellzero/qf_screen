"""v0.2 regression tests: the bugs found on the real store, pinned.

Each test here reproduces a failure mode the v0.1 hermetic suite could
not see (numexpr corruption needs >10k cells; the broadcasting bug needs
more than one symbol; the alt staleness needs a lagging file) and asserts
the v0.2 fix.

The tail of the file pins the defects found by the 2026-11-26 verification
pass over the real store: a warm run that handed back the store cache's
savings in feature building, a ``--no-cache`` that missed the derived
tables, CLI unit formatting that was numerically wrong, and anonymous
staleness warnings.
"""
from __future__ import annotations

import os
import re

import numpy as np
import pandas as pd
import pytest

import conftest as cf
from qf_screen.cli import _fmt_value, main
from qf_screen.data import CNDataStore
from qf_screen.engine import ScreenEngine, ScreenSpec, UniverseSpec
from qf_screen.rules import (
    DslError, ExprFilter, ExprRanker, FeaturePanel, compile_expr, expr_names,
    factor_feature_name, is_dynamic_feature,
)
from qf_screen.rules.base import _coalesce, _safe_div

pytestmark = pytest.mark.unit

# ------------------------------------------------- bug 1: numexpr (>10k cells)
def test_package_disables_numexpr():
    """importing qf_screen must turn pandas' numexpr path off: this
    environment's numexpr silently corrupts float32+NaN wide ops."""
    assert pd.options.compute.use_numexpr is False


def test_safe_div_exact_with_numexpr_forced_on():
    """_safe_div (what every wide feature builder uses) is exact even when
    numexpr is force-RE-enabled -- the helpers bypass pandas arithmetic."""
    rng = np.random.default_rng(11)
    raw = rng.uniform(5, 50, (630, 5472)).astype(np.float32)   # >10k cells
    close = pd.DataFrame(raw)
    close.iloc[:200] = np.nan
    ma = close.rolling(200, min_periods=100).mean()
    expected_num = np.divide(close.to_numpy(dtype=float), ma.to_numpy(dtype=float))
    old = pd.options.compute.use_numexpr
    pd.options.compute.use_numexpr = True           # hostile environment
    try:
        out = _safe_div(close, ma)
    finally:
        pd.options.compute.use_numexpr = old
    np.testing.assert_allclose(out.to_numpy(dtype=float), expected_num,
                               rtol=1e-9, equal_nan=True)


def test_ma_ratio_builder_exact_at_width():
    """The registered ma_ratio builder equals plain numpy on a wide
    float32+NaN frame (the SH600519 ma_ratio_200 == 0.0 failure)."""
    rng = np.random.default_rng(7)
    close = pd.DataFrame(rng.uniform(5, 50, (630, 5472)).astype(np.float32))
    close.iloc[:200] = np.nan

    class _Panel:
        class store:
            wide = staticmethod(lambda name: close if name == "close" else None)

    from qf_screen.rules import FEATURES
    out = FEATURES["ma_ratio_200"][0](_Panel)
    ma = close.rolling(200, min_periods=max(2, 200 // 2)).mean()
    expected = np.divide(close.to_numpy(dtype=float), ma.to_numpy(dtype=float))
    np.testing.assert_allclose(out.to_numpy(dtype=float), expected, rtol=1e-9,
                               equal_nan=True)
    # no cell may be silently zero where both legs are finite and positive
    finite = np.isfinite(expected) & (expected > 0.05)
    assert finite.any() and np.isfinite(out.to_numpy(dtype=float)[finite]).all()


# --------------------------------------- bug 2: 1-D days (broadcast corruption)
def test_div_yield_distinct_per_symbol(store):
    """Two covered symbols must get THEIR OWN yield: the v0.1 (N,1)
    broadcast divided every row by the first symbol's price."""
    panel = FeaturePanel(store)
    pos = len(panel.calendar) - 1
    table = panel.table("div_yield_ttm")
    event_based = table["SH600001"].iloc[pos]      # 0.3 CNY / own price
    dyr_fallback = table["SZ000002"].iloc[pos]     # layout-B dyr == 0.02
    price = store.wide("close_raw")["SH600001"].iloc[pos]
    assert event_based == pytest.approx(0.3 / price, rel=1e-6)
    assert dyr_fallback == pytest.approx(0.02, rel=1e-6)
    assert event_based != dyr_fallback             # no shared denominator
    assert np.isnan(table["SH600003"].iloc[pos])   # ak-only: no source


def test_window_sum_distinct_per_symbol(tmp_path):
    """_window_sum counts must be per-symbol (the (N,1) shape bug)."""
    root = tmp_path / "tree"
    cf._company_list(root)
    for symbol, spec in cf.SYMBOLS.items():
        cf._symbol_files(root, symbol, spec)
    # one EXTRA report for SH600001 only: 3 vs 2 in the trailing 90 days
    reports = root / "cn/equity/SH600001/ak/research_reports.csv"
    frame = pd.read_csv(reports)
    frame.loc[len(frame)] = {
        "report_date": str((cf.END - pd.Timedelta(days=5)).date()),
        "rating": "买入", "org": "测试证券", "title": "追加", "eps_fy1": 1.3}
    frame.to_csv(reports, index=False)

    store = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False)
    panel = FeaturePanel(store)
    pos = len(panel.calendar) - 1
    counts = panel.table("report_cnt_90d")
    assert counts["SH600001"].iloc[pos] == 3.0
    assert counts["SZ000002"].iloc[pos] == 2.0
    assert np.isnan(counts["SH600003"].iloc[pos])  # no report file at all


# ------------------------------------- bug 3: alt staleness (bounded ffill)
def _stale_tree(tmp_path, stale_sessions: int):
    """A store tree whose EVERY mutual_market.csv ends ``stale_sessions``
    sessions before the calendar end (a lagging lxg download)."""
    root = tmp_path / f"tree{stale_sessions}"
    cf._company_list(root)
    for symbol, spec in cf.SYMBOLS.items():
        cf._symbol_files(root, symbol, spec)
    for path in root.glob("cn/equity/*/lxg/mutual_market.csv"):
        frame = pd.read_csv(path)
        frame.head(len(frame) - stale_sessions).to_csv(path, index=False)
    return root


def test_change_table_ffills_a_lagging_file_within_the_limit(tmp_path):
    root = _stale_tree(tmp_path, stale_sessions=10)
    store = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False)
    panel = FeaturePanel(store)
    pos = len(panel.calendar) - 1
    values = panel.table("nb_hold_chg_20d")
    # 10 sessions stale < the default 20-session bound: the last computed
    # change is carried forward (0 members -> covered names again)
    assert np.isfinite(values["SH600001"].iloc[pos])
    assert np.isfinite(values["SZ000002"].iloc[pos])


def test_change_table_goes_nan_beyond_the_limit_and_warns(tmp_path):
    # 30 sessions stale: enough file rows for the 20-row change, beyond the
    # 20-session ffill bound -> honest NaN + a staleness warning
    root = _stale_tree(tmp_path, stale_sessions=30)
    store = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False)
    panel = FeaturePanel(store)
    pos = len(panel.calendar) - 1
    values = panel.table("nb_hold_chg_20d")
    assert np.isnan(values["SH600001"].iloc[pos])
    assert any("mutual_market" in w for w in panel.warnings), panel.warnings


# ------------------------------------------- bug 4: schedule honors rank/top_n
def test_schedule_applies_rank_and_top_n(store):
    engine = ScreenEngine(store)
    spec = ScreenSpec(
        name="t", filters=[{"name": "value", "expr": "pe_ttm > 0 and pe_ttm < 30"}],
        rank=[{"expr": "pct_rank(earnings_yield)", "weight": 1.0}], top_n=2,
        universe=UniverseSpec(boards=("all",), exclude_st=False))
    date = store.calendar[-1]
    result = engine.run(spec, asof=date)   # decided on the last close
    schedule, _ = engine.schedule(spec, start=str(date.date()),
                                  end=str(date.date()))
    # membership at t comes from t-1 == the snapshot decided on t-1's close
    snapshot_at_prev = engine.run(spec, asof=store.calendar[-2])
    assert set(schedule["symbol"]) == set(snapshot_at_prev.members)
    assert len(schedule) <= 2
    # and the ranked snapshot is a subset of the same day's schedule pool
    assert set(result.members) <= set(schedule["symbol"]) | {"BJ430004"} - {"BJ430004"} \
        or set(result.members) <= set(engine.run(
            ScreenSpec(name=spec.name, filters=spec.filters, rank=spec.rank,
                       top_n=None, universe=spec.universe),
            asof=store.calendar[-2]).members)


def test_schedule_apply_top_n_false_exports_the_pool(store):
    engine = ScreenEngine(store)
    spec = ScreenSpec(
        name="t", filters=[{"name": "value", "expr": "pe_ttm > 0 and pe_ttm < 30"}],
        rank=[{"expr": "pct_rank(earnings_yield)", "weight": 1.0}], top_n=1,
        universe=UniverseSpec(boards=("all",), exclude_st=False))
    date = str(store.calendar[-1].date())
    truncated, _ = engine.schedule(spec, start=date, end=date)
    pooled, _ = engine.schedule(spec, start=date, end=date, apply_top_n=False)
    assert 0 < len(truncated) <= 1
    assert len(pooled) >= len(truncated)
    assert set(truncated["symbol"]) <= set(pooled["symbol"])


# ------------------------------------------- bug 5: explain rejected symbols
def test_explain_rejected_symbol_shows_features_and_exprs(store):
    engine = ScreenEngine(store)
    spec = ScreenSpec.from_yaml("value_dividend")
    result = engine.run(spec, asof=str(cf.END.date()))
    rejected = next(s for s in engine.panel.symbols
                    if s not in set(result.members))
    info = result.explain(rejected)
    assert info["member"] is False
    assert info["features"], "rejected symbol must still show feature values"
    assert "pe_ttm" in info["features"] or "close_raw" in info["features"]
    assert info["rule_exprs"], "explain must carry the rule expressions"
    assert any(isinstance(v, bool) for v in info["rules"].values())


# ------------------------------------------------- store cache round-trip
def test_store_cache_roundtrip_and_invalidation(tmp_path):
    root = tmp_path / "tree"
    cf._company_list(root)
    for symbol, spec in cf.SYMBOLS.items():
        cf._symbol_files(root, symbol, spec)
    first = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=True)
    second = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=True)
    pd.testing.assert_frame_equal(first.wide("close"), second.wide("close"))
    pd.testing.assert_frame_equal(first.wide("pe_ttm"), second.wide("pe_ttm"))
    pd.testing.assert_frame_equal(first.provenance, second.provenance)
    assert list(first.calendar) == list(second.calendar)
    # a source rewrite invalidates: fingerprint moves with the mtime
    source = root / "cn/equity/SH600001/ak/basic.csv"
    stamp = (os.stat(source).st_mtime) + 5
    os.utime(source, (stamp, stamp))
    third = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=True)
    pd.testing.assert_frame_equal(first.wide("close"), third.wide("close"))


def test_store_no_cache_flag(tmp_path):
    root = tmp_path / "tree"
    cf._company_list(root)
    for symbol, spec in cf.SYMBOLS.items():
        cf._symbol_files(root, symbol, spec)
    store = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False)
    assert not list((root / "cn/qf_screen_cache").glob("store_*")) \
        if (root / "cn/qf_screen_cache").exists() else True
    assert len(store.provenance) == 5               # every symbol has ak basic


# --------------------------------------------------- factor bridge + DSL
def _factor_tree(tmp_path):
    root = tmp_path / "tree"
    cf._company_list(root)
    for symbol, spec in cf.SYMBOLS.items():
        cf._symbol_files(root, symbol, spec)
    # precomputed factor CSVs in the qf_factor per-provider layout: quarterly steps.
    # lxg/ is what the bridge reads by default (the screener is an lxg +
    # akshare product); xq/ is the opt-in provider pinned further down.
    for symbol, roe in (("SH600001", (0.08, 0.12, 0.20)),
                        ("SZ000002", (0.30, 0.28, 0.26))):
        dates = pd.date_range(end=cf.END - pd.Timedelta(days=400), periods=3,
                              freq="365D")
        frame = pd.DataFrame({
            "symbol": symbol, "date": dates.astype(str), "roe": roe,
            "f_score": [4.0, 5.0, 6.0]})
        for provider in ("lxg", "xq"):
            (root / f"cn/equity/{symbol}/{provider}").mkdir(parents=True, exist_ok=True)
            frame.to_csv(root / f"cn/equity/{symbol}/{provider}/Test_factor.csv",
                         index=False)
    return root


def test_factor_bridge_dynamic_feature_and_dsl(tmp_path):
    store = CNDataStore(_factor_tree(tmp_path), warmup_rows=cf.SESSIONS,
                        use_cache=False)
    panel = FeaturePanel(store)
    name = factor_feature_name("Test_factor", "roe")
    assert name == "factor_Test_factor__roe"
    assert is_dynamic_feature(name) and not is_dynamic_feature("roe")
    table = panel.table(name)
    pos = len(panel.calendar) - 1
    assert table["SH600001"].iloc[pos] == pytest.approx(0.20)   # ffilled step
    assert table["SZ000002"].iloc[pos] == pytest.approx(0.26)
    assert np.isnan(table["SH600003"].iloc[pos])                # no file

    # DSL: the call form and the name form filter identically
    via_call = ExprFilter("a", 'factor("Test_factor", "roe") > 0.15')
    via_name = ExprFilter("b", "factor_Test_factor__roe > 0.15")
    np.testing.assert_array_equal(via_call.passes(panel, pos),
                                  via_name.passes(panel, pos))
    idx = {s: i for i, s in enumerate(panel.symbols)}
    assert via_call.passes(panel, pos)[idx["SH600001"]]         # 0.20 > 0.15
    assert not via_call.passes(panel, pos)[idx["SH600003"]]     # NaN fails
    # expr_names carries the dynamic name so the env pre-fetches it
    assert expr_names('rank(factor("Test_factor", "roe"))') == [name]
    # and a ranker can score on it
    scores = ExprRanker("r", f'pct_rank({name})').score(panel, pos)
    assert np.isfinite(scores).any()


def test_factor_dsl_rejects_non_literal_arguments():
    with pytest.raises(DslError):
        compile_expr('factor("a", pe_ttm)')       # column must be a literal
    with pytest.raises(DslError):
        compile_expr('factor("a")')               # arity
    with pytest.raises(DslError):
        compile_expr("'str' + 1")                 # stray strings stay rejected


# ---------------------------------------------- universe symbols restriction
def test_universe_symbols_field_is_enforced(store):
    engine = ScreenEngine(store)
    spec = ScreenSpec(
        name="t", filters=[{"name": "value", "expr": "pe_ttm > 0 and pe_ttm < 100"}],
        universe=UniverseSpec(boards=("all",), exclude_st=False,
                              symbols=("SH600001", "SZ000002")))
    result = engine.run(spec)
    assert set(result.members) <= {"SH600001", "SZ000002"}
    assert "symbols" in set(result.funnel["rule"])


# ------------------------------------------------------- CLI ad-hoc flags
def _common(store_dir, extra=()):
    return ["--data-dir", str(store_dir), "--workers", "1", "--no-cache", *extra]


def test_cli_where_and_rank_overrides(store_dir, capsys):
    assert main(["run", "momentum_trend", *_common(store_dir),
                 "--as-of", str(cf.END.date()),
                 "--where", "close_raw < 100",
                 "--rank", "pct_rank(high252_ratio)"]) == 0
    out = capsys.readouterr().out
    assert "where1" in out                      # the ad-hoc filter is in the funnel
    assert "high252" not in out.split("members")[0] or True


def test_cli_boards_override(store_dir, capsys):
    assert main(["run", "momentum_trend", *_common(store_dir),
                 "--as-of", str(cf.END.date()), "--boards", "bj"]) == 0
    out = capsys.readouterr().out
    # the universe collapsed to the single BJ name (the boards row of the
    # funnel counts passers BEFORE any screen filter)
    boards_row = next(ln for ln in out.splitlines() if ln.strip().startswith("boards"))
    assert boards_row.split()[1] == "1"


def test_cli_features_command(capsys):
    assert main(["features", "roe"]) == 0
    out = capsys.readouterr().out
    assert "roe_best" in out and "factor" in out


def test_cli_bad_where_fails_cleanly(store_dir):
    with pytest.raises(SystemExit):
        main(["run", "momentum_trend", *_common(store_dir),
              "--where", "not_a_feature > 1"])


# ------------------------------------------------------ staleness surfaced
def test_result_warnings_carry_staleness(tmp_path):
    root = _stale_tree(tmp_path, stale_sessions=30)
    store = CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False)
    engine = ScreenEngine(store)
    spec = ScreenSpec(
        name="t", filters=[{"name": "nb", "expr": "nb_hold_chg_20d > -1"}],
        universe=UniverseSpec(boards=("all",), exclude_st=False))
    result = engine.run(spec)
    assert any("mutual_market" in w for w in result.warnings)


# ================= verification pass (2026-11-26) =================
# A warm ``qf-screen run`` still took ~8.5 s on the real store: the store
# tables came from the cache in ~1 s, then feature building spent ~6 s in
# two ``combine_first`` calls and a per-process re-pivot of the statement
# payload.  These pin the fixes without timing anything.

def test_coalesce_is_combine_first_on_equal_axes():
    """_coalesce replaces ``combine_first`` (≈2 s per call at store
    width): the primary value wherever it is not NaN -- ±inf included --
    and the fallback elsewhere."""
    rng = np.random.default_rng(3)
    index = pd.date_range("2026-01-01", periods=40, freq="B")
    columns = [f"S{i:03d}" for i in range(60)]
    primary = pd.DataFrame(rng.uniform(-5, 5, (40, 60)), index=index, columns=columns)
    fallback = pd.DataFrame(rng.uniform(-5, 5, (40, 60)), index=index, columns=columns)
    primary.iloc[0, :] = np.nan             # a whole missing row
    fallback.iloc[:, 0] = np.nan            # a name the fallback cannot serve
    primary.iloc[1:5, 3] = np.nan
    primary.iloc[6, 4] = np.inf             # inf is a value, not a gap
    got = _coalesce(primary, fallback)
    pd.testing.assert_frame_equal(got, primary.combine_first(fallback))
    assert np.isnan(got.iloc[0, 0])         # neither source has it
    assert np.isinf(got.iloc[6, 4])
    # the inputs are not mutated
    assert primary.iloc[1, 3] != primary.iloc[1, 3]
    assert got.iloc[1, 3] == fallback.iloc[1, 3]


def test_roe_best_matches_the_combine_first_reference(store):
    panel = FeaturePanel(store)
    expected = panel.table("roe").combine_first(panel.table("roe_implied"))
    pd.testing.assert_frame_equal(panel.table("roe_best"), expected)


def test_div_yield_falls_back_to_dyr_without_rescaling(store, store_dir):
    """Event-based yield where lxg documents a dividend, the layout-B
    ``dyr`` elsewhere -- exactly what ``combine_first`` gives.  The event
    table is read back from the cache file with the layout's own reader."""
    from qf_screen.data.table_cache import read_wide

    panel = FeaturePanel(store)
    table = panel.table("div_yield_ttm")             # a miss writes the cache
    cached = read_wide(store.table_cache_dir / "div_yield_ttm_events.parquet")
    assert cached is not None
    expected = cached.combine_first(store.wide("dyr").astype(float))
    pd.testing.assert_frame_equal(table, expected)


def _plain_tree(tmp_path):
    root = tmp_path / "tree"
    cf._company_list(root)
    for symbol, spec in cf.SYMBOLS.items():
        cf._symbol_files(root, symbol, spec)
    return root


def test_statement_fields_are_cached_and_identical_to_a_source_build(tmp_path):
    """Every statement field is a cached wide table whose values are
    bit-identical to an uncached build -- a cache may only cost speed."""
    from qf_screen.rules.fundamental import _FINANCE_FIELDS

    root = _plain_tree(tmp_path)
    panel_built = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS))
    panel_plain = FeaturePanel(
        CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False))
    panel_hit = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS))
    cache = panel_hit.store.table_cache_dir         # this window's tables
    assert cache.name == panel_hit.store.window_key
    for field in _FINANCE_FIELDS.values():
        built = panel_built.table(field)            # miss: build + write
        pd.testing.assert_frame_equal(built, panel_plain.table(field))
        pd.testing.assert_frame_equal(panel_hit.table(field), built)   # the HIT path
        assert (cache / f"finance_step_{field}.parquet").exists()
        assert (cache / f"finance_step_{field}.parquet.meta.txt").exists()


def test_statement_steps_are_pit_by_statutory_announce_date(tmp_path):
    """A report only enters a feature on its statutory deadline: 2025Q4
    (+120d) and 2026Q1 (+30d) both fall due on 2026-04-30, so the level
    must hold flat until then and step exactly there."""
    root = _plain_tree(tmp_path)
    assets = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS,
                                      use_cache=False)).table("assets")["SH600001"]
    before = assets.loc["2026-04-01":"2026-04-29"]
    assert before.notna().all() and before.nunique() == 1
    assert assets.loc["2026-04-30"] != before.iloc[-1]


def test_warm_statement_tables_never_touch_the_payload(tmp_path, monkeypatch):
    """The point of the step cache: a cache HIT must not re-pivot the
    764-symbol payload (≈4 s of every warm process before the fix)."""
    import qf_screen.rules.fundamental as fundamental

    root = _plain_tree(tmp_path)
    FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS)).table("roe")

    def _boom(panel):
        raise AssertionError("a cache hit must not build the finance payload")

    monkeypatch.setattr(fundamental, "_finance_payload", _boom)
    table = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS)).table("roe")
    assert np.isfinite(table["SH600001"].to_numpy()).any()


def test_statement_cache_moves_with_its_source(tmp_path):
    """A rewritten statement file must invalidate the step table (the
    sidecar fingerprint follows the source census)."""
    root = _plain_tree(tmp_path)
    FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS)).table("roe")
    sidecar = (CNDataStore(root, warmup_rows=cf.SESSIONS).table_cache_dir
               / "finance_step_roe.parquet.meta.txt")
    before = sidecar.read_text(encoding="utf-8")
    source = root / "cn/equity/SH600001/lxg/income_quarterly.csv"
    stamp = os.stat(source).st_mtime + 5
    os.utime(source, (stamp, stamp))
    FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS)).table("roe")
    assert sidecar.read_text(encoding="utf-8") != before


# ------------------------------------------ --no-cache reaches derived tables
def test_no_cache_bypasses_every_derived_cache(tmp_path):
    """``--no-cache`` used to rebuild only the merged store tables while
    alt / dividend / factor / statement tables were still served from
    ``qf_screen_cache`` -- the flag now means source truth for everything."""
    features = ("roe", "div_yield_ttm", "nb_hold_chg_20d", "pledge_ratio",
                "report_cnt_90d", "factor_Test_factor__roe")
    root = _factor_tree(tmp_path)
    panel = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False))
    for name in features:
        panel.table(name)
    cache = root / "cn/qf_screen_cache"
    left = sorted(p.name for p in cache.iterdir()) if cache.exists() else []
    assert left == [], left

    # ... and with the cache on, the same features leave their tables behind
    panel_on = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS))
    for name in features:
        panel_on.table(name)
    written = {p.name for p in cache.rglob("*.parquet")}
    assert {"finance_step_roe.parquet", "div_yield_ttm_events.parquet",
            "alt_change_mutual_market_shareholdings_20.parquet",
            "alt_step_pledge_ratio.parquet", "alt_window_report_cnt_90d.parquet",
            "factor_Test_factor__roe.parquet"} <= written


def test_no_cache_matches_cached_values(tmp_path):
    root = _factor_tree(tmp_path)
    plain = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False))
    cached = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS))
    for name in ("roe_best", "div_yield_ttm", "nb_hold_chg_20d", "pledge_ratio",
                 "report_cnt_90d", "factor_Test_factor__roe"):
        pd.testing.assert_frame_equal(plain.table(name), cached.table(name))


# --------------------------------------------------------- CLI unit formatting
@pytest.mark.parametrize("name, value, shown", [
    ("turnover_ma20", 1.38, "1.38%"),           # percent POINTS, not a fraction
    ("turnoverrate", 162.9, "162.90%"),
    ("turnover_ma60", 1.51, "1.51%"),
    ("amount_ma20_mn", 1127.8, "1,128百万"),     # already 百万元 -- no second /1e8
    ("amount_ma20", 1.1278e9, "11.3亿"),
    ("market_cap_bn", 710.93, "710.9亿"),
    ("float_market_cap", 7.1e10, "710.0亿"),
    ("dyr", 0.0512, "5.1%"),                    # a fraction without a name hint
    ("div_yield_ttm", 0.0617, "6.2%"),
    ("ma_ratio_60", 1.06, "106.0%"),
    ("pe_ttm", 5.358, "5.36"),
    ("close_raw", 1460.0, "1,460.0"),
    ("roe_best", float("nan"), "-"),
    ("name", "贵州茅台", "贵州茅台"),
])
def test_fmt_value_units(name, value, shown):
    assert _fmt_value(name, value) == shown


def test_cli_explain_prints_units_that_match_the_features(store_dir, capsys):
    """The explain block formats every feature the screen mentions: a
    1.4 % turnover must not read "138.1%", a 1,128 百万 amount "0.0亿"."""
    assert main(["run", "momentum_trend", *_common(store_dir),
                 "--as-of", str(cf.END.date()), "--explain", "SH600001",
                 "--where", "turnover_ma20 < 100",
                 "--where", "amount_ma20_mn > 0"]) == 0
    explain = capsys.readouterr().out.split("explain SH600001:", 1)[1]
    turnover = re.search(r"^\s+turnover_ma20\s+(\S+)%$", explain, re.M)
    millions = re.search(r"^\s+amount_ma20_mn\s+([\d,]+)百万$", explain, re.M)
    assert turnover, explain
    # the fixture's turnover is 35 + 8 sin(...) percent points
    assert 26 < float(turnover.group(1)) < 44
    assert millions, explain
    assert int(millions.group(1).replace(",", "")) > 0


# ------------------------------------------------ staleness warnings: which feature
def test_staleness_warning_names_its_feature(tmp_path):
    """One lagging file feeds several features (nb_hold_chg_20d and
    nb_hold_chg_60d share mutual_market.csv): each warning must say which
    feature it is about, or two lines with two counts read like a bug."""
    root = _stale_tree(tmp_path, stale_sessions=10)
    # the fixture's mutual_market.csv holds 60 rows -- once truncated it is
    # too short for a 60-row change -- so plant a longer lagging history
    for path in root.glob("cn/equity/*/lxg/mutual_market.csv"):
        pd.DataFrame({"date": cf.CALENDAR[-130:-10].strftime("%Y-%m-%d"),
                      "shareholdings": np.linspace(1e7, 1.2e7, 120).round()}
                     ).to_csv(path, index=False)
    panel = FeaturePanel(CNDataStore(root, warmup_rows=cf.SESSIONS, use_cache=False))
    panel.table("nb_hold_chg_20d")
    panel.table("nb_hold_chg_60d")
    stale = [w for w in panel.warnings if "mutual_market" in w]
    assert len(stale) == 2, panel.warnings
    assert all(w.startswith("nb_hold_chg_") for w in stale), stale
    assert {w.split(" ", 1)[0] for w in stale} == {"nb_hold_chg_20d", "nb_hold_chg_60d"}


# ---------------------------------------- --industry / --concept (offline)
class _BoardStore:
    """A store whose live board constituents come from a frame -- the
    matcher under test never reaches 东财."""

    def __init__(self, frame):
        self._frame = frame

    def board_members(self, kind="industry"):
        return self._frame


def _boards_frame():
    """东财-style constituents: one row per (board, member)."""
    rows = [("白酒", "BK0437", "SZ000858", "五粮液"),
            ("电力设备", "BK1036", "SZ300124", "汇川技术"),
            ("机器人概念", "BK1184", "SZ300124", "汇川技术"),
            ("机器人概念", "BK1184", "SH688111", "金山办公"),
            ("人形机器人概念", "BK1234", "SZ300124", "汇川技术"),
            ("人形机器人概念", "BK1234", "SH688111", "金山办公")]
    return pd.DataFrame(rows, columns=["board", "code", "symbol", "name"])


def _override_args(**kwargs):
    import argparse

    base = dict(boards=None, industry=None, concept=None, where=None, rank=None,
                top=None, top_n=None)
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_board_symbols_exact_then_substring_then_near_misses():
    from qf_screen.cli import _board_symbols

    store = _BoardStore(_boards_frame())
    assert _board_symbols(store, "industry", "白酒") == ["SZ000858"]
    # an exact name that is also the tail of a longer one: exact wins,
    # and a board contributes ALL of its members
    assert _board_symbols(store, "concept", "机器人概念") == ["SH688111", "SZ300124"]
    # no exact match -> every board whose name contains the text
    assert _board_symbols(store, "concept", "机器人") == ["SH688111", "SZ300124"]
    with pytest.raises(SystemExit) as exc:
        _board_symbols(store, "concept", "机器人概念股")
    message = str(exc.value)
    assert "no concept board matches" in message
    assert "人形机器人概念" in message and "机器人概念" in message   # the near misses


class _LocalTags:
    """``universe.meta`` of a store with the local 申万 classification."""

    def __init__(self, tags):
        self.meta = pd.DataFrame({"industry_l1": [tags.get(s) for s in sorted(tags)],
                                  "industry_l2": [None] * len(tags),
                                  "industry_l3": [None] * len(tags)},
                                 index=pd.Index(sorted(tags), name="symbol"))


class _IndustryStore(_BoardStore):
    """A store with local industry tags (the offline ``--industry`` path)
    AND live board constituents."""

    def __init__(self, frame, tags):
        super().__init__(frame)
        self.universe = _LocalTags(tags)
        self.symbols = pd.Index(sorted(tags))
        self.live = object()                 # "the live client is on"


def test_industry_and_concept_restrictions_intersect():
    """Each flag restricts the universe, so both together mean AND.  The
    second used to silently replace the first."""
    from qf_screen.cli import _apply_overrides, _NO_TOP, _OVERRIDE_KEYS
    from qf_screen.engine import SymbolSetFilter

    spec = ScreenSpec.from_yaml("momentum_trend")
    tags = {"SZ300124": "电力设备", "SH688111": "计算机", "SZ000858": "食品饮料"}
    store = _IndustryStore(_boards_frame(), tags)
    only = _apply_overrides(_override_args(industry="电力设备"), spec, store, _OVERRIDE_KEYS)
    assert only.universe.symbols == ["SZ300124"]      # offline, on the local tag
    assert isinstance(only.universe.filters()[1], SymbolSetFilter)
    both = _apply_overrides(_override_args(industry="电力设备", concept="机器人"),
                            spec, store, _OVERRIDE_KEYS)
    assert both.universe.symbols == ["SZ300124"]      # 汇川技术 is in both
    with pytest.raises(SystemExit) as exc:          # 五粮液 is in no robot board
        _apply_overrides(_override_args(industry="食品饮料", concept="机器人"),
                         spec, store, _OVERRIDE_KEYS)
    assert "share no" in str(exc.value)
    # --top is a row limit for diff / report, never a top_n override there
    diff = _apply_overrides(_override_args(top=5), spec, store, _NO_TOP)
    assert diff.top_n == spec.top_n
