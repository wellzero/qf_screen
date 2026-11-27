"""``qf_screen.symbols``: the store, both downloaders (fake akshare module /
fake GeoScope client -- never the network), sync, the offline universe /
CNDataStore integration, and the ``qf-screen symbol`` commands."""
from __future__ import annotations

import json
import shutil
from types import SimpleNamespace

import pandas as pd
import pytest

from qf_screen.cli import main
from qf_screen.data.universe import _parse_company_list, build_universe, \
    universe_fingerprint
from qf_screen.symbols import SymbolError, SymbolStore, sync_symbols
from qf_screen.symbols.ak_downloader import download_ak_sections, download_ak_stocks
from qf_screen.symbols.lxg_downloader import GeoScopeClient, \
    download_lxg_sections, download_lxg_stocks

pytestmark = pytest.mark.unit

# ------------------------------------------------------------ fakes
COMPANY_ROWS = [
    {"name": "贵州茅台", "market": "a", "exchange": "sh", "areaCode": "cn",
     "stockCode": "600519", "fsTableType": "non_financial",
     "listingStatus": "normally_listed", "ipoDate": "2001-08-27T00:00:00+08:00",
     "marginTradingAndSecuritiesLendingFlag": True, "mutualMarketFlag": True,
     "mutualMarkets": "['ha']"},
    {"name": "平安银行", "market": "a", "exchange": "sz", "areaCode": "cn",
     "stockCode": 1, "fsTableType": "bank", "listingStatus": "normally_listed",
     "ipoDate": "1991-04-03T00:00:00+08:00",
     "marginTradingAndSecuritiesLendingFlag": True, "mutualMarketFlag": True,
     "mutualMarkets": ""},
    {"name": "B股示例", "market": "b", "exchange": "sh", "areaCode": "cn",
     "stockCode": "900901", "fsTableType": "non_financial",
     "listingStatus": "normally_listed", "ipoDate": "1992-02-21T00:00:00+08:00",
     "marginTradingAndSecuritiesLendingFlag": False, "mutualMarketFlag": False,
     "mutualMarkets": ""},
]


def _fake_ak(primary_down: bool = False):
    """The five akshare endpoints the downloaders use, canned."""
    def info():
        if primary_down:
            raise RuntimeError("endpoint down")
        return pd.DataFrame({"code": ["600519", 1], "name": ["贵州茅台", "平安银行"]})
    return SimpleNamespace(
        stock_info_a_code_name=info,
        stock_zh_a_spot_em=lambda: pd.DataFrame(
            {"代码": ["600036"], "名称": ["招商银行"]}),
        stock_board_industry_name_em=lambda: pd.DataFrame(
            {"板块名称": ["银行", "白酒"]}),
        stock_board_concept_name_em=lambda: pd.DataFrame(
            {"板块名称": ["机器人", "无人驾驶"]}),
        stock_board_industry_cons_em=lambda symbol: pd.DataFrame(
            {"代码": ["600036", "000001"], "名称": ["招商银行", "平安银行"]})
            if symbol == "银行" else pd.DataFrame(
                {"代码": ["600519"], "名称": ["贵州茅台"]}),
        stock_board_concept_cons_em=lambda symbol: pd.DataFrame(
            {"代码": ["600519"], "名称": ["贵州茅台"]})
            if symbol == "机器人" else None,        # 无人驾驶: empty board
    )


class FakeGeo:
    """A GeoScope stand-in: canned payloads, records every POST."""

    def __init__(self, company=COMPANY_ROWS,
                 industry=({"name": "食品饮料", "level": 1},
                           {"industryName": "白酒", "industryLevel": 2}),
                 constituents=({"industry": "食品饮料",
                                "stockCodes": ["600519", "000001"]},
                               {"industryName": "白酒", "stockCode": "600519"})):
        self.company, self.industry, self.constituents = \
            company, industry, constituents
        self.posts = []

    def post(self, path, body):
        self.posts.append((path, body))
        if path.endswith("/company"):
            return self.company
        if path == "cn/industry":
            return self.industry
        if path.startswith("cn/industry/constituents"):
            return self.constituents
        raise AssertionError(f"unexpected path {path}")


