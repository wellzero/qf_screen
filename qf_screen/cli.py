"""``qf-screen`` — the command line.

Subcommands::

    qf-screen list                                 built-in presets
    qf-screen run [PRESET] [--as-of D] [--top N]   today's snapshot + funnel
                 [--where EXPR] [--rank EXPR] [--boards B,B] [--industry NAME]
                 [--concept NAME] [--max-per-industry N] [--explain SYM]
                 [--out FILE] [--format table|csv|json|markdown|md]
                 [--live] [--symbols A,B] [--workers N] [--data-dir DIR]
    qf-screen diff PRESET [--as-of D] [--prev D]  entered / exited / rank movers
                 [--top N] [--out FILE] [--format ...]
    qf-screen inspect SYMBOL [--as-of D]          one name through every preset
                 [--presets NAME ...] [--format table|json|markdown]
    qf-screen report [--presets NAME ...]         the daily briefing
                 [--as-of D] [--top N] [--out FILE]
    qf-screen pit PRESET --start --end [--freq Q] causal date,symbol schedule
                 [--out FILE] [--all-passers] [--where ...] [--rank ...]
    qf-screen validate PRESET --start --end       forward returns, portfolio path,
                 [--horizons 5,10,20] [--freq M]  turnover, Sharpe, benchmarks
                 [--benchmarks 000300,000905] [--curve FILE]
    qf-screen compare PRESET [PRESET ...] --start --end   presets side by side
    qf-screen cache status | clear | warm         the disk caches
    qf-screen features [PATTERN]                  every registered feature
    qf-screen data-status [--json]                coverage / freshness census

``run`` decides on the last COMPLETE session by default (a trailing
session whose close count collapsed is a data update in flight); pass
``--as-of`` to decide on any earlier session, or ``--live`` to append the
akshare spot snapshot as a provisional row first (network; disk-cached
with a TTL).

``run`` without a preset is an ad-hoc screen — ``qf-screen run --where
"roe_best > 0.15" --rank "pct_rank(earnings_yield)" --top 20`` — built on
the required hygiene block only (traded, price >= 2 CNY, 20d mean traded
value >= 5e7; whole universe, no ST, listed >= 1 year).

Ad-hoc screening without writing YAML: ``--where "pe_ttm > 0 and
roe_best > 0.15"`` appends DSL filters, ``--rank "pct_rank(earnings_yield)"``
replaces the ranking, ``--top N`` overrides the preset's ``top_n`` (``0``
= no limit), ``--max-per-industry N`` caps the names per industry, and
``--boards chinext,star`` / ``--industry 银行`` / ``--concept 机器人``
restrict the universe.  Boards come from the static metadata; an industry
is matched OFFLINE against the local 申万 classification of
``lxg/industries.csv`` (all three levels: ``医药生物``, ``白酒`` ...; an
exact name wins, otherwise every name containing the text) — under
``--live`` the 东财 industry boards join in for the names that have no
local classification.  A concept board needs ``--live`` for its 东财
constituents.  ``--industry`` and ``--concept`` given together intersect.

``--out`` honours ``--format``: a member CSV (``table`` / ``csv``), JSON
or a Markdown document.  Every command prints data warnings on stderr and
exits non-zero on a screen it cannot build (unknown preset, unknown
feature, no such board).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from quant_free.common.const_filename import get_data_dir

from qf_screen import __version__
from qf_screen.briefing import (
    compare_markdown, compare_text, daily_report, diff_markdown, diff_payload, diff_text,
    explain_text, inspect_markdown, inspect_payload, inspect_text, run_markdown,
    run_payload, run_text,
)
from qf_screen.data import CNDataStore, LiveDataError, resolve_boards
from qf_screen.data.table_cache import KIND_LABELS
from qf_screen.engine import DEFAULT_BENCHMARKS, ScreenEngine, ScreenSpec
from qf_screen.presets import PresetError, list_presets
from qf_screen.render import fmt_value as _fmt_value   # noqa: F401  (re-exported)
from qf_screen.render import frame_text, human_bytes
from qf_screen.rules import DslError, feature_names

logger = logging.getLogger("qf-screen")

FORMATS = ("table", "csv", "json", "markdown", "md")


# ------------------------------------------------------------- plumbing
def _warn(warnings: Sequence[str]) -> None:
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _jsonable(value):
    """JSON-ready copy of a result structure (numpy scalars, Timestamps
    and missing values become plain Python)."""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, pd.Index, np.ndarray)):
        return [_jsonable(item) for item in value]
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, str):
        return value
    return str(value)


def _write_json(payload, path: Optional[str], what: str = "json") -> None:
    text = json.dumps(_jsonable(payload), ensure_ascii=False, indent=2)
    if path:
        Path(path).write_text(text, encoding="utf-8")
        print(f"wrote {what} to {path}")
    else:
        print(text)


def _write_text(text: str, path: Optional[str], what: str) -> None:
    """A Markdown document to ``--out``, or to stdout when there is none."""
    if path:
        Path(path).write_text(text, encoding="utf-8")
        print(f"wrote {what} to {path}")
    else:
        print(text)


def _limit(value: Optional[int]) -> Optional[int]:
    """``--top N`` as a row limit; ``0`` means no limit."""
    return None if not value else int(value)


def _build_store(args) -> CNDataStore:
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] \
        if getattr(args, "symbols", None) else None
    # pit/validate/compare need HISTORY before their window: a 200-day MA at
    # the first export date needs ~a year of warmup, so the store loads from
    # well before --start and engine.schedule clips the MEMBERSHIP dates
    start = None
    if getattr(args, "command", "") in ("pit", "validate", "compare") and getattr(args, "start", None):
        start = str(pd.Timestamp(args.start) - pd.Timedelta(days=500))
    store = CNDataStore(
        data_dir=args.data_dir, symbols=symbols, start=start,
        end=getattr(args, "end", None) if start is not None else None,
        max_workers=args.workers, refresh=bool(getattr(args, "live", False)),
        use_cache=not getattr(args, "no_cache", False))
    if getattr(args, "live", False):
        meta = store.attach_live_spot()
        logger.info("live spot: fetched %s (fresh=%s)",
                    meta.get("fetched_at"), meta.get("fresh"))
        try:
            store.enrich_industry_tags()
        except LiveDataError as exc:
            logger.warning("东财 industry tags unavailable (%s) -- local lxg "
                           "classification only", exc)
    return store


# ----------------------------------------------------- universe overrides
def _board_symbols(store: CNDataStore, kind: str, wanted: str) -> List[str]:
    """Constituents of the 东财 industry/concept board matching ``wanted``
    (exact name first, then a case-insensitive substring).  Needs the live
    client (``--live``): the constituents are not on disk."""
    frame = store.board_members("industry" if kind == "industry" else "concept")
    boards = frame["board"].astype(str)
    match = boards == wanted
    if not match.any():
        lowered = boards.str.lower()
        match = lowered.str.contains(wanted.lower(), regex=False)
    if not match.any():
        near = sorted(set(boards[boards.str.lower().str.contains(
            wanted.lower()[:2], regex=False)]))[:10]
        raise SystemExit(f"no {kind} board matches {wanted!r}; near misses: "
                         f"{', '.join(near) or 'none'}")
    return sorted(set(frame.loc[match, "symbol"].astype(str)))


def _local_industry_tags(store: CNDataStore) -> Dict[str, tuple]:
    """Loaded symbol -> the industry names it carries in the universe
    census: the local 申万 levels of ``lxg/industries.csv``, plus whatever a
    ``--live`` run filled in from the 东财 boards.  A name without any is
    simply absent."""
    meta = store.universe.meta
    columns = [c for c in ("industry_l1", "industry_l2", "industry_l3") if c in meta.columns]
    tags: Dict[str, tuple] = {}
    if not columns:
        return tags
    values = meta[columns].reindex(store.symbols)
    for symbol, row in zip(values.index, values.to_numpy(dtype=object)):
        found: List[str] = []
        for cell in row:
            text = cell.strip() if isinstance(cell, str) else ""
            if text and text.lower() not in ("nan", "none"):
                found += [part.strip() for part in text.split("/") if part.strip()]
        if found:
            tags[str(symbol)] = tuple(dict.fromkeys(found))
    return tags


def _industry_symbols(store: CNDataStore, wanted: str, live: bool = False) -> List[str]:
    """Loaded symbols of an industry — OFFLINE first.

    ``wanted`` is matched against the local 申万 classification (all three
    levels; an exact name wins, otherwise every name containing the text).
    Under ``--live`` the 东财 industry boards join in: a board matching the
    text contributes its members that carry no local tag (or all of them
    when no local name matched).  A miss is an error naming the near
    misses, not an empty screen.
    """
    wanted = wanted.strip()
    lowered = wanted.lower()
    tags = _local_industry_tags(store)
    names = sorted({tag for found in tags.values() for tag in found})
    matched = [name for name in names if name == wanted] \
        or [name for name in names if lowered in name.lower()]
    allowed = {symbol for symbol, found in tags.items()
               if any(tag in matched for tag in found)}
    boards: List[str] = []
    if live:
        frame = store.board_members("industry")
        board_names = sorted(set(frame["board"].astype(str)))
        boards = [b for b in board_names if b == wanted] \
            or [b for b in board_names if lowered in b.lower()]
        if boards:
            members = set(frame.loc[frame["board"].astype(str).isin(boards), "symbol"]
                          .astype(str))
            # a local classification is the authority wherever one exists
            allowed |= members if not matched else {s for s in members if s not in tags}
    allowed &= set(store.symbols)
    if not allowed:
        near = sorted(n for n in names if lowered[:2] and lowered[:2] in n.lower())[:10]
        message = f"no industry matches {wanted!r}"
        if names:
            message += (" in the local lxg classification (near misses: "
                        f"{', '.join(near) or 'none'})")
        if live:
            message += (" nor among the 东财 industry boards (near misses: "
                        f"{', '.join(boards[:5]) or 'none'})")
        else:
            message += " -- pass --live to add the 东财 industry boards"
        raise SystemExit(message)
    return sorted(allowed)


def _apply_overrides(args, spec: ScreenSpec, store: CNDataStore,
                     keys: Sequence[str]) -> ScreenSpec:
    """CLI flags -> a mutated spec: --where appends filters, --rank replaces
    the ranking, --top / --max-per-industry replace the preset's, and
    --boards / --industry / --concept restrict the universe.  ``keys`` names
    the flags that apply to the calling command.

    ``--industry`` and ``--concept`` are both RESTRICTIONS, so given
    together they intersect (the second used to silently replace the
    first: ``--industry 白酒 --concept 机器人`` screened the whole
    concept board); an empty intersection is an error, not an empty screen.
    """
    universe = spec.universe
    if "boards" in keys and getattr(args, "boards", None):
        universe = replace(universe, boards=resolve_boards(
            [b.strip() for b in args.boards.split(",") if b.strip()]))
    live = bool(getattr(args, "live", False)) and store.live is not None
    restrictions = []
    if "industry" in keys and getattr(args, "industry", None):
        restrictions.append(set(_industry_symbols(store, args.industry, live)))
    if "concept" in keys and getattr(args, "concept", None):
        restrictions.append(set(_board_symbols(store, "concept", args.concept)))
    if restrictions:
        allowed = set.intersection(*restrictions)
        if not allowed:
            raise SystemExit(f"--industry/--concept: the industry {args.industry!r} and "
                             f"the concept {args.concept!r} share no loaded member")
        universe = replace(universe, symbols=sorted(allowed))
    filters = list(spec.filters)
    for i, expr in enumerate(getattr(args, "where", None) or []):
        filters.append({"name": f"where{i + 1}", "expr": expr,
                        "description": "CLI --where"})
    rank = spec.rank
    if getattr(args, "rank", None):
        rank = [{"expr": args.rank, "weight": 1.0}]
    # --top REPLACES the preset's top_n (0 = no limit): it used to be read
    # under the wrong name (args.top_n) and silently ignored
    top = spec.top_n
    if "top" in keys and getattr(args, "top", None) is not None:
        top = _limit(args.top)
    cap = spec.max_per_industry
    if getattr(args, "max_per_industry", None) is not None:
        cap = int(args.max_per_industry)
    out = replace(spec, filters=filters, rank=rank, top_n=top, universe=universe,
                  max_per_industry=cap)
    from qf_screen.presets import validate_filters
    validate_filters(out.filters)        # surface a bad --where HERE, not mid-run
    if rank:
        from qf_screen.rules import ExprRanker
        for entry in rank:
            ExprRanker("rank", str(entry["expr"]))   # same for --rank
    return out


#: CLI keys that can override a preset
_OVERRIDE_KEYS = ("where", "rank", "boards", "industry", "concept", "top",
                  "max_per_industry")
#: ... except ``--top`` where it is a row limit rather than the screen's top_n
_NO_TOP = tuple(key for key in _OVERRIDE_KEYS if key != "top")


def _load_spec(args, store: Optional[CNDataStore] = None,
               keys: Sequence[str] = _OVERRIDE_KEYS) -> ScreenSpec:
    """The screen a command runs: the named preset, or an ad-hoc one when
    no preset is given, with the CLI overrides in ``keys`` applied."""
    preset = getattr(args, "preset", None)
    try:
        if preset:
            spec = ScreenSpec.from_yaml(preset)
        else:
            where = list(getattr(args, "where", None) or [])
            rank = getattr(args, "rank", None)
            if not where and not rank:
                raise SystemExit("run needs a preset, or an ad-hoc screen: "
                                 "--where EXPR [--rank EXPR] (see `qf-screen run --help`)")
            # --where / --rank ARE the ad-hoc spec, not an override of it
            spec = ScreenSpec.adhoc(where=where, rank=rank)
            keys = tuple(key for key in keys if key not in ("where", "rank"))
        if store is not None and any(getattr(args, key, None) is not None for key in keys):
            spec = _apply_overrides(args, spec, store, keys)
    except (DslError, ValueError) as exc:
        raise SystemExit(f"qf-screen: {exc}") from exc
    return spec


def _specs_for(args, store: CNDataStore, names: Sequence[str],
               keys: Sequence[str]) -> List[ScreenSpec]:
    """One spec per preset name, each with the same CLI overrides."""
    return [_load_spec(argparse.Namespace(**{key: getattr(args, key, None) for key in keys}
                                      | {"preset": name}), store, keys)
            for name in names]


def _screen_warnings(store: CNDataStore, engine: ScreenEngine) -> List[str]:
    return list(store.warnings) + list(engine.panel.warnings)


# ------------------------------------------------------------- commands
def cmd_list(_args) -> int:
    for name in list_presets():
        spec = ScreenSpec.from_yaml(name)
        source = spec.source or ""
        first = (spec.description or "").strip().splitlines()[0] if spec.description else ""
        print(f"{name:22s} {first}")
        print(f"{'':22s} (top_n {spec.top_n or 'unlimited'}"
              + (f", max_per_industry {spec.max_per_industry}" if spec.max_per_industry else "")
              + f"; {source})")
    return 0


def cmd_features(args) -> int:
    from qf_screen.rules import FEATURES, allowed_functions
    pattern = (args.pattern or "").lower()
    rows = []
    for name in feature_names():
        if pattern and pattern not in name.lower():
            continue
        _builder, doc, kind = FEATURES[name]
        rows.append({"feature": name, "kind": kind, "description": doc})
    print(f"{len(rows)} registered features"
          + (f" matching {args.pattern!r}" if pattern else "")
          + f" ({len(allowed_functions())} DSL functions: "
            + ", ".join(allowed_functions()) + ")")
    print('dynamic: factor("<family>", "<column>") / '
          "factor_<family>__<column> screens a qf_factor CSV column of the lxg / ak "
          "trees (QF_SCREEN_FACTOR_PROVIDERS names others)\n")
    print(pd.DataFrame(rows).to_string(index=False, max_colwidth=88))
    return 0


def cmd_run(args) -> int:
    store = _build_store(args)
    engine = ScreenEngine(store)
    spec = _load_spec(args, store)
    result = engine.run(spec, asof=args.as_of)
    frame = result.to_frame()
    if args.format == "json":
        payload = run_payload(result, frame)
        payload["store_warnings"] = list(store.warnings)
        if args.explain:
            payload["explain"] = result.explain(args.explain)
        _write_json(payload, args.out)
        return 0
    _warn(_screen_warnings(store, engine) + list(result.warnings))
    print(result.summary())
    if args.explain:
        print(explain_text(result.explain(args.explain)))
    if args.format == "markdown":
        _write_text(run_markdown(result, frame), args.out, "markdown")
        return 0
    if args.format == "csv" and not args.out:
        print(run_text(result, frame, members=False))
        print(f"\nmembers ({len(frame)}):")
        print(frame.to_csv(index=False))
        return 0
    print(run_text(result, frame))
    if args.out:                        # --out always writes, whatever the view
        frame.to_csv(args.out, index=False)
        print(f"wrote {len(frame)} rows to {args.out}")
    return 0


def cmd_diff(args) -> int:
    store = _build_store(args)
    engine = ScreenEngine(store)
    spec = _load_spec(args, store, _NO_TOP)
    try:
        diff = engine.diff(spec, asof=args.as_of, prev=args.prev)
    except ValueError as exc:
        raise SystemExit(f"qf-screen diff: {exc}") from exc
    _warn(_screen_warnings(store, engine)
          + sorted(set(diff.previous.warnings) | set(diff.current.warnings)))
    if args.format == "json":
        _write_json(diff_payload(diff), args.out)
    elif args.format == "markdown":
        _write_text(diff_markdown(diff, _limit(args.top)), args.out, "markdown")
    else:
        print(diff_text(diff, _limit(args.top)))
        if args.out:                    # three tables, three CSV files
            stem = str(Path(args.out).with_suffix(""))
            for label, frame in (("entered", diff.entered), ("exited", diff.exited),
                                 ("movers", diff.movers)):
                frame.to_csv(f"{stem}.{label}.csv", index=False)
            print(f"\nwrote entered / exited / movers to {stem}.{{entered,exited,movers}}.csv")
    return 0


def cmd_inspect(args) -> int:
    store = _build_store(args)
    engine = ScreenEngine(store)
    try:
        card = engine.inspect(args.symbol, asof=args.as_of, presets=args.presets)
    except ValueError as exc:
        raise SystemExit(f"qf-screen inspect: {exc}") from exc
    if args.format == "json":
        payload = inspect_payload(card)
        payload["store_warnings"] = list(store.warnings)
        _write_json(payload, args.out)
        return 0
    _warn(_screen_warnings(store, engine))
    _write_text(inspect_markdown(card) if args.format == "markdown" else inspect_text(card),
                args.out, "scorecard")
    return 0


def cmd_report(args) -> int:
    store = _build_store(args)
    engine = ScreenEngine(store)
    names = list(dict.fromkeys(args.presets or list_presets()))
    specs = _specs_for(args, store, names, _NO_TOP)
    markdown, data = daily_report(engine, specs, asof=args.as_of, top=args.top)
    _warn(_screen_warnings(store, engine))
    if args.format == "json":
        _write_json(data, args.out, "report")
        return 0
    _write_text(markdown, args.out, "report")
    return 0


def cmd_pit(args) -> int:
    store = _build_store(args)
    engine = ScreenEngine(store)
    spec = _load_spec(args, store)
    schedule, funnel = engine.schedule(spec, start=args.start, end=args.end,
                                       freq=args.freq, out_path=args.out,
                                       apply_top_n=not args.all_passers)
    _warn(_screen_warnings(store, engine))
    per_day = schedule.groupby("date")["symbol"].nunique() if not schedule.empty else {}
    if len(per_day):
        print(f"schedule: {schedule['date'].nunique()} dates, "
              f"{int(per_day.min())}-{int(per_day.max())} members each, "
              f"{schedule['symbol'].nunique()} symbols ever")
    else:
        print("schedule: EMPTY (check the funnel for a veto-everything rule)")
    print("\nfunnel (average passing / unjudged names per rebalance):")
    _write_table(funnel)
    if not args.out:
        print(frame_text(schedule.head(20)))
    return 0


def cmd_validate(args) -> int:
    store = _build_store(args)
    engine = ScreenEngine(store)
    spec = _load_spec(args, store)
    horizons = [int(h) for h in args.horizons.split(",")]
    benchmarks = _benchmarks(args)
    report = engine.validate(spec, start=args.start, end=args.end, horizons=horizons,
                             freq=args.freq, apply_top_n=not args.all_passers,
                             benchmarks=benchmarks)
    if args.curve and report.curve is not None:            # whatever the format
        report.curve.to_csv(args.curve)
        print(f"wrote the daily cumulative curve to {args.curve}")
    if args.format == "json":
        _write_json({
            "preset": spec.name, "start": args.start, "end": args.end, "freq": args.freq,
            "horizons": report.horizons, "stats": report.stats, "spread": report.spread,
            "turnover": report.turnover, "notes": report.notes,
            "curve": report.curve if report.curve is None else
            {str(k): v for k, v in report.curve.round(6).to_dict().items()},
            "per_date": report.per_date.astype(str).to_dict(orient="records")}, args.out)
        return 0
    _warn(_screen_warnings(store, engine))
    pool = "the ranked top-N of each date" if not args.all_passers else "every passer"
    print(f"\n{spec.name}: schedule {args.start}..{args.end}, {args.freq} rebalances "
          f"({pool})\nforward returns of the members vs their candidate pool:")
    print(report.summary())
    if args.out:
        report.per_date.to_csv(args.out, index=False)
        print(f"\nwrote per-date detail to {args.out}")
    return 0


def _benchmarks(args) -> List[str]:
    raw = getattr(args, "benchmarks", None)
    return list(DEFAULT_BENCHMARKS) if raw is None \
        else [code.strip() for code in raw.split(",") if code.strip()]


def cmd_compare(args) -> int:
    store = _build_store(args)
    engine = ScreenEngine(store)
    specs = _specs_for(args, store, list(dict.fromkeys(args.presets)), _OVERRIDE_KEYS)
    horizons = [int(h) for h in args.horizons.split(",")]
    table, reports = engine.compare(specs, start=args.start, end=args.end,
                                    horizons=horizons, freq=args.freq,
                                    apply_top_n=not args.all_passers,
                                    benchmarks=_benchmarks(args))
    if not len(table):
        raise SystemExit("qf-screen compare: no preset produced a schedule over the window")
    notes = sorted({note for report in reports.values() for note in report.notes})
    _warn(_screen_warnings(store, engine))
    heading = (f"presets over {args.start}..{args.end}, {args.freq} rebalances "
               "(reference columns: the equal-weight whole market and the benchmarks, "
               "over the same sessions; each preset's own excess is against its own "
               "candidate pool)")
    if args.format == "json":
        _write_json({"start": args.start, "end": args.end, "freq": args.freq,
                     "table": table.to_dict(), "notes": notes,
                     "per_date": {name: report.per_date.astype(str).to_dict(orient="records")
                                  for name, report in reports.items()}}, args.out)
        return 0
    if args.format == "markdown":
        _write_text("# Preset comparison\n\n_" + heading + "_\n\n"
                    + compare_markdown(table, notes), args.out, "markdown")
        return 0
    print("\n" + heading + "\n")
    print(compare_text(table, notes))
    if args.out:
        table.to_csv(args.out)
        print(f"\nwrote the table to {args.out}")
    return 0


def cmd_data_status(args) -> int:
    store = _build_store(args)
    status = store.data_status()
    if args.json:
        print(json.dumps(_jsonable(status), ensure_ascii=False, indent=2))
        return 0
    for section in ("window", "universe", "price", "amount_source",
                    "valuation_source", "files_on_disk", "alt_freshness"):
        print(f"\n{section}:")
        for key, value in status[section].items():
            if isinstance(value, dict):
                print(f"  {key:28s} " + "  ".join(f"{k}={v}" for k, v in value.items()))
            else:
                print(f"  {key:28s} {value}")
    if status.get("warnings"):
        print("\nwarnings:")
        for warning in status["warnings"]:
            print(f"  - {warning}")
    return 0


def cmd_cache(args) -> int:
    from qf_screen import cache_ops
    from qf_screen.data.table_cache import cache_clear, cache_dir_for, cache_inventory

    data_dir, market = args.data_dir or get_data_dir(), "cn"
    directory = cache_dir_for(data_dir, market)
    if args.cache_command == "status":
        status = cache_ops.cache_status(data_dir, market)
        if args.json:
            print(json.dumps(_jsonable(status), ensure_ascii=False, indent=2))
            return 0
        print(f"qf_screen cache: {directory}")
        print(f"{status['entries']} entries, {human_bytes(status['bytes'])}"
              + (f" (+ the --live cache {status['live_cache_dir']})"
                 if Path(status['live_cache_dir']).is_dir() else "") + "\n")
        print(f"{'kind':10s} {'entries':>7s} {'size':>10s}  {'newest':16s}  {'oldest':16s}  what")
        for row in status["kinds"]:
            print(f"{row['kind']:10s} {row['entries']:7d} {human_bytes(row['bytes']):>10s}  "
                  f"{str(row['newest'])[:16]:16s}  {str(row['oldest'])[:16]:16s}  "
                  f"{KIND_LABELS.get(row['kind'], '')}")
        if status["windows"]:
            print("\nderived feature tables by store window:")
            for row in status["windows"]:
                print(f"  {row['window']:44s} {row['entries']:4d} tables  "
                      f"{human_bytes(row['bytes']):>10s}  newest {str(row['newest'])[:16]}")
        for row in status["stores"]:
            state = "not checked" if row["fresh"] is None else (
                "fresh" if row["fresh"] else "STALE -- a source file changed since it was built")
            print(f"\n  {row['name']}\n    {human_bytes(row['bytes']):>10s}  "
                  f"{row.get('mode', '')}  {row['symbols']} symbols, {row['sessions']} sessions "
                  f"{row['window'] or ''}  {state}"
                  + (f"\n    {row['note']}" if row.get("note") else ""))
        return 0

    if args.cache_command == "clear":
        kinds = list(args.what or ["all"])
        if args.dry_run:
            inventory = cache_inventory(data_dir, market)
            doomed = [e for e in inventory if "all" in kinds or e["kind"] in kinds]
            print(f"would remove {len(doomed)} entries "
                  f"({human_bytes(sum(int(e['bytes']) for e in doomed))}) from {directory}")
            for entry in doomed:
                print(f"  {entry['kind']:10s} {entry['name']}")
            return 0
        removed, freed = cache_clear(data_dir, market, kinds)
        print(f"removed {removed} cache entries ({human_bytes(freed)}) -- "
              f"{', '.join(kinds)} -- under {directory} and the --live cache beside it; "
              "everything rebuilds on demand")
        return 0

    result = cache_ops.cache_warm(
        data_dir=data_dir, market=market,
        presets=list(args.presets) if args.presets else None,
        symbols=[s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols
        else None,
        max_workers=args.workers, start=args.start, end=args.end, freq=args.freq)
    if not (args.start and args.end):
        print(f"store: {result['store']} in {result['seconds']['store']:.1f}s")
    else:
        print(f"store (history window): {result['store']} in "
              f"{result['seconds']['store']:.1f}s")
    for name, row in result["presets"].items():
        if "members" in row:
            print(f"  {name:22s} {row['members']:4d} members as of {row['asof']}  "
                  f"({row['seconds']:.1f}s)")
        else:
            print(f"  {name:22s} {row['rows']:5d} schedule rows over {row['dates']} dates  "
                  f"({row['seconds']:.1f}s)")
    print(f"cache warmed in {result['seconds']['total']:.1f}s -> {result['cache_dir']}")
    return 0


def _write_table(frame: pd.DataFrame) -> None:
    with pd.option_context("display.width", 200, "display.max_columns", 40,
                           "display.float_format", lambda v: f"{v:,.4g}"):
        print(frame.to_string(index=False))


# --------------------------------------------------------------- parser
def _screen_flags(parser: argparse.ArgumentParser, *, explain: bool = False,
                  universe: bool = False, top: bool = False, formats: Sequence[str] = FORMATS,
                  out: str = "output file (see --format)") -> None:
    """The flags the screen commands share, each only where it means
    something."""
    parser.add_argument("--as-of", default=None, help="decision date (YYYY-MM-DD)")
    if top:
        parser.add_argument("--top", type=int, default=None,
                            help="override the preset's top_n (0 = no limit)")
    parser.add_argument("--out", default=None, help=out)
    if formats:
        parser.add_argument("--format", choices=formats, default=formats[0])
    if explain:
        parser.add_argument("--explain", default=None,
                            help="explain this symbol's rule-by-rule outcome")
    if not universe:
        return
    parser.add_argument("--where", action="append", default=None, metavar="EXPR",
                        help="append a DSL boolean filter (repeatable), e.g. "
                             '--where "roe_best > 0.15"')
    parser.add_argument("--rank", default=None, metavar="EXPR",
                        help="replace the preset ranking with a DSL expression")
    parser.add_argument("--boards", default=None, metavar="B,B",
                        help="override universe boards (sh_main,chinext,star,bj,"
                             "main,a_share,all)")
    parser.add_argument("--industry", default=None, metavar="NAME",
                        help="restrict to an industry: a local 申万 name from "
                             "lxg/industries.csv (e.g. 医药生物, 白酒; exact, else a "
                             "substring); --live adds the 东财 industry boards")
    parser.add_argument("--concept", default=None, metavar="NAME",
                        help="restrict to a 东财 concept board (needs --live)")
    parser.add_argument("--max-per-industry", default=None, type=int, metavar="N",
                        help="at most N members per industry (default: the preset's)")


def _live_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--live", action="store_true",
                        help="append the live akshare spot row first (network); also "
                             "lets --industry match the 东财 boards and gives an industry "
                             "tag to names that have no local one")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qf-screen", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=None,
                        help="store root (default $QUANT_DATA_DIR)")
    common.add_argument("--workers", type=int, default=8,
                        help="worker processes for loading (default 8)")
    common.add_argument("--no-cache", action="store_true",
                        help="bypass every disk cache (universe census, merged store "
                             "tables and the derived alt/dividend/factor/statement/"
                             "feature tables): recompute from source, read and write nothing")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="list built-in presets")
    list_parser.set_defaults(func=cmd_list)

    features = sub.add_parser("features", help="list registered features")
    features.add_argument("pattern", nargs="?", default=None,
                          help="substring filter (case-insensitive)")
    features.set_defaults(func=cmd_features)

    run = sub.add_parser("run", parents=[common], help="snapshot screen run")
    run.add_argument("preset", nargs="?", default=None,
                     help="a built-in preset or a YAML path (omit for an ad-hoc screen)")
    _screen_flags(run, explain=True, universe=True, top=True)
    _live_flag(run)
    run.add_argument("--symbols", default=None,
                     help="restrict the universe (comma-separated)")
    run.set_defaults(func=cmd_run)

    diff = sub.add_parser("diff", parents=[common],
                          help="entered / exited / rank movers vs the previous session")
    diff.add_argument("preset")
    _screen_flags(diff, universe=True,
                  out="markdown file (--format markdown), JSON (--format json) or, in the "
                      "table view, the stem of <stem>.{entered,exited,movers}.csv")
    diff.add_argument("--top", type=int, default=10,
                      help="rows shown per list (default 10; 0 = all)")
    diff.add_argument("--prev", default=None,
                      help="the earlier decision date (default: the session before --as-of)")
    diff.set_defaults(func=cmd_diff)

    inspect = sub.add_parser("inspect", parents=[common],
                             help="one stock through every preset: pass/fail, why, scorecard")
    inspect.add_argument("symbol", help="e.g. SH600519")
    inspect.add_argument("--presets", nargs="+", default=None, metavar="NAME",
                         help="only these presets (default: every built-in)")
    _screen_flags(inspect, formats=("table", "json", "markdown", "md"),
                  out="markdown file (--format markdown) or JSON (--format json)")
    inspect.set_defaults(func=cmd_inspect)

    report = sub.add_parser("report", parents=[common],
                            help="the daily briefing across presets (Markdown by default)")
    report.add_argument("--presets", nargs="+", default=None, metavar="NAME",
                        help="presets to cover (default: every built-in)")
    report.add_argument("--top", type=int, default=10,
                        help="top picks per preset (default 10; 0 = all)")
    report.add_argument("--format", choices=("markdown", "md", "json"), default="markdown")
    _screen_flags(report, universe=True, formats=(),
                  out="briefing file (default: stdout)")
    _live_flag(report)
    report.set_defaults(func=cmd_report)

    pit = sub.add_parser("pit", parents=[common],
                         help="export the causal date,symbol schedule")
    pit.add_argument("preset")
    pit.add_argument("--start", required=True)
    pit.add_argument("--end", required=True)
    pit.add_argument("--freq", default=None, choices=("W", "M", "Q"),
                     help="rebalance cadence (default: every session)")
    pit.add_argument("--all-passers", action="store_true",
                     help="export every passer: no top_n truncation, no industry cap")
    _screen_flags(pit, universe=True, top=True, formats=(), out="schedule CSV path")
    pit.set_defaults(func=cmd_pit)

    val = sub.add_parser("validate", parents=[common],
                         help="forward-return + portfolio-path validation of a schedule")
    val.add_argument("preset")
    val.add_argument("--start", required=True)
    val.add_argument("--end", required=True)
    val.add_argument("--freq", default="M", choices=("W", "M", "Q"))
    val.add_argument("--horizons", default="5,10,20",
                     help="holding horizons in sessions (comma-separated)")
    val.add_argument("--all-passers", action="store_true",
                     help="validate the whole passing pool, not the ranked top-N")
    val.add_argument("--benchmarks", default=None, metavar="CODES",
                     help="index benchmarks (comma-separated; default 000300,000905 "
                           "where their lxg file is on disk; '' for none)")
    val.add_argument("--curve", default=None,
                     help="write the daily cumulative curve to this CSV")
    _screen_flags(val, universe=True, top=True, formats=("table", "json"),
                  out="per-date detail CSV (or JSON with --format json)")
    val.set_defaults(func=cmd_validate)

    cmp = sub.add_parser("compare", parents=[common],
                         help="run presets side by side over one window")
    cmp.add_argument("presets", nargs="+")
    cmp.add_argument("--start", required=True)
    cmp.add_argument("--end", required=True)
    cmp.add_argument("--freq", default="M", choices=("W", "M", "Q"))
    cmp.add_argument("--horizons", default="5,10,20")
    cmp.add_argument("--all-passers", action="store_true",
                     help="validate the whole passing pool, not the ranked top-N")
    cmp.add_argument("--benchmarks", default=None, metavar="CODES")
    _screen_flags(cmp, universe=True, top=True, formats=("table", "json", "markdown", "md"),
                  out="comparison table CSV (or Markdown / JSON with --format)")
    cmp.set_defaults(func=cmd_compare)

    status = sub.add_parser("data-status", parents=[common],
                            help="coverage / freshness census")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_data_status)

    cache = sub.add_parser("cache", help="inspect / clear / warm the caches")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    # the common flags sit on the sub-commands: `qf-screen cache warm --data-dir X`
    c_status = cache_sub.add_parser("status", parents=[common],
                                    help="what is cached, how big, how fresh")
    c_status.add_argument("--json", action="store_true")
    c_status.set_defaults(func=cmd_cache)
    c_clear = cache_sub.add_parser("clear", parents=[common], help="delete cache entries (they rebuild on demand)")
    c_clear.add_argument("--what", action="append", default=None, metavar="KIND",
                         help="cache kind to remove (repeatable; default all): "
                             + ", ".join(sorted(KIND_LABELS)))
    c_clear.add_argument("--dry-run", action="store_true", help="only list what would go")
    c_clear.set_defaults(func=cmd_cache)
    c_warm = cache_sub.add_parser("warm", parents=[common], help="pre-build the caches a daily run needs")
    c_warm.add_argument("--presets", nargs="+", default=None, metavar="NAME",
                        help="presets to warm (default: every built-in)")
    c_warm.add_argument("--symbols", default=None,
                        help="restrict the universe (comma-separated)")
    c_warm.add_argument("--start", default=None,
                        help="also warm a schedule window (needs --end)")
    c_warm.add_argument("--end", default=None)
    c_warm.add_argument("--freq", default=None, choices=("W", "M", "Q"))
    c_warm.set_defaults(func=cmd_cache)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    if getattr(args, "format", None) == "md":       # the short alias
        args.format = "markdown"
    try:
        return int(args.func(args) or 0)
    except BrokenPipeError:  # `qf-screen ... | head`
        return 0
    except (PresetError, LiveDataError) as exc:
        raise SystemExit(f"qf-screen: {exc}") from exc


if __name__ == "__main__":
    sys.exit(main())
