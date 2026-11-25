"""Data-layer tests on the synthetic store: universe dedup, provider merge,
adjusted-close rebuild, amount proxy, provenance, mocked live client."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from conftest import CALENDAR, DIV_DAY, SESSIONS, SPLIT_DAY, SYMBOLS

from qf_screen.data import (
    LiveDataError, build_adjusted_close, build_universe, prepare_dividend_events,
)
from qf_screen.data.ak_provider import LiveAkClient, normalize_spot

pytestmark = pytest.mark.unit


# ------------------------------------------------------------- universe
def test_universe_dedups_alias_directories(store_dir):
    universe = build_universe(store_dir)
    assert "SH600001" in universe
    assert universe.dir_names("SH600001") == ("SH600001", "600001")
    # both spellings resolve to ONE stock: no duplicate symbols anywhere
    assert not universe.symbols.duplicated().any()
    assert "600001" not in universe.symbols


def test_universe_metadata_and_st_flag(store_dir):
    universe = build_universe(store_dir)
    assert universe.get("SH600001", "board") == "sh_main"
    assert universe.get("SZ000002", "board") == "sz_main"
    assert universe.get("BJ430004", "board") == "bj"
    assert bool(universe.get("SH600001", "connect"))
    assert not bool(universe.get("BJ430004", "connect"))
    assert universe.get("SH600001", "industry_l1") == "食品饮料"
    assert universe.st_named == {"SH600005"}
    assert universe.names_asof is not None


def test_board_resolution_errors():
    from qf_screen.data.universe import resolve_boards
    assert resolve_boards(["main"]) == ("sh_main", "sz_main")
    assert resolve_boards(["沪市主板"]) == ("sh_main",)
    assert resolve_boards(["ALL"]) == ("sh_main", "sz_main", "chinext", "star", "bj")
    with pytest.raises(ValueError):
        resolve_boards(["nasdaq"])


# ---------------------------------------------------------------- store
def test_store_loads_every_symbol_with_data(store):
    assert set(store.provenance.index) == set(SYMBOLS)
    assert len(store.calendar) == SESSIONS
    assert store.calendar[0] == CALENDAR[0]


def test_split_and_dividend_rebuild(store):
    """The planted 10转10 must NOT appear in the adjusted close, and the
    cash dividend must (as a small step); the RAW close keeps both."""
    close = store.wide("close")["SH600001"]
    raw = store.wide("close_raw")["SH600001"]
    i = store.calendar.get_loc(SPLIT_DAY)
    raw_step = raw.iloc[i] / raw.iloc[i - 1] - 1
    adj_step = close.iloc[i] / close.iloc[i - 1] - 1
    assert raw_step < -0.45                      # the halving is in the raw price
    assert abs(adj_step) < 0.10                  # ... and removed when adjusted
    j = store.calendar.get_loc(DIV_DAY)
    adj_div = close.iloc[j] / close.iloc[j - 1] - 1
    raw_div = raw.iloc[j] / raw.iloc[j - 1] - 1
    # total-return adjustment: the adjusted return EXCEEDS the raw by the
    # 0.3 CNY dividend relative to the pre-event price
    assert adj_div - raw_div == pytest.approx(0.3 / raw.iloc[j - 1], rel=0.05)
    info = store.provenance.loc["SH600001"]
    assert info["dividend_adj"] >= 2
    assert info["split_adj"] == 0                # the documented event wins


def test_ak_only_symbol_uses_amount_proxy(store):
    info = store.provenance.loc["SH600003"]
    assert info["amount_source"] == "ak_proxy"
    amount = store.wide("amount")["SH600003"].dropna()
    assert len(amount) > SESSIONS * 0.9
    assert (amount > 0).all()
    assert store.provenance.loc["SH600001", "amount_source"] == "lxg"


def test_layout_b_valuation_and_share_counts(store):
    frame = store.wide("market_cap")["SZ000002"].dropna()
    assert len(frame) > SESSIONS * 0.9
    assert store.provenance.loc["SZ000002", "val_source"] in ("lxg", "mixed")


def test_build_adjusted_close_rejects_implausible_event():
    days = pd.bdate_range("2026-01-01", periods=5)
    raw = np.array([10.0, 10.2, 10.4, 10.1, 10.3])
    events = pd.DataFrame({"ex_date": [days[3]], "bonus": [50.0], "cash": [0.0]})
    adj, info = build_adjusted_close(raw, days, dividend_events=events)
    assert info["dividend_rejected"] == 1        # > EVENT_LIMIT: bad data
    np.testing.assert_allclose(adj, raw)         # left as raw returns


def test_prepare_dividend_events_filters_status():
    events = pd.DataFrame({
        "ex_date": [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-02-02")],
        "bonus_profit": [0.0, 1.0], "bonus_reserve": [0.0, 0.0],
        "cash_per_share": [0.5, 0.0],
        "status": ["board_director_plan", "implemented"],
        "currency": ["CNY", "CNY"]})
    out = prepare_dividend_events(events)
    assert len(out) == 1 and out.iloc[0]["bonus"] == 1.0


# ------------------------------------------------------------ live mock
class _FakeAk:
    def __init__(self):
        self.spot_calls = 0

    def stock_zh_a_spot_em(self):
        self.spot_calls += 1
        return pd.DataFrame({
            "代码": ["600001", "000002"], "名称": ["公司一", "公司二"],
            "最新价": [11.0, 13.0], "涨跌幅": [1.0, -0.5], "成交额": [1e8, 2e8],
            "换手率": [1.5, 2.0], "市盈率-动态": [12.0, 15.0], "市净率": [2.0, 3.0],
            "总市值": [1.1e10, 1.3e10], "流通市值": [6e9, 8e9], "昨收": [10.9, 13.1]})


def test_live_client_off_by_default(tmp_path):
    client = LiveAkClient(tmp_path / "cache", refresh=False, ak=_FakeAk())
    with pytest.raises(LiveDataError):
        client.spot()
    assert client.calls == 0


def test_live_client_caches_within_ttl(tmp_path):
    fake = _FakeAk()
    client = LiveAkClient(tmp_path / "cache", refresh=True, ttl_spot=60, ak=fake)
    first, meta_first = client.spot()
    second, meta_second = client.spot()
    assert fake.spot_calls == 1                  # the second hit was the cache
    assert meta_first["fresh"] and not meta_second["fresh"]
    assert set(first["symbol"]) == {"SH600001", "SZ000002"}
    assert first["market_capital"].notna().all()


def test_normalize_spot_rejects_empty():
    with pytest.raises(LiveDataError):
        normalize_spot(pd.DataFrame())


def test_attach_live_spot_extends_chain(store):
    fake = _FakeAk()
    store.live = LiveAkClient(store.data_dir / "ak_live", refresh=True,
                              ttl_spot=60, ak=fake)
    prev_raw = store.wide("close_raw")["SH600001"].iloc[-1]
    before = store.wide("close")["SH600001"].iloc[-1]
    store.attach_live_spot()
    assert store.live_date == store.calendar[-1]
    assert store.wide("close_raw")["SH600001"].iloc[-1] == pytest.approx(11.0)
    # the adjusted chain extends by live/prev-raw, so it moves with the quote
    expected = before * 11.0 / prev_raw
    assert store.wide("close")["SH600001"].iloc[-1] == pytest.approx(
        float(expected), rel=1e-4)