# ------------------------------------------------------------ downloaders
def test_ak_stocks_primary_and_fallback():
    frame = download_ak_stocks(ak=_fake_ak())
    assert list(frame["symbol"]) == ["SH600519", "SZ000001"]  # int code zfilled
    frame = download_ak_stocks(ak=_fake_ak(primary_down=True))
    assert list(frame["symbol"]) == ["SH600036"]              # spot fallback
    assert list(frame["name"]) == ["招商银行"]


def test_ak_sections_members_filter_and_empty_board():
    sections, members = download_ak_sections("industry", ak=_fake_ak(), pause=0)
    assert list(sections["name"]) == ["银行", "白酒"]
    assert set(members["symbol"]) == {"SH600036", "SZ000001", "SH600519"}
    assert dict(zip(sections["name"], sections["members"]))["银行"] == "2"

    sections, members = download_ak_sections(
        "industry", ak=_fake_ak(), pause=0, section_filter={"白酒"})
    assert list(sections["name"]) == ["白酒"]
    assert list(members["symbol"]) == ["SH600519"]

    # a concept board with no constituents is skipped, not fatal
    sections, members = download_ak_sections("concept", ak=_fake_ak(), pause=0)
    assert dict(zip(sections["name"], sections["members"]))["无人驾驶"] == "0"
    assert list(members.loc[members["section"] == "机器人", "symbol"]) == ["SH600519"]

    with pytest.raises(SymbolError, match="no concept board passed"):
        download_ak_sections("concept", ak=_fake_ak(), pause=0,
                             section_filter={"不存在"})
    with pytest.raises(SymbolError, match="unknown board kind"):
        download_ak_sections("style", ak=_fake_ak())


def test_lxg_stocks_raw_rows():
    client = FakeGeo()
    raw = download_lxg_stocks(client=client)
    assert len(raw) == 3 and client.posts == [("cn/company", {})]  # unfiltered


def test_lxg_stocks_need_key(monkeypatch):
    monkeypatch.delenv("LIXINGER_API_KEY", raising=False)
    monkeypatch.delenv("GEOSCOPE_API_KEY", raising=False)
    with pytest.raises(SymbolError, match="no GeoScope key"):
        download_lxg_stocks()


def test_geoscope_client_never_uses_a_proxy():
    """The key-suspension rule: direct connection, always."""
    client = GeoScopeClient("gfc_live_test")
    assert client._session.trust_env is False
    assert client._session.proxies == {"http": None, "https": None}
    assert client._session.headers["Authorization"] == "Bearer gfc_live_test"


def test_lxg_sections_shapes_and_empty_constituents():
    client = FakeGeo()
    sections, members = download_lxg_sections(client=client)
    assert client.posts == [("cn/industry", {"source": "sw_2021"}),
                            ("cn/industry/constituents/sw_2021", {})]
    assert list(sections["name"]) == ["食品饮料", "白酒"]
    assert list(sections["level"]) == ["1", "2"]          # int 1 normalized
    assert set(map(tuple, members[["section", "symbol"]].to_numpy())) == {
        ("食品饮料", "SH600519"), ("食品饮料", "SZ000001"), ("白酒", "SH600519")}

    # the constituents endpoint has been empty upstream: sections still land
    client = FakeGeo(constituents=())
    sections, members = download_lxg_sections(client=client)
    assert len(sections) == 2 and len(members) == 0

    with pytest.raises(SymbolError, match="no section names"):
        download_lxg_sections(client=FakeGeo(industry=({"level": 1},)))


# ------------------------------------------------------------ the store
def test_store_crud_persistence_and_errors(tmp_path):
    store = SymbolStore(tmp_path, "cn")
    assert not store.present

    store.upsert_section("机器篮子", note="first")
    with pytest.raises(SymbolError, match="not a CN A-share"):
        store.add_section_members("机器篮子", ["AAPL"])
    assert store.add_section_members("机器篮子", ["SH600519", "600036"]) == 2
    assert store.add_section_members("机器篮子", ["600519"]) == 0  # dedup
    assert store.section_members("机器篮子") == ["SH600519", "SH600036"]

    row = store.upsert_stock("SH600519", name="贵州茅台")
    assert row["code"] == "600519"
    assert store.get_stock("600519")["name"] == "贵州茅台"
    assert store.remove_stock("SZ000001") == 0            # never there

    # a fresh instance reads the same catalog back
    again = SymbolStore(tmp_path, "cn")
    assert again.present
    assert again.section_members("机器篮子") == ["SH600519", "SH600036"]
    assert again.get_stock("SH600519")["note"] == ""

    assert again.remove_section_members("机器篮子", ["600519"]) == 1
    assert again.delete_section("机器篮子") == 1
    assert again.section_members("机器篮子") == []
    with pytest.raises(SymbolError, match="no custom section"):
        again.delete_section("机器篮子")
    with pytest.raises(SymbolError, match="unknown section kind"):
        again.upsert_section("x", kind="style")


