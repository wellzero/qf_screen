"""``qf-screen`` — the command line.

Subcommands::

    qf-screen list                                 built-in presets
    qf-screen run PRESET [--as-of D] [--top N]     today's snapshot + funnel
                 [--out FILE] [--format table|csv|json] [--explain SYM]
                 [--live] [--symbols A,B] [--workers N] [--data-dir DIR]
    qf-screen pit PRESET --start --end [--freq Q] causal date,symbol schedule
                 [--out FILE]                                    (qf_factor-ready)
    qf-screen validate PRESET --start --end       forward returns vs baseline
                 [--horizons 5,10,20] [--freq M]
    qf-screen data-status [--json]                coverage / freshness census

``run`` decides on the last COMPLETE session by default (a trailing
session whose close count collapsed is a data update in flight); pass
``--as-of`` to decide on any earlier session, or ``--live`` to append the
akshare spot snapshot as a provisional row first (network; disk-cached
with a TTL).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import List, Optional

import pandas as pd

from qf_screen import __version__
from qf_screen.data import CNDataStore
from qf_screen.engine import ScreenEngine, ScreenSpec
from qf_screen.presets import list_presets

logger = logging.getLogger("qf-screen")


def _build_store(args) -> CNDataStore:
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] \
        if getattr(args, "symbols", None) else None
    # pit/validate need HISTORY before their window: a 200-day MA at the
    # first export date needs ~a year of warmup, so the store loads from
    # well before --start and engine.schedule clips the MEMBERSHIP dates
    start = None
    if getattr(args, "command", "") in ("pit", "validate") and getattr(args, "start", None):
        start = str(pd.Timestamp(args.start) - pd.Timedelta(days=500))
    store = CNDataStore(
        data_dir=args.data_dir, symbols=symbols, start=start,
        end=getattr(args, "end", None) if start is not None else None,
        max_workers=args.workers, refresh=bool(getattr(args, "live", False)))
    if getattr(args, "live", False):
        meta = store.attach_live_spot()
        logger.info("live spot: fetched %s (fresh=%s)",
                    meta.get("fetched_at"), meta.get("fresh"))
    return store


def _write_table(frame: pd.DataFrame, path: Optional[str]) -> None:
    if path:
        frame.to_csv(path, index=False)
        print(f"wrote {len(frame)} rows to {path}")
    else:
        with pd.option_context("display.width", 200, "display.max_columns", 40,
                               "display.float_format", lambda v: f"{v:,.4g}"):
            print(frame.to_string(index=False))


def cmd_list(_args) -> int:
    for name in list_presets():
        spec = ScreenSpec.from_yaml(name)
        source = spec.source or ""
        print(f"{name:22s} {(spec.description or '').strip().splitlines()[0] if spec.description else ''}")
        print(f"{'':22s} ({source})")
    return 0


def cmd_run(args) -> int:
    store = _build_store(args)
    engine = ScreenEngine(store)
    spec = ScreenSpec.from_yaml(args.preset)
    result = engine.run(spec, asof=args.as_of)
    for warning in store.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(result.summary())
    print("\nfunnel:")
    _write_table(result.funnel, None)
    frame = result.to_frame()
    if args.top:
        frame = frame.head(int(args.top))
    if args.explain:
        symbol = args.explain
        info = result.explain(symbol)
        print(f"\nexplain {symbol}:")
        for rule, outcome in info.get("rules", {}).items():
            print(f"    {'PASS' if outcome else 'FAIL'}  {rule}")
        for key, value in sorted(info.get("features", {}).items()):
            if isinstance(value, float):
                print(f"    {key:22s} {value:,.4g}")
        if "score" in info:
            print(f"    score {info['score']:,.4f} "
                  f"(percentile {info['score_percentile']:.0%})")
    if args.format == "json":
        payload = {"preset": spec.name, "asof": str(result.asof.date()),
                   "members": frame.astype(str).to_dict(orient="records"),
                   "funnel": result.funnel.astype(str).to_dict(orient="records")}
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            print(f"wrote json to {args.out}")
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if args.out:                        # --out always writes, whatever the view
            frame.to_csv(args.out, index=False)
            print(f"wrote {len(frame)} rows to {args.out}")
        print(f"\nmembers ({len(frame)}):")
        if args.format == "csv" and not args.out:
            print(frame.to_csv(index=False))
        else:
            _write_table(frame, None)
    return 0


def cmd_pit(args) -> int:
    store = _build_store(args)
    engine = ScreenEngine(store)
    spec = ScreenSpec.from_yaml(args.preset)
    schedule, funnel = engine.schedule(spec, start=args.start, end=args.end,
                                       freq=args.freq, out_path=args.out)
    per_day = schedule.groupby("date")["symbol"].nunique() if not schedule.empty else {}
    if len(per_day):
        print(f"schedule: {schedule['date'].nunique()} dates, "
              f"{int(per_day.min())}-{int(per_day.max())} members each, "
              f"{schedule['symbol'].nunique()} symbols ever")
    else:
        print("schedule: EMPTY (check the funnel for a veto-everything rule)")
    print("\nfunnel (avg passing per rebalance):")
    _write_table(funnel, None)
    if not args.out:
        print(schedule.head(20).to_string(index=False))
    return 0


def cmd_validate(args) -> int:
    store = _build_store(args)
    engine = ScreenEngine(store)
    spec = ScreenSpec.from_yaml(args.preset)
    horizons = [int(h) for h in args.horizons.split(",")]
    report = engine.validate(spec, start=args.start, end=args.end,
                             horizons=horizons, freq=args.freq)
    print(f"\n{spec.name}: forward returns of schedule members vs the "
          f"universe baseline ({args.freq} rebalances)")
    print(report.summary())
    if args.out:
        report.per_date.to_csv(args.out, index=False)
        print(f"\nwrote per-date detail to {args.out}")
    return 0


def cmd_data_status(args) -> int:
    store = _build_store(args)
    status = store.data_status()
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    for section in ("window", "universe", "price", "amount_source",
                    "valuation_source", "files_on_disk"):
        print(f"\n{section}:")
        for key, value in status[section].items():
            print(f"  {key:28s} {value}")
    if status.get("warnings"):
        print("\nwarnings:")
        for warning in status["warnings"]:
            print(f"  - {warning}")
    return 0


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
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="list built-in presets")
    list_parser.set_defaults(func=cmd_list)

    run = sub.add_parser("run", parents=[common], help="snapshot screen run")
    run.add_argument("preset")
    run.add_argument("--as-of", default=None, help="decision date (YYYY-MM-DD)")
    run.add_argument("--top", type=int, default=None, help="show only the top N")
    run.add_argument("--out", default=None, help="output CSV/JSON path")
    run.add_argument("--format", choices=("table", "csv", "json"), default="table")
    run.add_argument("--explain", default=None,
                     help="explain this symbol's rule-by-rule outcome")
    run.add_argument("--live", action="store_true",
                     help="append the live akshare spot row first (network)")
    run.add_argument("--symbols", default=None,
                     help="restrict the universe (comma-separated)")
    run.set_defaults(func=cmd_run)

    pit = sub.add_parser("pit", parents=[common],
                         help="export the causal date,symbol schedule")
    pit.add_argument("preset")
    pit.add_argument("--start", required=True)
    pit.add_argument("--end", required=True)
    pit.add_argument("--freq", default=None, choices=("W", "M", "Q"),
                     help="rebalance cadence (default: every session)")
    pit.add_argument("--out", default=None, help="schedule CSV path")
    pit.set_defaults(func=cmd_pit)

    val = sub.add_parser("validate", parents=[common],
                         help="forward-return validation of a schedule")
    val.add_argument("preset")
    val.add_argument("--start", required=True)
    val.add_argument("--end", required=True)
    val.add_argument("--freq", default="M", choices=("W", "M", "Q"))
    val.add_argument("--horizons", default="5,10,20",
                     help="holding horizons in sessions (comma-separated)")
    val.add_argument("--out", default=None, help="per-date detail CSV path")
    val.set_defaults(func=cmd_validate)

    status = sub.add_parser("data-status", parents=[common],
                            help="coverage / freshness census")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_data_status)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except BrokenPipeError:  # `qf-screen ... | head`
        return 0


if __name__ == "__main__":
    sys.exit(main())
