"""Lixinger catalog downloads through the GeoScope member proxy.

``https://geoscopeapp.com/api/v1/data/lixinger/`` -- the server holds the
Lixinger open-platform credentials and only forwards fixed read-only paths
of the official catalog; ``www.lixinger.com`` itself is never touched (its
anti-bot has suspended keys before).

Connection policy, copied verbatim from
:mod:`quant_free.datapull.lxg_geoscope_download` (user directive of
2026-11-02): **always direct** -- ``session.trust_env = False`` AND explicit
``proxies = None``, so neither ``HTTP(S)_PROXY``/``ALL_PROXY`` nor a later
``trust_env`` flip can route these calls through a proxy.  Rotating proxy
exits are exactly what trips GeoScope's "use from several countries or
networks" risk control and gets the key suspended.

The account-level quota is 60 req/min; a symbol sync makes ~3 calls, but the
real client still paces itself to ~55/min.

The client is injectable: anything with ``post(path, body) -> data`` (the
tests install a fake); ``None`` builds the real one.  The key comes from
``LIXINGER_API_KEY`` (fallback ``GEOSCOPE_API_KEY``).
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd

from quant_free.common.symbol_format import to_canonical

from .store import SymbolError

logger = logging.getLogger(__name__)

GEO_BASE = "https://geoscopeapp.com/api/v1/data/lixinger"
#: account quota is 60/min -- pace at ~55 to stay clear of it
_RATE_GAP = 60.0 / 55.0

#: row fields an industry listing may spell its name / level with (the
#: open-platform field names are not documented; first hit wins)
_NAME_KEYS = ("name", "industryName", "industry_name", "industry")
_LEVEL_KEYS = ("level", "industryLevel", "industry_level")
#: ... and a constituents row its section name / stock code
_MEMBER_SECTION_KEYS = ("industry", "industryName", "industry_name", "name",
                        "section")
_MEMBER_CODE_KEYS = ("stockCode", "code", "stock_code")
_MEMBER_LIST_KEYS = ("constituents", "stockCodes", "stocks", "list")


def _api_key(api_key: Optional[str]) -> str:
    import os

    key = api_key or os.environ.get("LIXINGER_API_KEY") \
        or os.environ.get("GEOSCOPE_API_KEY")
    if not key:
        raise SymbolError("no GeoScope key: set $LIXINGER_API_KEY (or "
                          "$GEOSCOPE_API_KEY) -- the gfc_live_... key from "
                          "the geoscopeapp.com account page")
    return key


class GeoScopeClient:
    """The real HTTP client (direct connection, paced, one 5xx retry)."""

    def __init__(self, api_key: str, base: str = GEO_BASE, gap: float = _RATE_GAP):
        import requests

        session = requests.Session()
        # NEVER route geoscope through a proxy (see the module docstring)
        session.trust_env = False
        session.proxies = {"http": None, "https": None}
        session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "qf-screen-symbols/1.0",
        })
        self._session = session
        self._base = base.rstrip("/")
        self._gap = float(gap)
        self._last = 0.0
        self.calls = 0

    def post(self, path: str, body: Dict):
        """POST ``{base}/{path}`` -> the inner ``data`` payload.

        The 200 envelope is ``{"data": {"code": 1, "message": ..., "data":
        ...}}``; ``code != 1`` and every HTTP error raise
        :class:`SymbolError` (auth problems say so -- a suspended key must
        not look like a network hiccup).
        """
        last_exc: Optional[Exception] = None
        for attempt in (0, 1):
            if attempt:
                time.sleep(5.0)
            wait = self._gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                resp = self._session.post(f"{self._base}/{path}", json=body,
                                          timeout=120)
            except Exception as exc:            # noqa: BLE001 - requests errors vary
                raise SymbolError(f"geoscope {path}: network error ({exc})") from exc
            if resp.status_code >= 500:
                last_exc = SymbolError(f"geoscope {path}: HTTP "
                                       f"{resp.status_code} (upstream)")
                logger.warning("%s (attempt %d)", last_exc, attempt + 1)
                continue
            return _unwrap(path, resp)
        raise last_exc                          # type: ignore[misc]


def _unwrap(path: str, resp) -> object:
    if resp.status_code == 200:
        outer = resp.json()
        inner = outer.get("data") if isinstance(outer, dict) else None
        if not isinstance(inner, dict):
            raise SymbolError(f"geoscope {path}: response envelope is "
                              f"unexpected: {str(outer)[:200]}")
        if inner.get("code") == 1:
            return inner.get("data")
        message = str(inner.get("message", "")).strip()
        if message.lower() == "no data":
            return None
        raise SymbolError(f"geoscope {path}: upstream code={inner.get('code')} "
                          f"message={message[:150]}")
    if resp.status_code in (401, 403):
        raise SymbolError(f"geoscope {path}: HTTP {resp.status_code} -- key "
                          f"missing/suspended or endpoint not granted "
                          f"({resp.text[:120]})")
    if resp.status_code == 429:
        raise SymbolError(f"geoscope {path}: rate limited (429) -- retry in a "
                          "minute; the account quota is 60 req/min")
    raise SymbolError(f"geoscope {path}: HTTP {resp.status_code}: "
                      f"{resp.text[:150]}")


def _client(api_key: Optional[str], client) -> GeoScopeClient:
    return client if client is not None else GeoScopeClient(_api_key(api_key))


def _pick(row: Dict, keys: Tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def download_lxg_stocks(api_key: Optional[str] = None, market: str = "cn",
                        client=None) -> pd.DataFrame:
    """The full company list (``POST {market}/company``), raw columns as
    returned -- the same rows that make ``cn/lxg/company_list.csv``.

    :meth:`SymbolStore.replace_stocks` normalizes them into the catalog
    schema (A-shares, canonical symbols); no filtering happens here so the
    company-list mirror stays byte-compatible with the reference downloader.
    """
    session = _client(api_key, client)
    rows = session.post(f"{market}/company", {})
    if rows is None:
        raise SymbolError(f"geoscope {market}/company: no data")
    if not isinstance(rows, (list, tuple)) or not rows:
        raise SymbolError(f"geoscope {market}/company: expected a row list, "
                          f"got {type(rows).__name__}")
    frame = pd.DataFrame([row for row in rows if isinstance(row, dict)])
    if frame.empty or "stockCode" not in frame.columns:
        raise SymbolError(f"geoscope {market}/company: rows lack stockCode")
    logger.info("lxg company list: %d rows", len(frame))
    return frame


def download_lxg_sections(source: str = "sw_2021", api_key: Optional[str] = None,
                          include_members: bool = True, client=None,
                          ) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """The 申万 / CNI industry sections and their constituents.

    ``POST cn/industry`` with ``{"source": source}`` lists the sections;
    ``POST cn/industry/constituents/{source}`` maps them to stock codes.
    The constituents endpoint has been returning empty upstream (probed
    2026-11: ``rows=0``) -- that yields an EMPTY members frame and a note,
    never an error, so a section sync still lands the section names.

    Returns ``(sections, members)`` in the store's bulk-replace shape
    (``name, level`` / ``section, symbol, code, name``).
    """
    session = _client(api_key, client)
    rows = session.post("cn/industry", {"source": source})
    if rows is None:
        raise SymbolError(f"geoscope cn/industry ({source}): no data")
    if not isinstance(rows, (list, tuple)):
        raise SymbolError(f"geoscope cn/industry ({source}): expected a row "
                          f"list, got {type(rows).__name__}")
    sections = pd.DataFrame([{
        "name": _pick(row, _NAME_KEYS),
        "level": _pick(row, _LEVEL_KEYS),
    } for row in rows if isinstance(row, dict)])
    sections = sections[sections["name"] != ""].drop_duplicates("name") \
        .reset_index(drop=True)
    if not len(sections):
        raise SymbolError(f"geoscope cn/industry ({source}): no section names "
                          f"in the response (fields: "
                          f"{sorted(rows[0]) if rows else 'none'})")

    members = pd.DataFrame(columns=["section", "symbol", "code", "name"])
    if include_members:
        data = session.post(f"cn/industry/constituents/{source}", {})
        flat: List[Dict] = []
        for row in (data or []) if isinstance(data, (list, tuple)) else []:
            if not isinstance(row, dict):
                continue
            section = _pick(row, _MEMBER_SECTION_KEYS)
            listed = next((row[k] for k in _MEMBER_LIST_KEYS
                           if isinstance(row.get(k), list)), None)
            if listed is not None:             # grouped shape
                flat += [{"section": section, "_code": str(code)}
                         for code in listed]
            else:
                flat.append({"section": section,
                             "_code": _pick(row, _MEMBER_CODE_KEYS)})
        if flat:
            codes = [row["_code"].strip().zfill(6) for row in flat]
            members = pd.DataFrame({
                "section": [row["section"] for row in flat],
                "symbol": [to_canonical(code) for code in codes],
                "code": codes, "name": "",
            })
            members = members[members["symbol"].notna()] \
                .drop_duplicates(["section", "symbol"]).reset_index(drop=True)
            members["symbol"] = members["symbol"].astype(str)
        else:
            logger.warning("geoscope cn/industry/constituents/%s: upstream "
                           "returned no rows -- sections landed without "
                           "members (probed empty 2026-11; retry later)",
                           source)
    logger.info("lxg %s industries: %d sections, %d memberships", source,
                len(sections), len(members))
    return sections, members
