"""Watchlist & portfolio risk monitor: ``qf-screen watch``.

One decision date (the last complete session, or ``--as-of``), one row per
name you hold, and every risk rule that fires on it::

    qf-screen watch --symbols SH600519,SZ000858 --preset value_dividend
    qf-screen watch --watchlist book.yaml --fail-on-alert

The built-in rules are DSL boolean expressions over the same feature panel
every screen reads, TRUE meaning "alert" — so a rule is exactly as
point-in-time as a screen, and a watchlist file can add its own::

    # a watchlist (YAML; a plain list of symbols, one per line, works too)
    preset: value_dividend           # alert when a holding is no longer a member
    symbols: [SH600519, SZ000858]
    rules:                           # custom rules: DSL boolean, true = alert
      - {name: fallen,   expr: "high252_ratio < 0.60", severity: high}
      - {name: too_hot,  expr: "ret_21d > 0.30"}
    disable: [pledge]                # built-in rules to switch off
    thresholds: {pledge: 0.20}       # ... or re-tune (pledge, liquidity)

A missing value never alerts (``NaN`` fails a comparison, as in a screen):
a rule reports a KNOWN problem.  The two exceptions are the rules that are
about the absence of data itself — ``suspended`` (no close on the as-of
session) and ``unknown_symbol`` (not in the loaded universe at all) —
because a holding nobody can price is the one thing a risk monitor must
not pass over in silence.

Severity orders the output and nothing more: ``high`` (act), ``medium``
(review).  ``--fail-on-alert`` turns any alert into exit code 2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import yaml

from quant_free.common.symbol_format import to_canonical

from qf_screen.data import BOARD_LABELS
from qf_screen.engine import UNCATEGORIZED, ScreenEngine, ScreenSpec, rule_features
from qf_screen.ui.render import fmt_value
from qf_screen.rules import DslError, compile_bool, expr_names, feature_names, is_dynamic_feature

__all__ = [
    "BUILTIN_RULES", "PRESET_EXIT", "SEVERITIES", "THRESHOLDS", "Alert", "Watchlist",
    "WatchlistError", "WatchReport", "WatchRule", "load_watchlist", "parse_watchlist",
    "watch", "watch_markdown", "watch_payload", "watch_text",
]

#: alert severities, most urgent first
SEVERITIES: Tuple[str, ...] = ("high", "medium", "low")
_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

#: rule names of the two alerts the engine raises itself
PRESET_EXIT = "preset_exit"
UNKNOWN_SYMBOL = "unknown_symbol"


class WatchlistError(ValueError):
    """A watchlist (or its rules) that cannot be used."""


@dataclass(frozen=True)
class WatchRule:
    """One risk rule: a DSL boolean expression (true = alert), what it
    means, how urgent it is, and the features its message quotes."""

    name: str
    expr: str
    description: str
    severity: str = "medium"
    context: Tuple[str, ...] = ()
    source: str = "custom"              # "built-in" for :data:`BUILTIN_RULES`


#: what the built-in rules compare against; a watchlist's ``thresholds``
#: override them by name
THRESHOLDS: Dict[str, float] = {"pledge": 0.30, "liquidity": 5e7}

BUILTIN_RULES: Tuple[WatchRule, ...] = (
    WatchRule("st", "st_flag > 0.5",
              "ST/*ST risk-warning name in the company-list snapshot", "high",
              source="built-in"),
    WatchRule("pledge", "pledge_ratio >= {pledge}", "pledge ratio spike", "high",
              ("pledge_ratio",), "built-in"),
    WatchRule("trend_break", "ma_ratio_60 < 1 or ma_ratio_200 < 1", "trend break", "medium",
              ("ma_ratio_60", "ma_ratio_200"), "built-in"),
    WatchRule("liquidity", "amount_ma20 < {liquidity}", "liquidity collapse", "high",
              ("amount_ma20",), "built-in"),
    WatchRule("suspended", "not (close_raw > 0)",
              "no close on the as-of session (suspended, halted or delisted)", "high",
              source="built-in"),
)

_WATCHLIST_KEYS = frozenset({"name", "preset", "symbols", "rules", "disable", "thresholds"})
_RULE_KEYS = frozenset({"name", "expr", "severity", "description"})


# --------------------------------------------------------------- watchlist
@dataclass
class Watchlist:
    """What to watch and how: symbols, an optional target preset, custom
    rules, built-in rules switched off and re-tuned thresholds."""

    symbols: List[str]
    preset: Optional[str] = None
    rules: Tuple[WatchRule, ...] = ()
    disable: Tuple[str, ...] = ()
    thresholds: Dict[str, float] = field(default_factory=dict)
    name: str = ""
    source: str = ""


#: what separates symbols in free text, the brackets / quotes stripped
#: around each, and the ``- `` of a YAML-style list item
_SEPARATORS = re.compile(r"[,;\s]+")
_WRAPPERS = "[]\"'"
_LIST_ITEM = re.compile(r"^\s*-\s+")


def _plain_symbols(text: str) -> List[str]:
    """Symbols out of free text: one or more per line, separated by
    commas / whitespace, ``#`` comments dropped, ``- `` list markers
    removed, in first-seen order."""
    found: List[str] = []
    for line in text.splitlines():
        line = _LIST_ITEM.sub("", line.split("#", 1)[0])
        for part in _SEPARATORS.split(line):
            part = part.strip(_WRAPPERS)
            if part and part not in found:
                found.append(part)
    return found


def _symbols(value) -> List[str]:
    """A watchlist's ``symbols``: a list of strings, or one comma/space
    separated string -- canonical spellings where the code is known."""
    items = value if isinstance(value, (list, tuple)) else [value]
    out: List[str] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        out += _plain_symbols(text)
    return out


def parse_watchlist(text: str, source: str = "") -> Watchlist:
    """A watchlist from its text: a YAML mapping (the documented keys), a
    bare YAML list of symbols, or plain text with symbols on the lines."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        doc = None
    if doc is None or isinstance(doc, (list, str, int, float)):
        symbols = _plain_symbols(text)           # read as text: keeps 000858's zeros
        if not symbols:
            raise WatchlistError(f"{source or 'watchlist'}: no symbols in it")
        return Watchlist(symbols=symbols, source=source)
    if not isinstance(doc, dict):
        raise WatchlistError(f"{source or 'watchlist'}: must be a mapping, a list of "
                             f"symbols or plain text, got {type(doc).__name__}")
    where = source or "watchlist"
    unknown = sorted(str(key) for key in doc if key not in _WATCHLIST_KEYS)
    if unknown:
        raise WatchlistError(f"{where}: unknown key(s) {unknown} (known: "
                             f"{', '.join(sorted(_WATCHLIST_KEYS))})")
    if "symbols" not in doc or doc["symbols"] is None:
        raise WatchlistError(f"{where}: no 'symbols'")
    symbols = _symbols(doc["symbols"])
    if not symbols:
        raise WatchlistError(f"{where}: no symbols in 'symbols'")
    built_in = {rule.name for rule in BUILTIN_RULES}
    rules: List[WatchRule] = []
    for i, entry in enumerate(doc.get("rules") or []):
        if not isinstance(entry, dict) or not isinstance(entry.get("expr"), str) \
                or not isinstance(entry.get("name"), str):
            raise WatchlistError(f"{where}: rules[{i}] must be a mapping with a 'name' "
                                 "and an 'expr' string")
        unknown = sorted(str(key) for key in entry if key not in _RULE_KEYS)
        if unknown:
            raise WatchlistError(f"{where}: unknown key(s) {unknown} in rules[{i}] "
                                 f"(known: {', '.join(sorted(_RULE_KEYS))})")
        name = entry["name"].strip()
        if not name or name in built_in or any(rule.name == name for rule in rules):
            raise WatchlistError(f"{where}: rules[{i}] name {name!r} is empty, a built-in "
                                 "rule or used twice")
        severity = str(entry.get("severity") or "medium").lower()
        if severity not in SEVERITIES:
            raise WatchlistError(f"{where}: rules[{i}] severity must be one of "
                                 f"{', '.join(SEVERITIES)}, got {entry.get('severity')!r}")
        rules.append(WatchRule(name, entry["expr"].strip(),
                               str(entry.get("description") or "custom rule"), severity,
                               source="custom"))
    disabled = [str(name) for name in (doc.get("disable") or [])]
    if isinstance(doc.get("disable"), str):
        disabled = _plain_symbols(doc["disable"])
    unknown = [name for name in disabled if name not in built_in]
    if unknown:
        raise WatchlistError(f"{where}: cannot disable {unknown} -- not built-in rules "
                             f"({', '.join(sorted(built_in))})")
    try:
        # YAML 1.1 reads ``1e8`` as a string (a float needs its dot), so a
        # numeric string is taken too
        thresholds = _checked_thresholds(doc.get("thresholds"))
    except WatchlistError as exc:
        raise WatchlistError(f"{where}: {exc}") from exc
    preset = doc.get("preset")
    return Watchlist(symbols=symbols, preset=str(preset) if preset else None,
                     rules=tuple(rules), disable=tuple(dict.fromkeys(disabled)),
                     thresholds=thresholds, name=str(doc.get("name") or ""),
                     source=source)


