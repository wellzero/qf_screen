"""The daily-trader views: text, JSON and Markdown renderings of a run, a
diff, a stock card, a preset comparison and the daily briefing.

Rendering only — every number comes from :mod:`qf_screen.engine`; nothing
here reads data or decides anything.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from qf_screen.engine import ScreenDiff, ScreenResult, ScreenSpec, StockCard
from qf_screen.render import fmt_value, frame_text, industry_line, markdown_table, num, pct

#: marker the funnel puts on a rule that passes names for lack of data
NA_MARK = " †"
_PICK_FEATURES = 4                                  # extra feature columns in a top-picks table


# ------------------------------------------------------------------ run
def funnel_view(result: ScreenResult) -> pd.DataFrame:
    """The funnel as displayed: cumulative survival shares, the number of
    names each rule could not judge (only when any rule leaves some) and a
    ``†`` on the rules that let those names through."""
    funnel = result.funnel
    if funnel.empty:
        return funnel
    base = float(funnel["passing"].iloc[0]) if len(funnel) else 0.0
    shows_unknown = "unknown" in funnel.columns and int(funnel["unknown"].sum()) > 0
    rules = funnel["rule"].astype(str).tolist()
    if shows_unknown and "na_pass" in funnel.columns:
        rules = [rule + NA_MARK if flag else rule
                 for rule, flag in zip(rules, funnel["na_pass"])]
    view = pd.DataFrame({"rule": rules, "passing": funnel["passing"].to_numpy()})
    if shows_unknown:
        view["unknown"] = funnel["unknown"].to_numpy()
    view["cumulative"] = funnel["cumulative"].to_numpy()
    view["members"] = funnel["members"].to_numpy()
    view["survival"] = [pct(value, 0) for value in (
        funnel["cumulative"] / base if base else funnel["cumulative"] * np.nan)]
    return view


def funnel_legend(result: ScreenResult) -> str:
    """The line explaining the ``unknown`` column, when there is one."""
    funnel = result.funnel
    if "unknown" not in funnel.columns or int(funnel["unknown"].sum()) == 0:
        return ""
    return ("unknown = names where every feature the rule mentions is missing (no "
            "source on disk, or a value the feature cannot take); they FAIL a strict "
            "rule and PASS one marked † (na_pass)")


def run_text(result: ScreenResult, frame: pd.DataFrame, members: bool = True) -> str:
    """The snapshot as terminal text: funnel, industry mix, notes, members
    (``members=False`` leaves the member table to the caller — the CSV view
    prints it raw)."""
    lines = ["", "funnel:"]
    with pd.option_context("display.width", 200, "display.max_columns", 40):
        lines.append(funnel_view(result).to_string(index=False))
    legend = funnel_legend(result)
    if legend:
        lines.append(f"({legend})")
    lines += ["", f"industry mix: {industry_line(result.industry_mix())}"]
    notes = result.notes()
    if notes:
        lines += ["notes:"] + [f"  - {note}" for note in notes]
    if members:
        lines += ["", f"members ({len(frame)}):", frame_text(frame)]
    return "\n".join(lines)


def run_markdown(result: ScreenResult, frame: pd.DataFrame, full: bool = True) -> str:
    """The snapshot as a Markdown document."""
    spec = result.spec
    lines = [f"## `{spec.name}` — {len(frame)} members as of {result.asof.date()}", ""]
    description = (spec.description or "").strip()
    if description and full:
        lines += [" ".join(description.splitlines()), ""]
    lines += ["### funnel", "", markdown_table(funnel_view(result))]
    legend = funnel_legend(result)
    if legend:
        lines += ["", f"_{legend}_"]
    lines += ["", f"**industry mix:** {industry_line(result.industry_mix())}", ""]
    notes = result.notes()
    if notes:
        lines += ["**notes:**"] + [f"- {note}" for note in notes] + [""]
    lines += [f"### members ({len(frame)})", "", markdown_table(frame)]
    if result.warnings:
        lines += ["", "### warnings", ""] + [f"- {warning}" for warning in result.warnings]
    return "\n".join(lines) + "\n"


def run_payload(result: ScreenResult, frame: pd.DataFrame) -> Dict[str, object]:
    """The snapshot as a JSON-ready dict."""
    mix = result.industry_mix()
    return {
        "preset": result.spec.name, "asof": str(result.asof.date()),
        "members": frame.astype(str).to_dict(orient="records"),
        "funnel": result.funnel.astype(str).to_dict(orient="records"),
        "industry_mix": {str(k): int(v) for k, v in mix.items()},
        "capped": [str(s) for s in result.capped],
        "notes": result.notes(), "warnings": list(result.warnings),
    }


def explain_text(info: Dict) -> str:
    """``engine.run(...).explain(symbol)`` as terminal text."""
    lines = [f"explain {info['symbol']}:" + (f"  {info['name']}" if info.get("name") else "")]
    unknown = set(info.get("unknown") or [])
    for rule, outcome in info.get("rules", {}).items():
        expression = info.get("rule_exprs", {}).get(rule, "")
        tail = "  (no data: passed by na_pass)" if rule in unknown and outcome else ""
        lines.append(f"    {'PASS' if outcome else 'FAIL'}  {rule:16s} {expression}{tail}")
    for key, value in sorted(info.get("features", {}).items()):
        if isinstance(value, float):
            lines.append(f"    {key:22s} {fmt_value(key, value)}")
        elif isinstance(value, str) and value:
            lines.append(f"    {key:22s} {value}")
    if "score" in info:
        lines.append(f"    score {info['score']:,.4f} "
                     f"(percentile {info['score_percentile']:.0%})")
    return "\n".join(lines)


# ------------------------------------------------------------------ diff
def _ranked_list(frame: pd.DataFrame, limit: Optional[int]) -> List[str]:
    shown = frame if limit is None else frame.head(limit)
    columns = [c for c in ("rank", "symbol", "name", "industry_l1") if c in shown.columns]
    out = []
    for values in shown[columns].itertuples(index=False, name=None):
        parts = dict(zip(columns, values))
        rank = f"#{parts.pop('rank')} " if "rank" in parts else ""
        industry = parts.get("industry_l1")
        tag = f" ({industry})" if isinstance(industry, str) and industry.strip() else ""
        out.append(f"{rank}{parts['symbol']} {parts.get('name', '')}{tag}".rstrip())
    return out


def diff_text(diff: ScreenDiff, limit: Optional[int] = 10) -> str:
    lines = [diff.summary(), ""]
    for title, frame in (("entered", diff.entered), ("exited", diff.exited)):
        sign = "+" if title == "entered" else "-"
        lines.append(f"{title} ({sign}{len(frame)}):")
        lines.append(frame_text(frame, limit) if len(frame) else "  none")
        lines.append("")
    movers = diff.movers
    if diff.ranked:
        lines.append(f"rank movers ({len(movers)}):")
        if len(movers):
            view = pd.DataFrame({
                "symbol": movers["symbol"], "name": movers["name"],
                "rank": [f"{a} → {b}" for a, b in zip(movers["rank_prev"], movers["rank_now"])],
                "change": [f"+{int(v)}" if v > 0 else f"{int(v)}" for v in movers["change"]],
                "score_prev": movers["score_prev"], "score_now": movers["score_now"]})
            lines.append(frame_text(view, limit))
        else:
            lines.append("  none")
        steady = len(diff.current.members) - len(diff.entered) - len(movers)
        lines += ["", f"unchanged in rank: {steady}"]
    else:
        lines.append("no ranking in this preset: rank movers are not shown "
                     "(its order is the symbol order)")
    return "\n".join(lines)


def diff_markdown(diff: ScreenDiff, limit: Optional[int] = 10) -> str:
    lines = [f"## `{diff.spec.name}` — changes as of {diff.asof.date()}", "",
             diff.summary().split(" -- ", 1)[-1], ""]
    for title, frame, sign in (("entered", diff.entered, "+"),
                               ("exited", diff.exited, "−")):
        names = ", ".join(_ranked_list(frame, limit)) if len(frame) else "none"
        lines.append(f"**{title} ({sign}{len(frame)})**: {names}")
    lines.append("")
    movers = diff.movers
    if not diff.ranked:
        lines += ["_no ranking in this preset: rank movers are not shown_", ""]
    elif len(movers):
        view = movers.copy()
        view["rank"] = [f"{a} → {b}" for a, b in zip(view["rank_prev"], view["rank_now"])]
        view["change"] = [f"+{int(v)}" if v > 0 else f"{int(v)}" for v in view["change"]]
        shown = view[["symbol", "name", "rank", "change"]]
        lines += [f"**rank movers** ({len(movers)}, biggest gains first):", "",
                  markdown_table(shown if limit is None else shown.head(limit)), ""]
    else:
        lines += ["**rank movers**: none", ""]
    return "\n".join(lines).rstrip() + "\n"


def diff_payload(diff: ScreenDiff) -> Dict[str, object]:
    def _rows(frame: pd.DataFrame) -> List[Dict[str, str]]:
        return frame.astype(str).to_dict(orient="records")

    movers = diff.movers
    return {
        "preset": diff.spec.name, "asof": str(diff.asof.date()),
        "prev": str(diff.prev.date()), "ranked": diff.ranked,
        "members": len(diff.current.members), "previous_members": len(diff.previous.members),
        "retained": diff.retained,
        "entered": _rows(diff.entered), "exited": _rows(diff.exited),
        "movers": movers.astype(str).to_dict(orient="records"),
        "warnings": sorted(set(diff.previous.warnings) | set(diff.current.warnings)),
    }


# --------------------------------------------------------------- inspect
def _rule_line(failure: Dict) -> str:
    values = "  ".join(f"{feature} {fmt_value(feature, value)}"
                       for feature, value in failure["features"].items())
    return f"{failure['rule']}: {failure['expr']}" + (f"   ({values})" if values else "")


def inspect_text(card: StockCard) -> str:
    info = card.info
    lines = [f"{info['symbol']} {info['name']} -- {info['board']} · {info['industry']} "
             f"-- as of {card.asof.date()}", ""]
    passed = card.passed
    lines.append(f"passes {len(passed)} of {len(card.presets)} presets"
                 + (f": {', '.join(passed)}" if passed else ""))
    for entry in card.presets:
        if entry["member"]:
            lines.append(f"  PASS  {entry['preset']:20s} rank {entry['rank']}/"
                         f"{entry['members']}  score {pct(entry['score'])}")
        elif entry.get("capped_out"):
            lines.append(f"  RANK  {entry['preset']:20s} passes every rule -- "
                         f"{entry['pool_rank']} of {entry['pool']} passers, displaced "
                         "by the max_per_industry cap")
        elif entry["passes_rules"]:
            lines.append(f"  RANK  {entry['preset']:20s} passes every rule -- "
                         f"{entry['pool_rank']} of {entry['pool']} passers, only the "
                         f"top {entry['members']} are kept")
        else:
            lines.append(f"  FAIL  {entry['preset']:20s}")
            lines += [f"        {_rule_line(failure)}" for failure in entry["failed"]]
    lines += ["", "feature scorecard (pct = share of the loaded names with a lower "
              "value; covered = names that have a value):"]
    lines.append(frame_text(_stats_view(card)))
    return "\n".join(lines)


def inspect_markdown(card: StockCard) -> str:
    info = card.info
    passed = card.passed
    lines = [f"# `{info['symbol']}` {info['name']} — {info['board']} · {info['industry']} "
             f"— as of {card.asof.date()}", "",
             f"passes **{len(passed)} of {len(card.presets)}** presets"
             + (f": {', '.join(passed)}" if passed else ""), "",
             "| preset | verdict | rank | score | rules it failed |", "|---|---|---|---|---|"]
    for entry in card.presets:
        if entry["member"]:
            lines.append(f"| `{entry['preset']}` | PASS | {entry['rank']}/{entry['members']} "
                         f"| {pct(entry['score'])} | |")
        elif entry["passes_rules"]:
            lines.append(f"| `{entry['preset']}` | ranked out"
                         + (" (industry cap)" if entry.get("capped_out") else "")
                         + f" | {entry['pool_rank']}/"
                         f"{entry['pool']} passers | - | passes every rule; only the "
                         f"top {entry['members']} are kept |")
        else:
            failed = "<br>".join(_rule_line(failure).replace("|", "\\|")
                                 for failure in entry["failed"])
            lines.append(f"| `{entry['preset']}` | FAIL | - | - | {failed} |")
    lines += ["", "## feature scorecard", "",
              "_pct = share of the loaded names with a lower value; covered = names "
              "that have a value_", "", markdown_table(_stats_view(card))]
    return "\n".join(lines) + "\n"


def _stats_view(card: StockCard) -> pd.DataFrame:
    view = card.stats.copy()
    if view.empty:
        return view
    view["value"] = [fmt_value(f, v) for f, v in zip(view["feature"], view["value"])]
    view["pct_rank"] = ["-" if not np.isfinite(v) else f"{v:.0%}" for v in view["pct_rank"]]
    return view


def inspect_payload(card: StockCard) -> Dict[str, object]:
    stats = card.stats.copy()
    if len(stats):
        stats["value"] = stats["value"].astype(float)
        stats["pct_rank"] = stats["pct_rank"].astype(float)
    return {
        "symbol": card.info["symbol"], "name": card.info["name"],
        "board": card.info["board"], "industry": card.info["industry"],
        "asof": str(card.asof.date()), "passed": card.passed,
        "presets": [{**entry, "failed": [dict(f) for f in entry["failed"]]}
                    for entry in card.presets],
        "stats": stats.to_dict(orient="records"),
    }


# --------------------------------------------------------------- compare
_PERCENT_METRICS = ("ann_return", "ann_vol", "max_drawdown", "cum_return", "turnover",
                    "excess_ann")


def compare_cells(table: pd.DataFrame) -> pd.DataFrame:
    """A comparison table (metrics x presets) with formatted cells."""
    rows = []
    for metric, values in table.iterrows():
        cells: Dict[str, object] = {"metric": metric}
        for preset, value in values.items():
            try:
                missing = value is None or not np.isfinite(float(value))
            except (TypeError, ValueError):
                missing = True
            if missing:
                cells[preset] = "-"
            elif metric in _PERCENT_METRICS or metric.startswith("spread_"):
                cells[preset] = pct(float(value))
            elif metric.startswith("hit_"):
                cells[preset] = pct(float(value), 0)
            else:
                cells[preset] = num(float(value), 1 if metric in ("members", "rebalances")
                                    else 2)
        rows.append(cells)
    return pd.DataFrame(rows)


def compare_text(table: pd.DataFrame, notes: Sequence[str] = ()) -> str:
    lines = [compare_cells(table).to_string(index=False)]
    if len(table):
        lines.append(
            "\nann_return / cum_return / max_drawdown / turnover: the equal-weight "
            "portfolio that follows each schedule, rebalanced on its dates;\n"
            "excess_ann / spread_Nd: its edge over the preset's OWN candidate pool; "
            "hit_Nd: share of rebalances with a positive edge;\n"
            "universe: every name with a price the session before, equal-weighted")
    notes = list(notes)
    if notes:
        lines += ["notes:"] + [f"  - {note}" for note in notes]
    return "\n".join(lines)


def compare_markdown(table: pd.DataFrame, notes: Sequence[str] = ()) -> str:
    lines = ["## preset comparison", "", markdown_table(compare_cells(table))]
    if notes:
        lines += ["", "notes:", ""] + [f"- {note}" for note in notes]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- report
def _top_picks(result: ScreenResult, top: int) -> pd.DataFrame:
    frame = result.to_frame().head(top)
    if frame.empty:
        return frame
    keep = ["symbol", "name", "industry_l1"]
    picks = keep + ["score"] + [c for c in frame.columns
                                if c not in keep + ["score"]][:_PICK_FEATURES]
    out = frame[picks].copy()
    out.insert(0, "rank", range(1, len(out) + 1))
    return out


def daily_report(engine, specs: Sequence[ScreenSpec], asof=None, top: int = 10
                 ) -> Tuple[str, Dict[str, object]]:
    """The daily Markdown briefing across ``specs``: an overview table, one
    section per preset (membership change, industry mix, top picks) and
    every data warning.  Returns ``(markdown, data)``.

    The change is measured against the session before the as-of date
    (``engine.diff``); a preset whose diff cannot be formed (no earlier
    session) simply reports no change.
    """
    runs: Dict[str, ScreenResult] = {}
    diffs: Dict[str, Optional[ScreenDiff]] = {}
    for spec in specs:
        result = engine.run(spec, asof=asof)
        runs[spec.name] = result
        try:
            diffs[spec.name] = engine.diff(spec, asof=result.asof)
        except ValueError:
            diffs[spec.name] = None
    store = engine.store
    first = next(iter(runs.values()))
    warnings = list(store.warnings) + sorted(
        {warning for result in runs.values() for warning in result.warnings})
    live = f", live row {store.live_date.date()}" if store.live_date is not None else ""
    lines = [f"# qf-screen daily briefing — {first.asof.date()}", "",
             f"_decided on the {first.asof.date()} close · last session "
             f"{store.last_session.date()}{live} · {len(store.provenance)} names loaded · "
             + (f"{len(warnings)} data warning(s)_" if warnings else "no data warnings_"),
             "", "## Overview", "",
             "| preset | members | entered | exited | retained | top industry |",
             "|---|---|---|---|---|---|"]
    data: Dict[str, object] = {
        "asof": str(first.asof.date()), "warnings": warnings, "presets": {}}
    for spec in specs:
        result, diff = runs[spec.name], diffs[spec.name]
        mix = result.industry_mix()
        classified = mix.drop(labels="未分类", errors="ignore")
        leading = "no members"
        if len(classified):
            leading = (f"{classified.index[0]} {int(classified.iloc[0])} "
                       f"({classified.iloc[0] / mix.sum():.0%})")
        if "未分类" in mix.index:
            leading += (", " if leading != "no members" else "") + f"未分类 {int(mix['未分类'])}"
        if diff is not None:
            entered, exited = f"+{len(diff.entered)}", f"−{len(diff.exited)}"
            kept = diff.retained
            retained = f"{kept} ({kept / len(diff.previous.members):.0%})" \
                if len(diff.previous.members) else str(kept)
            frame_in, frame_out = diff.entered, diff.exited
        else:
            entered = exited = "-"
            retained = "n/a"
            frame_in, frame_out = pd.DataFrame(), pd.DataFrame()
        lines.append(f"| `{spec.name}` | {len(result.members)} | {entered} | {exited} "
                     f"| {retained} | {leading} |")
        presets = data["presets"]
        assert isinstance(presets, dict)
        presets[spec.name] = {
            "members": result.to_frame().astype(str).to_dict(orient="records"),
            "industry_mix": {str(k): int(v) for k, v in mix.items()},
            "entered": frame_in.astype(str).to_dict(orient="records"),
            "exited": frame_out.astype(str).to_dict(orient="records"),
            "notes": result.notes()}
    lines.append("")
    for spec in specs:
        result, diff = runs[spec.name], diffs[spec.name]
        lines += [f"## `{spec.name}` — {len(result.members)} members", ""]
        if diff is not None:
            lines.append("entered: " + (", ".join(_ranked_list(
                diff.entered, None)) if len(diff.entered) else "none"))
            lines.append("")
            lines.append("exited: " + (", ".join(_ranked_list(
                diff.exited, None)) if len(diff.exited) else "none"))
            lines.append("")
        lines.append(f"**industry mix:** {industry_line(result.industry_mix())}")
        lines.append("")
        notes = result.notes()
        if notes:
            lines += [f"_note: {note}_" for note in notes] + [""]
        picks = _top_picks(result, top)
        if len(picks):
            lines += ["top picks:", "", markdown_table(picks), ""]
        else:
            lines += ["_no members_", ""]
    if warnings:
        lines += ["## Data warnings", ""] + [f"- {warning}" for warning in warnings]
    return "\n".join(lines).rstrip() + "\n", data


__all__ = [
    "compare_cells", "compare_markdown", "compare_text", "daily_report", "diff_markdown",
    "diff_payload", "diff_text", "explain_text", "funnel_legend", "funnel_view",
    "inspect_markdown", "inspect_payload", "inspect_text", "run_markdown", "run_payload",
    "run_text",
]
