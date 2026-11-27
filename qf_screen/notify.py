"""Webhook delivery for the daily commands — strictly opt-in.

``qf-screen diff … --fail-on-change --webhook-format feishu`` lets the
post-close job push its result without a glue script.  Nothing is ever
sent unless ``--webhook-format`` is on the command line: the URL may come
from ``$QF_SCREEN_WEBHOOK_URL``, but on its own that variable does
nothing (a variable set in a login profile must not turn every ``diff``
into a network call).

Formats
    ``json``      the command's JSON payload, as ``--format json`` prints it
    ``slack``     an incoming webhook: ``{"text": "..."}``
    ``feishu``    a 飞书 custom bot: ``{"msg_type": "text", "content":
                  {"text": "..."}}``; with a secret, signed the way a bot
                  that enforces 签名校验 expects
    ``dingtalk``  a 钉钉 custom robot: ``{"msgtype": "text", "text":
                  {"content": "..."}}``; with a secret, the HMAC signature
                  appended to the URL as its 加签 security setting wants

The secret is ``--webhook-secret`` or ``$QF_SCREEN_WEBHOOK_SECRET``.  A
机器人 without a secret works too where its security setting is a keyword
(put the keyword in the preset's name) or an IP allow-list.

:func:`post` is the ONLY function that opens the network; everything else
is pure, so the payload of every format is unit-tested offline and the
delivery is tested against a recorder.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

__all__ = [
    "FORMATS", "SECRET_ENV", "URL_ENV", "Delivery", "WebhookError", "build_payload",
    "build_request", "dingtalk_sign", "feishu_sign", "message", "post", "send", "webhook_secret",
    "webhook_url",
]

FORMATS = ("json", "feishu", "dingtalk", "slack")
#: environment variables the URL and the signing secret may come from
URL_ENV = "QF_SCREEN_WEBHOOK_URL"
SECRET_ENV = "QF_SCREEN_WEBHOOK_SECRET"
#: seconds to wait for the receiver's answer
TIMEOUT = 10.0
#: names listed per change list in a message (the JSON payload keeps them all)
_LISTED = 10


class WebhookError(RuntimeError):
    """A webhook that could not be delivered."""


@dataclass
class Delivery:
    """The outcome of one push.  ``ok`` is False when the request failed
    or the platform answered with an error of its own."""

    ok: bool
    status: int
    detail: str
    url: str = ""


def webhook_url(explicit=None) -> Optional[str]:
    """``--webhook-url`` when given, else ``$QF_SCREEN_WEBHOOK_URL``."""
    return str(explicit or "").strip() or os.environ.get(URL_ENV, "").strip() or None


def webhook_secret(explicit=None) -> Optional[str]:
    """``--webhook-secret`` when given, else ``$QF_SCREEN_WEBHOOK_SECRET``."""
    return str(explicit or "").strip() or os.environ.get(SECRET_ENV, "").strip() or None


def _dumps(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def _name(row: Dict, rank_first: bool = True) -> str:
    """``#5 SH600519 贵州茅台`` -- what a change list prints per name."""
    parts = []
    if rank_first and row.get("rank") not in (None, "", "nan"):
        parts.append(f"#{row['rank']}")
    parts.append(str(row.get("symbol", "")))
    if str(row.get("name", "") or "").strip() not in ("", "nan"):
        parts.append(str(row["name"]).strip())
    return " ".join(parts)


def _listed(rows, label: str, sign: str) -> str:
    """``entered +3: #5 SH… ; #7 SZ…`` -- the first ``_LISTED`` names, then
    how many more the JSON payload holds."""
    rows = list(rows)
    if not rows:
        return f"{label}: none"
    more = f" … +{len(rows) - _LISTED} more" if len(rows) > _LISTED else ""
    return (f"{label} {sign}{len(rows)}: "
            + "; ".join(_name(row) for row in rows[:_LISTED]) + more)