def test_store_bulk_replace_and_board_frame(tmp_path):
    store = SymbolStore(tmp_path, "cn")
    raw = pd.DataFrame(COMPANY_ROWS)
    assert store.replace_stocks("lxg", raw) == 2           # A-shares only
    stocks = store.stocks(source="lxg")
    assert set(stocks["symbol"]) == {"SH600519", "SZ000001"}
    row = stocks.set_index("symbol").loc["SZ000001"]
    assert row["exchange"] == "sz" and row["list_date"] == "1991-04-03"
    assert row["connect"] and row["fs_type"] == "bank"

    sections, members = download_lxg_sections(client=FakeGeo())
    counts = store.replace_sections("lxg", "industry", sections, members)
    assert counts == {"sections": 2, "members": 3}
    assert store.industry_l1() == {"SH600519": "食品饮料", "SZ000001": "食品饮料"}

    sections, members = download_ak_sections("concept", ak=_fake_ak(), pause=0)
    store.replace_sections("ak", "concept", sections, members)
    frame = store.board_frame("concept")
    assert set(frame["board"]) == {"机器人"}               # 无人驾驶 is empty
    assert store.membership(kind="concept").keys() == {"机器人"}
    with pytest.raises(SymbolError, match="bulk-replace"):
        store.replace_sections("custom", "concept", sections, members)


def test_store_local_import(store_dir):
    store = SymbolStore(store_dir, "cn")
    counts = store.import_local_lxg()
    assert counts == {"stocks": 5, "sections": 2, "members": 8}
    sections = store.sections(kind="industry", source="lxg")
    assert dict(zip(sections["name"], sections["level"]))["食品饮料"] == "1"
    assert set(store.section_members("白酒")) == {
        "SH600001", "SZ000002", "SH600005", "BJ430004"}
    assert store.industry_l1()["SH600001"] == "食品饮料"
    assert store.stock_names()["SH600005"] == "*ST宏达"
    status = store.status()
    assert status["present"]
    assert status["sources"]["lxg"]["stocks"]["rows"] == 5
    assert status["sources"]["lxg"]["stocks"]["synced_at"]


def test_mirror_company_list_keeps_the_universe_contract(tmp_path):
    store = SymbolStore(tmp_path, "cn")
    path = store.mirror_company_list(pd.DataFrame(COMPANY_ROWS))
    parsed = _parse_company_list(pd.read_csv(path, dtype=str))
    assert set(parsed.index) == {"SH600519", "SZ000001"}
    assert parsed.at["SZ000001", "name"] == "平安银行"
    assert parsed.at["SH600519", "connect"]


# ------------------------------------------------------------ sync
def test_sync_local_then_failing_source_records_error(store_dir):
    result = sync_symbols(data_dir=store_dir, source="local")
    assert result.ok and result.stocks == {"local": 5}

    import qf_screen.symbols.sync as sync_mod
    original = sync_mod.download_ak_stocks
    sync_mod.download_ak_stocks = lambda **kw: (_ for _ in ()).throw(
        SymbolError("akshare is not installed"))
    try:
        result = sync_symbols(data_dir=store_dir, source="ak",
                              what=("stocks",))
    finally:
        sync_mod.download_ak_stocks = original
    assert not result.ok and result.errors
    assert "akshare is not installed" in result.errors[0]


