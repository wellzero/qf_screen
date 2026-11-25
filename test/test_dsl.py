"""DSL tests: vector semantics, cross-sectional ops, unsafe rejection."""
from __future__ import annotations

import numpy as np
import pytest

from qf_screen.rules import DslError, compile_bool, compile_expr, compile_score, \
    expr_names

pytestmark = pytest.mark.unit

ENV = {
    "pe": np.array([10.0, 20.0, np.nan, -5.0]),
    "roe": np.array([0.1, 0.2, 0.3, np.nan]),
    "mc": np.array([1e10, 2e10, 3e10, 4e10]),
    "_industry": np.array(["A", "A", "B", "B"]),
}


def test_comparison_nan_fails_closed():
    assert compile_bool("pe > 0 and pe < 15")(ENV).tolist() == [True, False, False, False]


def test_not_passes_nan_and_documented_semantics():
    # NaN is not > 12, so 'not (pe > 12)' passes it -- documented DSL behavior
    assert compile_bool("not (pe > 12)")(ENV).tolist() == [True, False, True, True]


def test_pct_rank_and_zscore_exclude_nan():
    ranked = compile_score("pct_rank(mc)")(ENV)
    assert ranked.tolist() == [0.25, 0.5, 0.75, 1.0]
    z = compile_score("zscore(pe)")(ENV)
    assert np.isnan(z[2])
    assert z[0] == pytest.approx(0.16, abs=0.02)


def test_industry_rank_ranks_within_groups():
    ranked = compile_score("industry_rank(roe)")(ENV)
    assert ranked[0] == pytest.approx(0.5)       # two names in industry A
    # industry B has a single finite member: a rank of one name is
    # meaningless, so it is NaN (fails an industry filter conservatively)
    assert np.isnan(ranked[2])


def test_arithmetic_and_fillna_clip():
    assert compile_score("fillna(pe, 100)")(ENV).tolist() == [10.0, 20.0, 100.0, -5.0]
    clipped = compile_score("clip(pe, 0, 15)")(ENV)
    assert clipped[:2].tolist() == [10.0, 15.0] and clipped[3] == 0.0
    assert np.isnan(clipped[2])
    np.testing.assert_allclose(compile_score("log(mc)")(ENV), np.log(ENV["mc"]))
    assert compile_score("1 / pe")(ENV)[0] == pytest.approx(0.1)


def test_ifexp_needs_boolean_condition():
    assert compile_score("pe if pe > 0 else 0")(ENV).tolist() == [10.0, 20.0, 0.0, 0.0]
    with pytest.raises(DslError):
        compile_expr("pe if roe else mc")


def test_expr_names_extracts_features_only():
    assert expr_names("pct_rank(pe) + 0.5 * industry_rank(div_yield_ttm)") == \
        ["div_yield_ttm", "pe"]


@pytest.mark.parametrize("bad", [
    "__import__('os').system('x')", "pe.__class__", "pe[0]", "lambda x: x",
    "eval('1')", "open('/etc/passwd')", "pe == 10", "a < pe < 15",
    "'str' + pe", "pe; import os", "{}", "[1, 2]", "f'{pe}'", "pe if roe else mc",
    "rank", "rank(pe, 2)",
])
def test_unsafe_or_unknown_rejected(bad):
    with pytest.raises(DslError):
        compile_expr(bad)


def test_compile_bool_requires_boolean():
    with pytest.raises(DslError):
        compile_bool("pe")


def test_compile_score_requires_numeric():
    with pytest.raises(DslError):
        compile_score("pe > 0")


def test_syntax_error_is_a_dsl_error():
    with pytest.raises(DslError):
        compile_expr("pe >< 3")
