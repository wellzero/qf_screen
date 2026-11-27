"""The built-in preset library itself: structure, compilation, hygiene.

Every ``qf_screen/presets/*.yaml`` must load through the real loader,
validate against the schema, compile every filter as a boolean DSL
expression and every rank leg as a numeric one, carry the required
hygiene block, and be documented -- hermetically, no store needed.  The
catalog is pinned so a stray file cannot ship silently.
"""
from __future__ import annotations

import pytest
import yaml

from qf_screen.engine import ScreenSpec
from qf_screen.presets import (
    REQUIRED_HYGIENE, built_in_presets, load_preset, preset_path,
)

#: the shipped catalog: 5 originals + 45 new (PLAN_presets.md) + 4
#: undervalued+tailwind presets (2026-11-27)
CATALOG = {
    # originals
    "low_vol_defensive", "momentum_trend", "quality_growth",
    "smart_money_ashare", "value_dividend",
    # value & income
    "deep_value_pb", "fcf_cash_cow", "magic_formula", "high_dividend_yield",
    "dividend_growth_quality", "low_ps_sales_value", "low_pcf_cash_value",
    # quality & financial strength
    "high_roe_compounders", "net_cash_fortress", "high_margin_moat",
    "cash_earnings_quality", "quality_at_reasonable_price",
    "financial_blue_chips",
    # growth & GARP
    "garp_peg", "hyper_growth_revenue", "earnings_acceleration",
    "growth_momentum", "value_turnaround_growth",
    # technical / momentum / breakout
    "breakout_52w_high", "long_term_momentum_12m", "short_term_momentum_3m",
    "steady_uptrend_low_vol", "volume_expansion_trend",
    "low_turnover_neglected", "volatility_compression",
    # mean reversion & pullback
    "uptrend_pullback", "short_term_reversal_quality", "deep_oversold_value",
    "quiet_pullback_low_vol",
    # size & board-specific
    "small_cap_value", "small_cap_quality_momentum", "mid_cap_core_growth",
    "large_cap_blue_chip", "chinext_growth_leaders", "star_tech_leaders",
    "bj_emerging_leaders",
    # alt / smart money & governance
    "chip_concentration", "analyst_consensus_buy", "margin_momentum_ashare",
    "clean_governance_insider", "institutional_block_value",
    # industry-neutral & multi-factor
    "industry_neutral_value", "industry_neutral_momentum",
    "all_weather_multifactor", "value_momentum_combo",
    # undervalued + tailwind (value with a catalyst, added 2026-11-27)
    "undervalued_tailwind", "davis_double_play", "left_side_accumulation",
    "capital_inflection_value",
}


def test_the_catalog_is_exactly_the_54_named_presets():
    assert set(built_in_presets()) == CATALOG
    assert len(CATALOG) == 54
    # the undervalued+tailwind family ships one directory deep
    for name in ("undervalued_tailwind", "davis_double_play",
                 "left_side_accumulation", "capital_inflection_value"):
        assert preset_path(name).parent.name == "undervalue_tailwind"


@pytest.mark.parametrize("name", sorted(CATALOG))
def test_every_preset_loads_validates_and_compiles(name):
    path = preset_path(name)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))

    # the name is the file stem, and the file is documented
    assert doc["name"] == name
    assert len(str(doc.get("description", "")).strip()) >= 60, \
        "every shipped preset carries a real description"

    spec = ScreenSpec.from_yaml(name)        # schema + boolean filters compile
    assert spec.name == name
    ranker = spec.ranker()                   # every rank leg compiles numeric
    assert ranker is not None, "every shipped preset ranks its passers"
    assert len(ranker.rankers) >= 1

    # the required hygiene block leads the funnel, in order
    loaded = load_preset(name)
    hygiene_exprs = [expr.replace(" ", "")
                     for _n, expr in REQUIRED_HYGIENE]
    head = [str(f["expr"]).replace(" ", "") for f in loaded["filters"][:3]]
    assert head == hygiene_exprs

    # the knobs are sane
    assert spec.top_n is None or (isinstance(spec.top_n, int) and spec.top_n >= 0)
    assert spec.max_per_industry is None or spec.max_per_industry >= 1
    boards = spec.universe.boards
    assert boards and all(isinstance(b, str) for b in boards)


def test_the_two_data_limited_presets_say_so_in_their_description():
    """smart_money_ashare (dead northbound disclosure) and
    bj_emerging_leaders (no BJ prices in the default merge) ship anyway --
    their descriptions must keep explaining why their membership can be
    tiny, so a 0-member run reads as data honesty, not a bug."""
    for name, needle in (("smart_money_ashare", "northbound"),
                         ("bj_emerging_leaders", "ONE Beijing name")):
        text = load_preset(name)["description"]
        assert needle in text, name


def test_inspect_survives_a_failed_boards_rule(store):
    """A name that FAILS a preset's board restriction reaches the failed-
    rule card, whose "what the rule read" leg used to parse the
    BoardFilter's DESCRIPTION as a DSL expression -- and 'boards: bj' does
    not parse.  Surfaced when the 50-preset library added a Beijing-only
    preset: inspecting any main-board name through every preset crashed."""
    from qf_screen.engine import ScreenEngine

    engine = ScreenEngine(store)
    card = engine.inspect("SH600001", presets=["bj_emerging_leaders"])
    entry = card.presets[0]
    assert entry["preset"] == "bj_emerging_leaders" and not entry["member"]
    boards = [f for f in entry["failed"] if f["rule"] == "boards"]
    assert boards and boards[0]["features"] == {}   # quoted nothing, crashed not