def test_sync_ak_and_lxg_with_fakes(tmp_path, monkeypatch):
    import qf_screen.symbols.sync as sync_mod

    monkeypatch.setattr(sync_mod, "download_ak_stocks",
                        lambda **kw: download_ak_stocks(ak=_fake_ak()))
    monkeypatch.setattr(sync_mod, "download_ak_sections",
                        lambda **kw: download_ak_sections(pause=0, **{
                            k: v for k, v in kw.items() if k != "pause"}
                            | {"ak": _fake_ak()}))
    result = sync_symbols(data_dir=tmp_path, source="all", client=FakeGeo(),
                          pause=0)
    assert result.ok, result.errors
    assert result.stocks == {"lxg": 2, "ak": 2}
    assert result.sections == {"lxg:industry": 2, "ak:industry": 2,
                               "ak:concept": 2}
    assert result.members == {"lxg:industry": 3, "ak:industry": 3,
                              "ak:concept": 1}
    # the company-list mirror landed (and is parse-compatible)
    assert any(note.startswith("company_list mirror:")
               for note in result.notes)
    assert set(_parse_company_list(pd.read_csv(
        tmp_path / "cn/lxg/company_list.csv", dtype=str)).index) == {
        "SH600519", "SZ000001"}

    result = sync_symbols(data_dir=tmp_path, source="lxg", client=FakeGeo(),
                          mirror_company_list=False)
    assert result.notes == []


# ------------------------------------------------- universe / store wiring
def _private_copy(store_dir, tmp_path):
    """A private copy of the synthetic store (its caches included)."""
    copy = tmp_path / "store"
    shutil.copytree(store_dir, copy)
    return copy


def test_universe_fill_from_symbol_store_and_cache_stability(store_dir, tmp_path):
    root = _private_copy(store_dir, tmp_path)
    sym = SymbolStore(root, "cn")
    sym.import_local_lxg()
    sym.upsert_section("我的行业", kind="industry", note="curated")
    sym.add_section_members("我的行业", ["SH600003"])   # the ak-only, untagged name

    plain = universe_fingerprint(root / "cn/equity", root / "cn/lxg/company_list.csv")
    catalog = universe_fingerprint(root / "cn/equity",
                                   root / "cn/lxg/company_list.csv",
                                   symbol_root=sym.root)
    assert catalog.startswith(plain + ":")            # the catalog joined in

    first = build_universe(root, symbol_store=sym)
    assert first.get("SH600003", "industry_l1") is None  # custom ≠ lxg L1 fill
    names = sym.stock_names()
    assert names["SH600003"] == "公司600003"

    # lxg L1 sections DO fill a gap: wipe one industries.csv, rebuild
    (root / "cn/equity/BJ430004/lxg/industries.csv").unlink()
    second = build_universe(root, symbol_store=sym, use_cache=False)
    assert second.get("BJ430004", "industry_l1") == "食品饮料"
    assert second.get("SH600001", "industry_l1") == "食品饮料"  # untouched


def test_store_serves_board_members_and_tags_offline(store_dir, tmp_path):
    from qf_screen.data import CNDataStore, LiveDataError

    root = _private_copy(store_dir, tmp_path)
    store = CNDataStore(root, warmup_rows=320, use_cache=False)
    with pytest.raises(LiveDataError):        # nothing local yet, no --live
        store.board_members("industry")
        store.board_members("concept")

    sym = store.symbol_store                  # the store the CNDataStore holds
    sym.upsert_section("机器人", kind="concept")
    sym.add_section_members("机器人", ["SH600001", "SZ000002"])
    sym.upsert_section("定制作业", kind="industry")
    sym.add_section_members("定制作业", ["SH600003"])

    frame = store.board_members("concept")               # served offline
    assert set(frame["board"]) == {"机器人"}
    assert set(frame["symbol"]) == {"SH600001", "SZ000002"}

    filled = store.enrich_industry_tags()                # offline now
    assert filled == 1
    assert store.universe.get("SH600003", "industry_l1") == "定制作业"
    assert store.universe.get("SH600001", "industry_l1") == "食品饮料"


# ------------------------------------------------------------ the CLI
def _common(data_dir):
    return ["--data-dir", str(data_dir)]


