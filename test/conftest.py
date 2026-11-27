"""Hermetic test fixtures: a synthetic CN store in ``tmp_path``.

The builder writes the REAL on-disk layouts (lxg layout-A and layout-B
``daily.csv``, ``valuation.csv``, ``dividend.csv``, ak ``basic.csv``,
finance statement pivots, the alt-data files, the company list) with
deterministic content and known corporate actions, so every layer —
universe, providers, adjusted-close rebuild, features, engine — is tested
against the same shapes the production store has, without touching
``QUANT_DATA_DIR`` or the network.

Known planted facts (asserted by the tests):

* ``SH600001`` — layout A, a 10转10 bonus split at ``SPLIT_DAY`` (shares
  double, raw price halves) plus a small cash dividend later; an alias
  directory ``600001`` duplicates it (dedup must collapse them);
* ``SZ000002`` — layout B (valuation embedded), a cash dividend at
  ``DIV_DAY``;
* ``SH600003`` — ak-only (no lxg daily): amount must come from the
  turnover × float-cap proxy;
* ``SH600005`` — ST name in the company list (``*ST`` prefix +
  ``special_treatment``);
* ``BJ430004`` — a Beijing name with minimal data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

SESSIONS = 320
END = pd.Timestamp("2026-06-30")
CALENDAR = pd.bdate_range(end=END, periods=SESSIONS)
SPLIT_DAY = CALENDAR[200]
DIV_DAY = CALENDAR[120]

# daily drifts are per-SESSION (320 sessions ≈ 15 months): +0.12%/day
# compounds to roughly +47% over the window -- a strong but sane trend
SYMBOLS = {
    "SH600001": {"layout": "a", "alias": True, "start": 8.0, "drift": 0.0012},
    "SZ000002": {"layout": "b", "start": 12.0, "drift": 0.0008},
    "SH600003": {"layout": None, "start": 6.0, "drift": 0.0015},
    "SH600005": {"layout": "a", "start": 3.0, "drift": -0.0008},
    "BJ430004": {"layout": "b", "start": 5.0, "drift": 0.0008},
}


def _raw_prices(spec) -> np.ndarray:
    """Deterministic raw close path with the planted corporate actions."""
    rng = np.random.default_rng(int(spec["start"] * 1e6) % 2**31)
    ret = spec["drift"] + rng.normal(0, 0.01, SESSIONS)
    raw = spec["start"] * np.cumprod(1 + ret)
    if spec["layout"] == "a":                      # SH600001's split
        raw[CALENDAR.get_loc(SPLIT_DAY):] /= 2.0
    return raw


def _write_csv(path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _company_list(root):
    rows = []
    for symbol, spec in SYMBOLS.items():
        exchange = symbol[:2].lower()
        code = int(symbol[2:])
        rows.append({
            "name": "*ST宏达" if symbol == "SH600005" else f"公司{code}",
            "market": "a", "exchange": exchange, "areaCode": "cn", "stockCode": code,
            "fsTableType": "bank" if symbol == "SZ000002" else "non_financial",
            "listingStatus": "special_treatment" if symbol == "SH600005"
            else "normally_listed",
            "ipoDate": "2010-01-01T00:00:00+08:00",
            "marginTradingAndSecuritiesLendingFlag": symbol != "BJ430004",
            "mutualMarketFlag": symbol in ("SH600001", "SZ000002"),
            "mutualMarkets": "['ha']" if symbol in ("SH600001", "SZ000002") else "",
        })
    _write_csv(root / "cn/lxg/company_list.csv", pd.DataFrame(rows))


def _symbol_files(root, symbol: str, spec: dict) -> None:
    raw = _raw_prices(spec)
    shares = np.full(SESSIONS, 1e8)
    shares[CALENDAR.get_loc(SPLIT_DAY):] = 2e8 if spec["layout"] == "a" else 1e8
    float_shares = shares * 0.6
    dates = CALENDAR.strftime("%Y-%m-%d")
    # very liquid names: ~35% of float turns over daily, so the presets' 1e8
    # CNY floor is cleared by four of the five symbols (the declining ST
    # name's low price keeps it below -- it must fail the liquidity veto)
    turnover = 35.0 + 8.0 * np.sin(np.arange(SESSIONS))
    amount = raw * float_shares * (turnover / 100.0)
    base = {"date": dates, "close_raw": raw.round(4), "volume": (shares * 0.001).round(),
            "amount": amount.round(2), "turnoverrate": turnover.round(4)}
    # lxg's "adjusted" close: honest ONLY for the split (not the cash div) —
    # mirrors the real store's inconsistency the rebuild must beat
    adjusted = raw.copy()
    split_i = CALENDAR.get_loc(SPLIT_DAY)
    adjusted[:split_i] = raw[:split_i] / 2.0 if spec["layout"] == "a" \
        else raw[:split_i]

    if spec["layout"] == "a":
        lx = pd.DataFrame({**base, "open": raw.round(4), "high": (raw * 1.01).round(4),
                           "low": (raw * 0.99).round(4), "close": adjusted.round(6),
                           "change": np.nan})
        _write_csv(root / f"cn/equity/{symbol}/lxg/daily.csv", lx)
        _write_csv(root / f"cn/equity/{symbol}/lxg/valuation.csv", pd.DataFrame({
            "date": dates, "pe_ttm": (raw / 0.8).round(3), "pb": (raw / 4).round(3),
            "ps_ttm": (raw / 2).round(3), "pcf_ttm": (raw / 1.2).round(3),
            "mc": (raw * shares).round()}))
    elif spec["layout"] == "b":
        _write_csv(root / f"cn/equity/{symbol}/lxg/daily.csv", pd.DataFrame({
            **base, "close": adjusted.round(6),
            "market_capital": (raw * shares).round(), "float_market_capital":
            (raw * float_shares).round(), "pe_ttm": (raw / 0.8).round(3),
            "pb": (raw / 4).round(3), "ps_ttm": (raw / 2).round(3),
            "pcf_ttm": (raw / 1.2).round(3), "dyr": 0.02,
            "total_shares": shares, "float_shares": float_shares}))

    if spec["layout"] is not None:
        if symbol == "SH600001":
            _write_csv(root / f"cn/equity/{symbol}/lxg/dividend.csv", pd.DataFrame([
                {"date": str(SPLIT_DAY.date() - pd.Timedelta(days=20)),
                 "fsEndDate": f"{SPLIT_DAY.year}-12-31T00:00:00+08:00",
                 "currency": "CNY",
                 "registerDate": str(SPLIT_DAY.date() - pd.Timedelta(days=1)),
                 "exDate": str(SPLIT_DAY.date()), "paymentDate": str(SPLIT_DAY.date()),
                 "bonusSharesFromProfit": 0.0, "bonusSharesFromCapitalReserve": 1.0,
                 "dividend": 0.0, "dividendAmount": 0.0, "status": "implemented"},
                {"date": str(DIV_DAY.date() - pd.Timedelta(days=15)),
                 "fsEndDate": f"{DIV_DAY.year}-12-31T00:00:00+08:00", "currency": "CNY",
                 "registerDate": str(DIV_DAY.date() - pd.Timedelta(days=1)),
                 "exDate": str(DIV_DAY.date()), "paymentDate": str(DIV_DAY.date()),
                 "bonusSharesFromProfit": 0.0, "bonusSharesFromCapitalReserve": 0.0,
                 "dividend": 0.3, "dividendAmount": 0.3e8, "status": "implemented"},
                {"date": str(DIV_DAY.date() - pd.Timedelta(days=30)),
                 "fsEndDate": f"{DIV_DAY.year}-06-30T00:00:00+08:00", "currency": "CNY",
                 "registerDate": "", "exDate": "", "paymentDate": "",
                 "bonusSharesFromProfit": 0.0, "bonusSharesFromCapitalReserve": 0.0,
                 "dividend": 0.1, "dividendAmount": np.nan,
                 "status": "board_director_plan"},
            ]))
        _write_csv(root / f"cn/equity/{symbol}/lxg/industries.csv", pd.DataFrame({
            "areaCode": ["cn"] * 3, "source": ["sw_2021"] * 3,
            "name": ["食品饮料", "白酒", "白酒"]}))
        _write_csv(root / f"cn/equity/{symbol}/lxg/mutual_market.csv", pd.DataFrame({
            "date": dates[-60:], "shareholdings": np.linspace(1e7, 1.2e7, 60).round()}))
        _write_csv(root / f"cn/equity/{symbol}/lxg/margin.csv", pd.DataFrame({
            "date": dates[-40:], "financingBalance": np.linspace(2e8, 2.6e8, 40).round()}))
        _write_csv(root / f"cn/equity/{symbol}/lxg/pledge.csv", pd.DataFrame({
            "date": dates[-10:], "pledgeRatio": [0.05] * 10}))
        _write_csv(root / f"cn/equity/{symbol}/lxg/shareholders_num.csv", pd.DataFrame({
            "date": [str(d.date()) for d in CALENDAR[::60]],
            "num": np.linspace(5e4, 4e4, len(CALENDAR[::60])).round()}))
        _finance_pivots(root, symbol)

    # ak basic for every symbol (float mcap from the post-split shares)
    ak = pd.DataFrame({
        "date": dates, "close": raw.round(4),
        "pct_chg": pd.Series(raw).pct_change().mul(100).round(4),
        "turnoverrate": turnover.round(4), "pe_ttm": (raw / 0.8).round(3),
        "pe_static": (raw / 0.85).round(3), "pb": (raw / 4).round(3),
        "ps": (raw / 2).round(3), "pcf": (raw / 1.2).round(3), "peg": 1.1,
        "market_capital": (raw * shares).round(),
        "float_market_capital": (raw * float_shares).round(),
        "total_shares": shares, "float_shares": float_shares})
    _write_csv(root / f"cn/equity/{symbol}/ak/basic.csv", ak)
    if symbol in ("SH600001", "SZ000002"):
        _write_csv(root / f"cn/equity/{symbol}/ak/research_reports.csv",
                   pd.DataFrame([
                       {"report_date": str((END - pd.Timedelta(days=10)).date()),
                        "rating": "买入", "org": "测试证券", "title": "覆盖", "eps_fy1": 1.2},
                       {"report_date": str((END - pd.Timedelta(days=40)).date()),
                        "rating": "中性", "org": "测试证券", "title": "更新", "eps_fy1": 1.1},
                   ]))
    if spec.get("alias"):
        # duplicate tree under the bare code: the universe must dedup them
        import shutil
        target = root / f"cn/equity/{symbol[2:]}"
        shutil.copytree(root / f"cn/equity/{symbol}", target, dirs_exist_ok=True)


def _finance_pivots(root, symbol: str) -> None:
    """Statement pivots in the lxg layout: one row per 指标, one column per
    period, newest period FIRST (the real files' order).

    History reaches back to 2023 so every quarter inside the synthetic
    calendar has the year-ago quarters the TTM construction needs -- a Q2
    or Q3 TTM without them is honestly NaN (the bridge's conservative PIT
    gap), which would leave the quality filters empty mid-window.
    """
    periods = [f"{year}Q{quarter}"
               for year in range(2026, 2022, -1) for quarter in (1, 2, 3, 4)][:13]

    def pivot(rows):
        data = {"指标": [name for name, _ in rows]}
        for i, period in enumerate(periods):
            # newest period carries the largest values (planted growth)
            scale = 1.0 + 0.10 * (len(periods) - 1 - i)
            data[period] = [round(base * scale) for _, base in rows]
        return pd.DataFrame(data)

    _write_csv(root / f"cn/equity/{symbol}/lxg/income_quarterly.csv", pivot([
        ("一、营业总收入", 1e9), ("其中：营业成本", 0.7e9),
        ("三、营业利润", 7e8), ("五、净利润", 6e8)]))
    _write_csv(root / f"cn/equity/{symbol}/lxg/balance_quarterly.csv", pivot([
        ("一、资产总计", 5e9), ("二、负债合计", 2e9),
        ("三、所有者权益合计", 3e9), ("货币资金", 8e8),
        ("短期借款", 3e8), ("长期借款", 4e8)]))
    _write_csv(root / f"cn/equity/{symbol}/lxg/cash_quarterly.csv", pivot([
        ("经营活动产生的现金流量净额", 2e8),
        ("购建固定资产、无形资产及其他长期资产所支付的现金", 5e7)]))


@pytest.fixture(scope="session")
def store_dir(tmp_path_factory):
    root = tmp_path_factory.mktemp("cn_store")
    _company_list(root)
    for symbol, spec in SYMBOLS.items():
        _symbol_files(root, symbol, spec)
    return root


@pytest.fixture()
def store(store_dir):
    from qf_screen.data import CNDataStore

    return CNDataStore(store_dir, warmup_rows=SESSIONS)


@pytest.fixture(autouse=True)
def _user_config_isolated(tmp_path, monkeypatch):
    """The user-level knobs qf_screen reads from the environment -- where
    user presets are discovered, where snapshots are archived, the webhook
    the daily commands push to -- never leak into a test run: a developer
    with ``~/.config/qf_screen/presets/`` must get the same "every preset
    is a built-in" count as CI, and no test may post anywhere."""
    monkeypatch.setenv("QF_SCREEN_PRESETS_DIR", str(tmp_path / "presets"))
    monkeypatch.setenv("QF_SCREEN_HISTORY_DIR", str(tmp_path / "history"))
    monkeypatch.delenv("QF_SCREEN_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("QF_SCREEN_WEBHOOK_SECRET", raising=False)


@pytest.fixture(autouse=True)
def _no_webhook_network(monkeypatch):
    """No test may reach the network.  The webhook's one network call
    (:func:`qf_screen.notify.post`) is replaced by a tripwire; a test that
    wants a delivered webhook installs its own recorder on top of it."""
    import qf_screen.notify as notify

    def tripwire(*_args, **_kwargs):
        raise AssertionError("qf_screen.notify.post reached for the network inside a test")

    tripwire.original = notify.post         # for the one test of `post` itself
    monkeypatch.setattr(notify, "post", tripwire)