def message(event: Dict) -> str:
    """The plain-text reading of an event (what every platform's text
    message carries).  ``event`` is the command's JSON payload with an
    ``event`` key — ``diff``, ``watch`` or ``report``."""
    kind = str(event.get("event", ""))
    if kind == "diff":
        movers = [dict(row, change=int(float(row["change"])))
                  for row in event.get("movers") or []]
        up = [f"↑+{row['change']} {_name(row, rank_first=False)}" for row in movers
              if row["change"] > 0][:_LISTED]
        down = [f"↓−{abs(row['change'])} {_name(row, rank_first=False)}"
                for row in movers if row["change"] < 0][:_LISTED]
        share = event["retained"] / event["previous_members"] \
            if event.get("previous_members") else float("nan")
        kept = event.get("retained", 0)
        lines = [f"qf-screen diff: {event['preset']} — {event['asof']} vs {event.get('prev', '')}",
                 _listed(event.get("entered") or [], "entered", "+"),
                 _listed(event.get("exited") or [], "exited", "\u2212"),
                 "retained " + (f"{kept} ({share:.0%})" if share == share else f"{kept}")]
        if event.get("ranked"):
            lines.append(f"rank movers ({len(movers)}): " + ("; ".join(up + down) or "none"))
        return "\n".join(lines)
    if kind == "watch":
        lines = [f"qf-screen watch — {event['asof']} · {event['symbols']} symbols · "
                 f"{event['alerts']} alerts" + (f" (preset {event['preset']})"
                                                if event.get("preset") else "")]
        names = {str(row.get("symbol")): str(row.get("name", "") or "")
                 for row in event.get("rows") or []}
        names = {symbol: name for symbol, name in names.items() if name not in ("", "nan")}
        by_severity: Dict[str, list] = {}
        for alert in event.get("detail") or []:
            by_severity.setdefault(str(alert["severity"]), []).append(alert)
        for severity in ("high", "medium", "low"):
            for alert in by_severity.get(severity, []):
                who = names.get(str(alert["symbol"]), "")
                lines.append(f"{severity.upper()} {alert['symbol']} {who}: {alert['rule']} — "
                             f"{alert['message']}".replace("  ", " "))
        if not event.get("alerts"):
            lines.append("no alerts: " + "; ".join(_name(row, rank_first=False)
                                                   for row in event.get("rows") or []))
        return "\n".join(lines)
    if kind == "report":
        warnings = event.get("warnings") or []
        lines = [f"qf-screen daily briefing — {event['asof']}"]
        for name, entry in (event.get("presets") or {}).items():
            mix = entry.get("industry_mix") or {}
            lead = f" · {next(iter(mix))} {int(next(iter(mix.values())))}" if mix else ""
            lines.append(f"{name}: {len(entry.get('members') or [])} members "
                         f"(+{len(entry.get('entered') or [])} / "
                         f"−{len(entry.get('exited') or [])}){lead}")
        if warnings:
            lines.append(f"{len(warnings)} data warning(s)")
        return "\n".join(lines)
    raise WebhookError(f"no message for an event of kind {kind!r}")


def feishu_sign(timestamp: int, secret: str) -> str:
    """A 飞书 bot signature: HMAC-SHA256 keyed by ``"<timestamp>\\n<secret>"``
    over an empty message, Base64."""
    code = hmac.new(f"{timestamp}\n{secret}".encode("utf-8"), digestmod=hashlib.sha256)
    return base64.b64encode(code.digest()).decode("ascii")


def dingtalk_sign(timestamp: int, secret: str) -> str:
    """A 钉钉 robot signature (milliseconds): HMAC-SHA256 keyed by the
    secret over ``"<timestamp>\\n<secret>"``, Base64, URL-encoded."""
    code = hmac.new(secret.encode("utf-8"), f"{timestamp}\n{secret}".encode("utf-8"),
                    digestmod=hashlib.sha256)
    return urllib.parse.quote_plus(base64.b64encode(code.digest()).decode("ascii"))


