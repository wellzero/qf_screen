"""Rule-layer tests: features, filters, NA semantics, and the equivalence
of the vectorized MAX against qf_factor's primitive."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from conftest import SESSIONS

from qf_screen.rules import (
    ExprFilter, ExprRanker, FeaturePanel, feature_names,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def panel(store_module):
    return FeaturePanel(store_module)


@pytest.fixture(scope="module")
def store_module(store_dir):
    from qf_screen.data import CNDataStore
    return CNDataStore(store_dir, warmup_rows=SESSIONS)


def test_feature_registry_covers_the_families(panel):
    names = set(feature_names())
    for family in ("close", "ret_21d", "mom_126s21", "vol_60d", "ma_ratio_200",
                   "high252_ratio", "max21", "amount_ma20", "pe_ttm", "pb",
                   "earnings_yield", "roe", "debt_assets", "div_yield_ttm",
                   "nb_hold_chg_20d", "margin_chg_20d", "report_cnt_90d",
                   "st_flag", "listed_days", "board", "industry_l1"):
        assert family in names, family


def test_price_features_align_to_calendar(panel):
    pos = len(panel.calendar) - 1
    mom = panel.cross_section("mom_126s21", pos)
    assert len(mom) == len(panel.symbols)
    assert np.isfinite(mom).any()
    # the drifting names have positive 126d momentum, the declining ST does not
    idx = {s: i for i, s in enumerate(panel.symbols)}
    assert mom[idx["SH600001"]] > 0
    assert mom[idx["SH600005"]] < 0


def test_st_flag_never_backdated(panel, store_module):
    """The synthetic company list's mtime postdates the whole calendar, so
    the snapshot speaks for NO session in it: the flag must be all False
    (unknown != ST)."""
    flag = panel.table("st_flag")["SH600005"]
    assert not flag.any()
    assert store_module.universe.names_asof > panel.calendar[-1]


def test_na_semantics_fail_closed(panel):
    pos = len(panel.calendar) - 1
    # SH600003 has no lxg statements -> roe NaN -> must NOT pass
    flt = ExprFilter("roe", "roe > 0.10")
    passed = flt.passes(panel, pos)
    idx = {s: i for i, s in enumerate(panel.symbols)}
    assert not passed[idx["SH600003"]]
    assert passed[idx["SH600001"]]
    # na_pass lets the unknown name through
    relaxed = ExprFilter("roe", "roe > 0.10", na_pass=True)
    assert relaxed.passes(panel, pos)[idx["SH600003"]]


def test_finance_features_are_pit_stepped(panel):
    """roe carries the newest ANNOUNCED report only: before its statutory
    announce date the value must still be the previous report's."""
    roe = panel.table("roe")["SH600001"].dropna()
    assert len(roe) > 0
    # steps are flat between announcement deadlines
    changes = roe.diff().fillna(0).abs()
    assert (changes > 1e-12).sum() <= 6          # at most one move per quarter


def test_div_yield_tracks_planted_cash_dividend(panel):
    pos = len(panel.calendar) - 1
    idx = {s: i for i, s in enumerate(panel.symbols)}
    yield_now = panel.cross_section("div_yield_ttm", pos)[idx["SH600001"]]
    assert 0 < yield_now < 0.10                  # 0.3 CNY on a ~5 CNY price


def test_max21_matches_qf_factor_primitive():
    """The vectorized MAX must equal qf_factor's row-loop primitive
    (which is correct but ~15 s at full-universe width)."""
    from qf_factor.framework.selection.screens import max_lottery_score
    from qf_screen.rules import FEATURES

    rng = np.random.default_rng(7)
    close = pd.DataFrame(
        np.exp(np.cumsum(rng.normal(0, 0.02, (120, 6)), axis=0)) * 10,
        index=pd.bdate_range("2026-01-01", periods=120),
        columns=[f"S{i}" for i in range(6)])
    close.iloc[3, 2] = np.nan                    # a suspension must stay NaN

    class _Panel:                                # the builder reads only .store.wide
        class store:
            wide = staticmethod(lambda name: close if name == "close" else None)

    ours = FEATURES["max21"][0](_Panel)
    theirs = max_lottery_score(close, window=21, k=5, lag=0)
    pd.testing.assert_frame_equal(ours, theirs, check_dtype=False)


def test_expr_filter_rejects_unknown_feature():
    with pytest.raises(ValueError, match="unknown feature"):
        ExprFilter("bad", "not_a_feature > 1")


def test_expr_ranker_rejects_boolean():
    with pytest.raises(ValueError):
        ExprRanker("bad", "pe_ttm > 0")


def test_combined_ranker_weight_renormalization(panel):
    from qf_screen.rules import CombinedRanker
    pos = len(panel.calendar) - 1
    ranker = CombinedRanker(
        [ExprRanker("a", "ret_21d"), ExprRanker("b", "mom_63s21")], [3.0, 1.0])
    scores = ranker.score(panel, pos)
    assert np.isfinite(scores).any()
    assert (scores >= 0).all() and (scores <= 1).all()