def load_watchlist(path) -> Watchlist:
    """:func:`parse_watchlist` over a file (YAML or TXT)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WatchlistError(f"cannot read the watchlist {path}: {exc.strerror}") from exc
    return parse_watchlist(text, source=str(path))


# ----------------------------------------------------------------- report
@dataclass
class Alert:
    """One rule firing for one name."""

    symbol: str
    rule: str
    severity: str
    message: str
    expr: str = ""


@dataclass
class WatchReport:
    """The monitor's verdict on one decision date.

    ``rows`` holds one row per watched name — ``symbol, name, board,
    industry, close_raw, ret_21d, alerts, severity, detail`` — with the
    most urgent names first and the quiet ones last; ``alerts`` one row
    per alert; ``rules`` the rules that were in force.
    """

    asof: pd.Timestamp
    preset: Optional[str]
    rows: pd.DataFrame
    alerts: pd.DataFrame
    rules: List[Dict[str, str]]
    warnings: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.alerts)

    def by_severity(self) -> Dict[str, int]:
        counts = dict.fromkeys(SEVERITIES, 0)
        for severity in self.alerts.get("severity", pd.Series(dtype=str)):
            counts[str(severity)] += 1
        return counts


def _finite(value) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _value(env: Dict[str, np.ndarray], feature: str, at: int) -> float:
    """One name's value of a feature in the rule's environment."""
    values = env.get(feature)
    try:
        value = float(values[at]) if values is not None else np.nan
    except (TypeError, ValueError, IndexError):
        return np.nan
    return value if np.isfinite(value) else np.nan


def _detail(rule: WatchRule, env: Dict[str, np.ndarray], at: int,
            thresholds: Dict[str, float]) -> str:
    """A rule's message for one name: what it means, with the values it
    read in their display units."""
    if rule.name == "trend_break":
        broken = [feature for feature in rule.context if _value(env, feature, at) < 1]
        if broken:
            windows = " or ".join(f"{feature.rsplit('_', 1)[-1]}-day" for feature in broken)
            quoted = ", ".join(f"{feature} {fmt_value(feature, _value(env, feature, at))}"
                               for feature in broken)
            return f"close below its {windows} mean ({quoted})"
        return rule.description
    if rule.name == "pledge":
        return (f"pledge ratio {fmt_value('pledge_ratio', _value(env, 'pledge_ratio', at))} "
                f"at or above {thresholds['pledge']:.0%}")
    if rule.name == "liquidity":
        return (f"20d mean traded value "
                f"{fmt_value('amount_ma20', _value(env, 'amount_ma20', at))}, under the "
                f"{fmt_value('amount_ma20', thresholds['liquidity'])} floor")
    quoted = " ".join(f"{feature} {fmt_value(feature, value)}"
                      for feature in rule.context
                      if _finite(value := _value(env, feature, at)))
    return rule.description + (f" ({quoted})" if quoted else "")


def _checked_thresholds(overrides) -> Dict[str, float]:
    """A watchlist's threshold overrides, validated: only keys a built-in
    rule takes, each a finite number (YAML 1.1 reads ``1e8`` as a string --
    a float needs its dot -- so a numeric string is taken too)."""
    checked: Dict[str, float] = {}
    for key, value in (overrides or {}).items():
        if key not in THRESHOLDS:
            raise WatchlistError(f"no built-in rule takes a threshold {key!r} "
                                 f"({', '.join(sorted(THRESHOLDS))})")
        try:
            number = float(value)
            if isinstance(value, bool) or not np.isfinite(number):
                raise ValueError
        except (TypeError, ValueError):
            raise WatchlistError(f"thresholds.{key} must be a number, got {value!r}") from None
        checked[str(key)] = number
    return checked


def _compiled_rules(panel, pos: int, rules: Sequence[WatchRule], disable: Sequence[str],
                    thresholds: Optional[Dict[str, float]]
                    ) -> Tuple[List[Tuple[WatchRule, object, Dict[str, np.ndarray]]],
                               Dict[str, float]]:
    """``(rules in force, thresholds)``: the built-ins minus the disabled
    ones plus the custom ones, each with its thresholds filled in and its
    expression compiled over the panel at row ``pos``."""
    limits = {**THRESHOLDS, **_checked_thresholds(thresholds)}
    built_in = {rule.name for rule in BUILTIN_RULES}
    unknown = [str(name) for name in disable if str(name) not in built_in]
    if unknown:
        raise WatchlistError(f"cannot disable {unknown} -- not built-in rules "
                             f"({', '.join(sorted(built_in))})")
    active = [replace(rule, expr=rule.expr.format(**limits))
              for rule in BUILTIN_RULES if rule.name not in set(map(str, disable))]
    compiled = []
    for rule in active + list(rules):
        if rule.source != "built-in" and not rule.context:
            rule = replace(rule, context=tuple(expr_names(rule.expr)))  # quotes what it reads
        if rule.severity not in SEVERITIES:
            raise WatchlistError(f"rule {rule.name!r}: severity must be one of "
                                 f"{', '.join(SEVERITIES)}, got {rule.severity!r}")
        # checked HERE, with the rule's name in the message: an unknown
        # feature would otherwise surface as a KeyError from deep inside the
        # panel, a non-boolean expression as a bare DslError
        unknown = [feature for feature in expr_names(rule.expr)
                   if feature not in feature_names() and not is_dynamic_feature(feature)]
        if unknown:
            raise WatchlistError(f"rule {rule.name!r} references unknown feature(s) {unknown}; "
                                 "`qf-screen features` lists them")
        try:
            compiled.append((rule, compile_bool(rule.expr),
                             panel.env(pos, expr_names(rule.expr))))
        except DslError as exc:
            raise WatchlistError(f"rule {rule.name!r}: {exc}") from exc
    return compiled, limits


def _canonical(symbols: Sequence[str]) -> List[str]:
    """The watchlist's symbols, canonical where the code is known (``600519``
    -> ``SH600519``), de-duplicated in first-seen order."""
    wanted: List[str] = []
    for symbol in symbols:
        text = str(symbol).strip()
        if text and (to_canonical(text) or text) not in wanted:
            wanted.append(to_canonical(text) or text)
    return wanted


def watch(engine: ScreenEngine, symbols: Sequence[str] = (), preset: Optional[str] = None,
          rules: Sequence[WatchRule] = (), disable: Sequence[str] = (),
          thresholds: Optional[Dict[str, float]] = None, asof=None) -> WatchReport:
    """Run every rule in force over ``symbols`` on the decision date.

    ``preset`` (a name, a YAML path or a :class:`ScreenSpec`) adds the
    preset-exit alert: a name that is not a member of the preset's
    snapshot that day — with the rules it fails, or its rank among the
    passers when the truncation alone kept it out.  ``rules`` are custom
    rules, ``disable`` names built-in rules to switch off and
    ``thresholds`` re-tune :data:`THRESHOLDS`.
    """
    store, panel = engine.store, engine.panel
    date = store.decision_date if asof is None else pd.Timestamp(asof).normalize()
    pos = panel.position_of(date)
    date = panel.calendar[pos]
    compiled, limits = _compiled_rules(panel, pos, rules, disable, thresholds)
    wanted = _canonical(symbols)
    if not wanted:
        raise WatchlistError("no symbols to watch")

    spec = None
    if isinstance(preset, ScreenSpec):
        spec = preset
    elif preset:
        spec = ScreenSpec.from_yaml(preset)
    result = engine.run(spec, asof=date) if spec is not None else None
    members = set(result.members) if result is not None else set()
    # a name that clears every rule and still misses the list is shown its
    # rank among the WHOLE passing pool -- computed once, only when needed
    pool = None
    if result is not None:
        for symbol in wanted:
            if symbol in panel.symbols and symbol not in members \
                    and not _fails(result, symbol)[0]:
                pool = engine.run(replace(spec, top_n=None, max_per_industry=None), asof=date)
                break

    universe = store.universe
    closes = panel.cross_section("close_raw", pos)
    returns = panel.cross_section("ret_21d", pos)
    records: List[Dict[str, object]] = []
    alerts: List[Alert] = []
    for order, symbol in enumerate(wanted):
        found: List[Alert] = []
        name = board = industry = ""
        close = change = np.nan
        if symbol not in panel.symbols:
            found.append(Alert(symbol, UNKNOWN_SYMBOL, "high",
                               "not in the loaded universe (check the spelling: "
                               "SH600519, SZ000001, BJ920001)"))
        else:
            at = panel.symbols.get_loc(symbol)
            for rule, test, env in compiled:
                if test(env)[at]:
                    found.append(Alert(symbol, rule.name, rule.severity,
                                       _detail(rule, env, at, limits), rule.expr))
            if result is not None and symbol not in members:
                found.append(_exit_alert(spec, result, symbol, pool))
            board = str(universe.get(symbol, "board", "") or "")
            name = str(universe.get(symbol, "name", "") or "")
            industry = str(universe.get(symbol, "industry_l1", "") or "") \
                or UNCATEGORIZED
            close, change = float(closes[at]), float(returns[at])
        worst = min((alert.severity for alert in found), key=_RANK.get, default="-")
        records.append({
            "symbol": symbol, "name": name, "board": BOARD_LABELS.get(board, board),
            "industry": industry, "close_raw": close, "ret_21d": change,
            "alerts": len(found), "severity": worst, "_order": order,
            "detail": "; ".join(alert.message for alert in found)})
        # the name's own alerts, most urgent first
        alerts += sorted(found, key=lambda alert: _RANK[alert.severity])
    # most urgent names first, the quiet ones last; each list in the order shown
    records.sort(key=lambda row: (_RANK.get(str(row["severity"]), len(SEVERITIES)),
                                  -int(row["alerts"]), str(row["symbol"])))
    shown = {row["symbol"]: row["_order"] for row in records}
    alerts.sort(key=lambda alert: (shown[alert.symbol], _RANK[alert.severity]))
    rows = pd.DataFrame(records).drop(columns="_order")
    fired = pd.DataFrame([alert.__dict__ for alert in alerts],
                         columns=["symbol", "rule", "severity", "message", "expr"])
    in_force = [{"name": rule.name, "expr": rule.expr, "severity": rule.severity,
                 "description": rule.description, "source": rule.source}
                for rule, _test, _env in compiled]
    if spec is not None:
        in_force.append({"name": PRESET_EXIT, "expr": "", "severity": "medium",
                         "description": f"no longer a member of {spec.name}",
                         "source": "preset"})
    return WatchReport(asof=date, preset=spec.name if spec is not None else None, rows=rows,
                       alerts=fired, rules=in_force,
                       warnings=list(store.warnings) + list(panel.warnings))


def _fails(result, symbol: str) -> Tuple[List[str], Dict]:
    """``(names of the rules the name fails, the run's explanation)``."""
    info = result.explain(symbol)
    return [name for name, passed in info["rules"].items() if not passed], info


def _exit_alert(spec: ScreenSpec, result, symbol: str, pool) -> Alert:
    """Why a name is out of the preset's list: the rules it fails (with
    what they read), or where it ranks among the passers."""
    failed, info = _fails(result, symbol)
    if failed:
        parts = []
        for name in failed:
            expression = info["rule_exprs"].get(name, "")
            values = " ".join(
                f"{feature} {fmt_value(feature, info['features'][feature])}"
                for feature in rule_features(expression)
                if feature in info["features"] and _finite(info["features"][feature]))
            parts.append(f"{name} ({values})" if values else name)
        return Alert(symbol, PRESET_EXIT, "medium",
                     f"out of {spec.name}: fails {', '.join(parts)}")
    if pool is None or symbol not in pool.members:
        return Alert(symbol, PRESET_EXIT, "medium", f"out of {spec.name}")
    rank = int(pool.members.get_loc(symbol)) + 1
    capped = symbol in set(result.capped)
    return Alert(symbol, PRESET_EXIT, "medium",
                 f"out of {spec.name}: passes every rule"
                 + (f", displaced by the max_per_industry {spec.max_per_industry} cap"
                    if capped else "")
                 + f", ranks {rank} of {len(pool.members)} passers"
                 + (f" (top {spec.top_n} kept)" if spec.top_n else ""))


# ------------------------------------------------------------------ views
def _summary(report: WatchReport) -> str:
    counts = report.by_severity()
    tally = ", ".join(f"{counts[severity]} {severity}" for severity in SEVERITIES
                      if counts[severity])
    return (f"{len(report.rows)} symbols as of {report.asof.date()}"
            + (f" (preset: {report.preset})" if report.preset else "")
            + (f" -- {report.count} alerts: {tally}" if tally else " -- no alerts"))


def _line(row) -> str:
    """One name's header line: symbol, name, board · industry, close, 21-day return."""
    parts = [str(row.symbol), row.name or "-"]
    where = " · ".join(part for part in (row.board, row.industry) if part)
    if where:
        parts.append(where)
    if _finite(row.close_raw):
        parts.append(f"close {fmt_value('close_raw', row.close_raw)}")
    if _finite(row.ret_21d):
        parts.append(f"21d {fmt_value('ret_21d', row.ret_21d)}")
    return "  ".join(parts)


def watch_text(report: WatchReport) -> str:
    lines = ["watch: " + _summary(report), "rules in force: "
             + ", ".join(rule["name"] for rule in report.rules), ""]
    for row in report.rows.itertuples(index=False):
        lines.append(_line(row))
        fired = _fired(report, row.symbol)
        if len(fired):
            lines += [f"    {alert.severity.upper():7s} {alert.rule:14s} {alert.message}"
                      for alert in fired.itertuples(index=False)]
        else:
            lines.append("    ok")
    return "\n".join(lines)


def _fired(report: WatchReport, symbol: str):
    """The alerts of one name, most urgent first."""
    fired = report.alerts[report.alerts["symbol"] == symbol]
    return fired.sort_values("severity", key=lambda column: column.map(_RANK),
                             kind="stable") if len(fired) else fired


def watch_markdown(report: WatchReport) -> str:
    counts = report.by_severity()
    tally = ", ".join(f"**{counts[severity]} {severity}**" for severity in SEVERITIES
                      if counts[severity])
    lines = [f"## watch — {report.asof.date()} · {len(report.rows)} symbols"
             + (f" · preset `{report.preset}`" if report.preset else "")
             + (f" · {report.count} alerts ({tally})" if tally else " · no alerts"), "",
             "| symbol | name | industry | close | 21d | alerts | detail |",
             "|---|---|---|---|---|---|---|"]
    for row in report.rows.itertuples(index=False):
        fired = _fired(report, row.symbol)
        rules = ", ".join(f"**{alert.severity}** {alert.rule}" for alert in fired.itertuples())
        detail = "; ".join(str(alert.message) for alert in fired.itertuples())
        lines.append(
            f"| `{row.symbol}` | {row.name or '-'} | {row.industry} | "
            f"{fmt_value('close_raw', row.close_raw)} | {fmt_value('ret_21d', row.ret_21d)} | "
            f"{rules or '-'} | {detail or '-'} |")
    lines += ["", "_rules in force: " + ", ".join(rule["name"] for rule in report.rules)
              + "_", ""]
    return "\n".join(lines)


def watch_payload(report: WatchReport) -> Dict[str, object]:
    return {
        "asof": str(report.asof.date()), "preset": report.preset,
        "symbols": len(report.rows), "alerts": report.count,
        "by_severity": report.by_severity(),
        "rules": report.rules,
        "rows": report.rows.astype(str).to_dict(orient="records"),
        "detail": report.alerts.to_dict(orient="records"),
        "warnings": list(report.warnings),
    }