def test_symbol_cli_status_sync_query_flow(store_dir, tmp_path, capsys):
    empty = tmp_path / "empty"                 # a fresh root: nothing synced
    empty.mkdir()
    assert main(["symbol", "status", *_common(empty), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["present"] is False

    assert main(["symbol", "sync", "--source", "local", *_common(store_dir)]) == 0
    capsys.readouterr()
    assert main(["symbol", "status", *_common(store_dir), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sources"]["lxg"]["stocks"]["rows"] == 5

    assert main(["symbol", "stocks", "-q", "600001", *_common(store_dir)]) == 0
    assert "SH600001" in capsys.readouterr().out
    assert main(["symbol", "sections", "--kind", "industry",
                 *_common(store_dir), "--format", "csv"]) == 0
    assert "食品饮料" in capsys.readouterr().out
    assert main(["symbol", "members", "白酒", *_common(store_dir)]) == 0
    assert "SH600001" in capsys.readouterr().out
    assert main(["symbol", "lookup", "白酒", *_common(store_dir)]) == 0
    assert "1 section(s)" in capsys.readouterr().out


def test_symbol_cli_custom_sections(store_dir, capsys):
    assert main(["symbol", "add-section", "机器篮子", "--kind", "concept",
                 "--note", "holdings", *_common(store_dir)]) == 0
    capsys.readouterr()
    assert main(["symbol", "add-members", "机器篮子", "600001,SZ000002",
                 *_common(store_dir)]) == 0
    assert "added 2" in capsys.readouterr().out
    assert main(["symbol", "remove-members", "机器篮子", "SZ000002",
                 *_common(store_dir)]) == 0
    assert "removed 1" in capsys.readouterr().out
    assert main(["symbol", "remove-section", "机器篮子", *_common(store_dir)]) == 0
    assert "removed section" in capsys.readouterr().out


def test_symbol_cli_errors_are_one_line(store_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["symbol", "members", "不存在", *_common(store_dir)])
    assert "has no members" in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        main(["symbol", "remove-section", "不存在", *_common(store_dir)])
    assert "no custom section" in str(exc.value)


def test_run_concept_and_industry_offline_from_catalog(store_dir, capsys):
    assert main(["symbol", "sync", "--source", "local", *_common(store_dir)]) == 0
    capsys.readouterr()
    assert main(["symbol", "add-section", "机器人", "--kind", "concept",
                 *_common(store_dir)]) == 0
    assert main(["symbol", "add-members", "机器人", "SH600001,SZ000002",
                 *_common(store_dir)]) == 0
    capsys.readouterr()
    assert main(["run", "--workers", "1", "--as-of", "2026-06-30",
                 "--where", "close > 0", "--concept", "机器人", "--top", "5",
                 *_common(store_dir)]) == 0
    out = capsys.readouterr().out
    assert "SH600001" in out and "SZ000002" in out
    assert "SH600003" not in out and "SH600005" not in out

    # the synced lxg 申万 L1 answers --industry offline
    assert main(["run", "--workers", "1", "--as-of", "2026-06-30",
                 "--where", "close > 0", "--industry", "食品饮料", "--top", "5",
                 *_common(store_dir)]) == 0
    out = capsys.readouterr().out
    assert "SH600001" in out and "SH600003" not in out

    # a miss names the fix, offline and without --live
    with pytest.raises(SystemExit) as exc:
        main(["run", "--workers", "1", "--as-of", "2026-06-30",
              "--where", "close > 0", "--concept", "不存在",
              *_common(store_dir)])
    assert "symbol sync" in str(exc.value)


def test_symbol_sync_json_and_section_filter(tmp_path, capsys, monkeypatch):
    import qf_screen.symbols.sync as sync_mod

    monkeypatch.setattr(sync_mod, "download_ak_stocks",
                        lambda **kw: download_ak_stocks(ak=_fake_ak()))
    monkeypatch.setattr(sync_mod, "download_ak_sections",
                        lambda **kw: download_ak_sections(
                            pause=0, ak=_fake_ak(),
                            section_filter=kw.get("section_filter")))
    assert main(["symbol", "sync", "--source", "ak", "--section-filter",
                 "白酒,机器人", "--json", *_common(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"], payload["errors"]
    assert payload["stocks"] == {"ak": 2}
    assert payload["sections"] == {"ak:industry": 1, "ak:concept": 1}
    assert payload["members"] == {"ak:industry": 1, "ak:concept": 1}