def build_payload(fmt: str, event: Dict, secret: Optional[str] = None,
                  now: Optional[float] = None) -> dict:
    """The request body of a format.  A secret signs the 飞书 body."""
    if fmt not in FORMATS:
        raise WebhookError(f"unknown webhook format {fmt!r} (known: {', '.join(FORMATS)})")
    if fmt == "json":
        return dict(event)
    text = message(event)
    if fmt == "slack":
        return {"text": text}
    if fmt == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    body: Dict = {"msg_type": "text", "content": {"text": text}}
    if secret:
        stamp = int(now if now is not None else time.time())
        body.update({"timestamp": str(stamp), "sign": feishu_sign(stamp, secret)})
    return body


def build_request(fmt: str, url: str, event: Dict, secret: Optional[str] = None,
                  now: Optional[float] = None) -> Tuple[str, bytes, Dict[str, str]]:
    """``(address, body, headers)``: the URL (signed for 钉钉), the JSON
    body and the headers to POST."""
    if not str(url).strip():
        raise WebhookError(f"no webhook URL: pass --webhook-url or set ${URL_ENV}")
    if fmt not in FORMATS:
        raise WebhookError(f"unknown webhook format {fmt!r} (known: {', '.join(FORMATS)})")
    address = str(url).strip()
    if fmt == "dingtalk" and secret:
        stamp = int((now if now is not None else time.time()) * 1000)
        address += ("&" if "?" in address else "?") + \
            f"timestamp={stamp}&sign={dingtalk_sign(stamp, secret)}"
    from qf_screen import __version__

    return (address, _dumps(build_payload(fmt, event, secret, now)),
            {"Content-Type": "application/json; charset=utf-8",
             "User-Agent": f"qf-screen/{__version__}"})


def _platform_error(fmt: str, body: str) -> Optional[str]:
    """A 200 that is a refusal: 飞书 answers ``{"code": 19021, ...}``, 钉钉
    ``{"errcode": 310000, ...}`` and Slack ``{"ok": false, ...}``."""
    if not body.lstrip().startswith("{"):
        return None
    try:
        answer = json.loads(body)
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None
    if fmt == "feishu":
        refused = bool(answer.get("code", 0))
    elif fmt == "dingtalk":
        refused = bool(answer.get("errcode", 0))
    else:
        refused = answer.get("ok") is False
    return f"the platform refused it: {body[:200]}" if refused else None


def post(url: str, data: bytes, headers: Dict[str, str],
         timeout: float = TIMEOUT) -> Tuple[int, str]:
    """The one network call: a JSON POST, ``(status, the answer's body)``.
    Raises :class:`WebhookError` when the request does not complete."""
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return int(answer.status), answer.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:                 # answered, and said no
        try:                                             # the body, when it sent one
            body = exc.read(2000).decode("utf-8", "replace")
        except (OSError, ValueError, AttributeError):
            body = ""
        return int(exc.code), body.strip() or str(exc.msg or exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise WebhookError(f"{type(exc).__name__}: {exc}") from exc


def send(fmt: str, event: Dict, url: Optional[str] = None, secret: Optional[str] = None
         ) -> Delivery:
    """Push ``event`` to a webhook in ``fmt``.  Never raises for a failed
    delivery: the result says whether it arrived and what came back."""
    if fmt not in FORMATS:
        raise WebhookError(f"unknown webhook format {fmt!r} (known: {', '.join(FORMATS)})")
    try:
        address, body, headers = build_request(fmt, url or webhook_url() or "", event,
                                               secret if secret is not None else webhook_secret())
    except WebhookError as exc:
        return Delivery(False, 0, str(exc))
    try:
        status, detail = post(address, body, headers)
    except Exception as exc:                 # noqa: BLE001 - the contract is "never raises"
        return Delivery(False, 0, f"{type(exc).__name__}: {exc} -- {address}")
    refused = _platform_error(fmt, detail) if 200 <= status < 300 else \
        f"HTTP {status} {detail[:200]}"
    return Delivery(ok=refused is None, status=status,
                    detail=detail[:200] if refused is None else refused, url=address)
