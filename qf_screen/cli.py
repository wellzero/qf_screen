"""``qf-screen`` — the command line.

Subcommands::

    qf-screen list                                 the presets: built-in and your own
    qf-screen init-preset NAME [--from PRESET]     scaffold a documented custom preset
                 [--out PATH] [--force]
    qf-screen run [PRESET] [--as-of D] [--top N]   today's snapshot + funnel
                 [--where EXPR] [--rank EXPR] [--boards B,B] [--industry NAME]
                 [--concept NAME] [--max-per-industry N] [--explain SYM]
                 [--out FILE] [--format table|csv|json|markdown|md]
                 [--save-history [DIR]] [--live] [--symbols A,B] [--workers N]
                 [--data-dir DIR]
    qf-screen diff PRESET [--as-of D] [--prev D]  entered / exited / rank movers
                 [--top N] [--out FILE] [--format ...] [--fail-on-change]
    qf-screen inspect SYMBOL [--as-of D]          one name through every preset
                 [--presets NAME ...] [--format table|json|markdown]
    qf-screen report [--presets NAME ...]         the daily briefing
                 [--as-of D] [--top N] [--out FILE] [--format markdown|json|html]
                 [--save-history [DIR]]
    qf-screen watch (--symbols A,B | --watchlist FILE)   risk alerts for the names you hold
                 [--preset NAME] [--as-of D] [--format table|markdown|json]
                 [--fail-on-alert]
    qf-screen history PRESET [--days N]           how long each member has stayed
                 [--history-dir DIR] [--format table|markdown|json]
    qf-screen pit PRESET --start --end [--freq Q] causal date,symbol schedule
                 [--out FILE] [--all-passers] [--where ...] [--rank ...]
    qf-screen validate PRESET --start --end       forward returns, portfolio path,
                 [--horizons 5,10,20] [--freq M]  turnover, Sharpe, benchmarks
                 [--benchmarks 000300,000905] [--curve FILE] [--cost-bps BPS]
                 [--skip-limit-up] [--format table|json|html]
    qf-screen compare PRESET [PRESET ...] --start --end   presets side by side
    qf-screen cache status | clear | warm         the disk caches
                 (clear: [--what KIND] [--stale] [--older-than DAYS] [--max-gb N]
                  [--dry-run])
    qf-screen features [PATTERN]                  every registered feature
    qf-screen data-status [--json]                coverage / freshness census
    qf-screen symbol sync [--source ak|lxg|local|all]   the stock & section catalogs
                 [--what stocks,sections] [--no-members] [--pause S]     (network;
                 [--section-filter NAMES] [--lxg-source sw_2021|cni]    `local` is
                 [--api-key KEY] [--no-mirror] [--json]                 offline)
    qf-screen symbol stocks [-q TEXT] [--source ak|lxg|custom]   the cataloged stocks
                 [--limit N] [--format table|csv|json] [--out FILE]
    qf-screen symbol sections [--kind industry|concept|custom]   industries, concepts,
                 [--source ak|lxg|custom] [-q TEXT] ...           your own sections
    qf-screen symbol members SECTION [--source ...]              one section's stocks
    qf-screen symbol lookup TEXT                                stocks AND sections
    qf-screen symbol add-section NAME [--kind ...] [--note ...]  a custom section
    qf-screen symbol remove-section NAME                         delete it
    qf-screen symbol add-members SECTION A,B                     curate membership
    qf-screen symbol remove-members SECTION A,B
    qf-screen symbol status [--json]                             what's cataloged

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
exact name wins, otherwise every name containing the text) and against the
synced symbol catalog (``qf-screen symbol sync``: lxg 申万 sections, 东财
boards, custom sections — a custom one takes every member it was given) —
under ``--live`` the 东财 industry boards join in for the names that have
no local classification.  A concept board resolves offline from the synced
catalog, or needs ``--live`` for its 东财 constituents.
``--industry`` and ``--concept`` given together intersect.

**Presets.**  Every ``*.yaml`` in ``$QF_SCREEN_PRESETS_DIR`` (default
``~/.config/qf_screen/presets/``) is a preset named by its file stem,
found by every command that takes a preset name; one named like a
built-in replaces it.  ``init-preset`` writes a documented starting file
there.  A list of presets (``--presets``) takes spaces or commas.

**Costs and the limit-up guard** (``validate`` / ``compare``).  The
per-horizon forward returns measure the SCREEN, gross.  ``--cost-bps BPS``
prices the portfolio path like a book: BPS per side of every trade (the
initial purchase, each rebalance's sells and buys, the final
liquidation), and the report carries the net figures beside the gross
ones.  ``--skip-limit-up`` refuses to buy a member whose close sits at its
board's daily limit on the entry day (10 % main board, 20 % ChiNext /
STAR, 30 % Beijing): that share of the book stays in cash for the period.

**Exit codes.**  ``0`` all is well; ``1`` a screen could not be built;
``2`` ``diff --fail-on-change`` saw a change or ``watch --fail-on-alert``
saw an alert; ``3`` a webhook that was asked for could not be delivered.

**Snapshots.**  ``run`` / ``report --save-history [DIR]`` appends the
session's ranked members to a CSV per preset under
``$QF_SCREEN_HISTORY_DIR`` (default ``~/.local/share/qf_screen/history/``),
idempotent per (date, preset); ``history PRESET`` reads back how many
sessions in a row each member has stayed.

**Webhooks** (``diff``, ``watch``, ``report``) are strictly opt-in:
nothing is sent unless ``--webhook-format json|feishu|dingtalk|slack`` is
on the command line.  The URL is ``--webhook-url`` or
``$QF_SCREEN_WEBHOOK_URL``, the bot's signing secret ``--webhook-secret``
or ``$QF_SCREEN_WEBHOOK_SECRET``.

**The cache** only grows by use.  ``cache status`` shows it;
``cache clear`` deletes by kind, or prunes: ``--stale`` (what no run will
read again: another version's layout, an interrupted write, a
merged-store cache whose sources moved on, and the tables of such
windows — the fresh daily window stays), ``--older-than DAYS`` (not used
for that long), ``--max-gb N`` (least recently used until it fits N GB).
``--dry-run`` lists what would go, with the reason.

``--out`` honours ``--format``: a member CSV (``table`` / ``csv``), JSON,
a Markdown document, or -- for ``report`` / ``validate`` / ``compare`` --
one self-contained HTML page (inline SVG charts, sortable and filterable
tables; nothing is fetched).  Every command prints data warnings on
stderr and exits non-zero on a screen it cannot build (unknown preset,
unknown feature, no such board).
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
from qf_screen.ui.briefing import (
    compare_markdown, compare_text, daily_report, diff_markdown, diff_payload, diff_text,
    explain_text, inspect_markdown, inspect_payload, inspect_text, run_markdown,
    run_payload, run_text,
)
from qf_screen.data import CNDataStore, LiveDataError, resolve_boards
from qf_screen.data.table_cache import KIND_LABELS
from qf_screen.engine import DEFAULT_BENCHMARKS, ScreenEngine, ScreenSpec
from qf_screen.history import HISTORY_ENV, HistoryError, history_dir, save_snapshot
from qf_screen.notify import FORMATS as WEBHOOK_FORMATS
from qf_screen.presets import (
    USER_PRESETS_ENV, PresetError, list_presets, preset_origin, user_preset_dir,
)
from qf_screen.symbols import SymbolStore, SymbolError, sync_symbols
from qf_screen.ui.render import fmt_value as _fmt_value   # noqa: F401  (re-exported)
from qf_screen.ui.render import frame_text, human_bytes, markdown_table
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


class _NameList(argparse.Action):
    """A preset list that accepts ``--presets a b`` AND ``--presets a,b``
    (the spelling ``--boards`` / ``--symbols`` / ``--horizons`` always
    took).  ``nargs="+"`` alone used to hand ``report`` the single name
    ``"momentum_trend,value_dividend"`` and fail on it."""

    def __call__(self, parser, namespace, values, option_string=None):
        names = [part.strip() for value in values for part in str(value).split(",")]
        names = [name for name in names if name]
        if not names:
            parser.error(f"{option_string} needs at least one preset name")
        setattr(namespace, self.dest, list(dict.fromkeys(names)))


def _cost_bps(value: str) -> float:
    """``--cost-bps``: basis points per side of a trade, ``0 .. 10000``."""
    try:
        rate = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not 0.0 <= rate < 1e4:
        raise argparse.ArgumentTypeError("cost must be 0 or more basis points (below 10000)")
    return rate


#: exit code of ``diff --fail-on-change`` / ``watch --fail-on-alert`` when
#: the condition fires (a cron job keys on it)
EXIT_ALERT = 2
#: exit code when a ``--webhook-*`` push could not be delivered
EXIT_WEBHOOK = 3


def _build_store(args, restrict: bool = True) -> CNDataStore:
    """The store a command reads: the default universe, restricted to
    ``--symbols`` when the command uses that flag for a store restriction
    (``watch`` does not -- its ``--symbols`` is the watchlist, and a
    ``--preset`` needs the whole market to rank against)."""
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] \
        if restrict and getattr(args, "symbols", None) else None
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
def _catalog(store: CNDataStore, kind: str):
    """section name -> members from the LOCAL symbol catalog (sections of
    ``kind``, every source), or ``{}`` when nothing is synced."""
    sym = getattr(store, "symbol_store", None)
    return sym.membership(kind) if sym is not None and sym.present else {}


def _match_names(wanted: str, names: Sequence[str]) -> List[str]:
    """Exact name first, then case-insensitive substrings."""
    exact = [name for name in names if name == wanted]
    if exact:
        return exact
    lowered = wanted.lower()
    return [name for name in names if lowered in name.lower()]


def _board_symbols(store: CNDataStore, kind: str, wanted: str) -> List[str]:
    """Constituents of the industry/concept board matching ``wanted``
    (exact name first, then a case-insensitive substring).

    Offline from the synced symbol catalog when one exists (ak 东财 boards
    + custom sections of that kind); the live client (``--live``) is the
    fallback for what is not on disk."""
    wanted = wanted.strip()
    catalog = _catalog(store, kind)
    matched = _match_names(wanted, sorted(catalog)) if catalog else []
    if matched:
        members: set = set()
        for name in matched:
            members |= set(catalog[name])
        return sorted(members)
    try:
        frame = store.board_members("industry" if kind == "industry" else "concept")
    except LiveDataError:
        near = sorted(name for name in catalog
                      if wanted.lower()[:2] in name.lower())[:10]
        message = f"no {kind} board matches {wanted!r}"
        if near:
            message += f"; catalog near misses: {', '.join(near)}"
        message += (" -- sync the catalog (`qf-screen symbol sync`) or pass "
                    "--live for the 东财 boards")
        raise SystemExit(message) from None
    boards = frame["board"].astype(str)
    match = boards == wanted
    if not match.any():
        lowered = boards.str.lower()
        match = lowered.str.contains(wanted.lower(), regex=False)
    if not match.any():
        near = sorted(set(boards[boards.str.lower().str.contains(
            wanted.lower()[:2], regex=False)]))[:10]
        message = (f"no {kind} board matches {wanted!r}; near misses: "
                   f"{', '.join(near) or 'none'}")
        if catalog:
            message += (" -- `qf-screen symbol sync` refreshes it, or pass "
                        "--live for the 东财 boards")
        raise SystemExit(message)
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
    levels; an exact name wins, otherwise every name containing the text),
    then against the synced symbol catalog's industry sections (lxg 申万,
    ak 东财 boards, custom).  A catalog section that is CUSTOM is
    authoritative (its curated members join in as-is); ak/lxg ones fill
    gaps, exactly like the ``--live`` boards below: a board matching the
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
    sym = getattr(store, "symbol_store", None)
    custom_names: set = set()
    catalog = _catalog(store, "industry")
    if catalog:
        custom_names = set(
            sym.sections(kind="industry", source="custom")["name"])
    for name in _match_names(wanted, sorted(catalog)) if catalog else []:
        members = set(catalog[name])
        if name in custom_names:
            allowed |= members               # curated: takes every member
        elif not matched:
            allowed |= members               # no local name matched at all
        else:
            allowed |= {s for s in members if s not in tags}
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
        if catalog:
            cat_near = sorted(n for n in catalog if lowered[:2] in n.lower())[:10]
            message += (f" nor the symbol catalog (near misses: "
                        f"{', '.join(cat_near) or 'none'}; `qf-screen symbol sync` "
                        "refreshes it)")
        if live:
            message += (" nor among the 东财 industry boards (near misses: "
                        f"{', '.join(boards[:5]) or 'none'})")
        else:
            message += " -- sync the catalog (`qf-screen symbol sync`) or " \
                       "pass --live to add the 东财 industry boards"
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
    """Every preset the commands can name, built-in and user, each with
    where it comes from.  A user preset that does not load is listed with
    the reason instead of hiding the rest (and makes the exit non-zero)."""
    broken = 0
    for name in list_presets():
        origin = preset_origin(name) or {"kind": "built-in"}
        try:
            spec = ScreenSpec.from_yaml(name)
        except ValueError as exc:                     # PresetError / DslError
            broken += 1
            print(f"{name:22s} DOES NOT LOAD: {' '.join(str(exc).splitlines())}")
            continue
        first = (spec.description or "").strip().splitlines()[0] if spec.description else ""
        source = {"built-in": "built-in",
                  "user": f"user: {origin.get('path')}",
                  "override": f"user preset, REPLACES the built-in: {origin.get('path')}"}
        print(f"{name:22s} {first}")
        print(f"{'':22s} (top_n {spec.top_n or 'unlimited'}"
              + (f", max_per_industry {spec.max_per_industry}" if spec.max_per_industry else "")
              + f"; {source[str(origin['kind'])]})")
    print(f"\nuser presets are discovered in {user_preset_dir()} "
          f"(${USER_PRESETS_ENV}); `qf-screen init-preset NAME` writes one")
    return 1 if broken else 0


def cmd_init_preset(args) -> int:
    from qf_screen.presets import init_preset

    try:
        path, notes = init_preset(args.name, base=args.copy_from, out=args.out,
                                  force=args.force)
    except PresetError as exc:
        raise SystemExit(f"qf-screen: {exc}") from exc
    print(f"wrote {path}")
    print(f"  qf-screen list{'':23s}# it shows up as a user preset")
    print(f"  qf-screen run {path.stem} --top 10 --explain SH600519")
    print(f"  qf-screen validate {path.stem} --start 2026-01-05 --end 2026-09-30")
    for note in notes:
        print(f"note: {note}")
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
    if args.save_history:
        _archive(args.save_history, [(spec.name, result)])
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
    payload = diff_payload(diff)
    if args.format == "json":
        _write_json(payload, args.out)
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
    changed = bool(len(diff.entered) or len(diff.exited))
    code = EXIT_ALERT if args.fail_on_change and changed else 0
    return _push(args, {"event": "diff", **payload}) or code


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


def cmd_watch(args) -> int:
    from qf_screen.ui.watch import (
        load_watchlist, watch, watch_markdown, watch_payload, watch_text,
    )

    if not getattr(args, "symbols", None) and not getattr(args, "watchlist", None):
        raise SystemExit("qf-screen watch: pass --symbols A,B or --watchlist FILE "
                         "(see `qf-screen watch --help`)")
    # a bad file, an unknown preset and a rule that will not compile are all
    # one kind of error to the user: `qf-screen watch: <why>`, not a traceback
    try:
        listed = load_watchlist(args.watchlist) if args.watchlist else None
    except ValueError as exc:                         # WatchlistError
        raise SystemExit(f"qf-screen watch: {exc}") from exc
    store = _build_store(args, restrict=False)
    engine = ScreenEngine(store)
    # a --symbols flag and a --watchlist file combine; the flag's --preset wins
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] \
        if args.symbols else []
    try:
        report = watch(
            engine, (listed.symbols if listed else []) + symbols,
            preset=args.preset or (listed.preset if listed else None),
            rules=listed.rules if listed else (), disable=listed.disable if listed else (),
            thresholds=listed.thresholds if listed else None, asof=args.as_of)
    except ValueError as exc:                         # WatchlistError / PresetError / DslError
        raise SystemExit(f"qf-screen watch: {exc}") from exc
    payload = watch_payload(report)
    if args.format == "json":
        _write_json(payload, args.out, "watch report")
    else:
        _warn(report.warnings)
        _write_text(watch_markdown(report) if args.format == "markdown" else watch_text(report),
                    args.out, "watch report")
    code = EXIT_ALERT if args.fail_on_alert and report.count else 0
    return _push(args, {"event": "watch", **payload}) or code


def cmd_report(args) -> int:
    store = _build_store(args)
    engine = ScreenEngine(store)
    names = list(dict.fromkeys(args.presets or list_presets()))
    specs = _specs_for(args, store, names, _NO_TOP)
    document, data = daily_report(
        engine, specs, asof=args.as_of, top=args.top,
        view="html" if args.format == "html" else "markdown",
        on_result=(lambda result: _archive(args.save_history, [(result.spec.name, result)]))
        if args.save_history else None)
    _warn(_screen_warnings(store, engine))
    if args.format == "json":
        _write_json(data, args.out, "report")
    else:
        _write_text(document, args.out, "HTML report" if args.format == "html" else "report")
    return _push(args, {"event": "report", **data})


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
                             benchmarks=benchmarks, cost_bps=args.cost_bps,
                             skip_limit_up=args.skip_limit_up)
    if args.curve and report.curve is not None:            # whatever the format
        report.curve.to_csv(args.curve)
        print(f"wrote the daily cumulative curve to {args.curve}")
    if args.format == "html":
        from qf_screen.ui.html_report import validate_html

        _write_text(validate_html(
            report, preset=spec.name, start=args.start, end=args.end, freq=args.freq,
            pool="the ranked top-N of each date" if not args.all_passers else "every passer",
            execution=_execution_note(args)[2:] or "before costs",
            warnings=_screen_warnings(store, engine)),
            args.out, "HTML report")
        return 0
    if args.format == "json":
        _write_json({
            "preset": spec.name, "start": args.start, "end": args.end, "freq": args.freq,
            "horizons": report.horizons, "stats": report.stats, "spread": report.spread,
            "turnover": report.turnover, "notes": report.notes,
            "cost_bps": report.cost_bps, "net": report.net, "skipped": report.skipped,
            "curve": report.curve if report.curve is None else
            {str(k): v for k, v in report.curve.round(6).to_dict().items()},
            "per_date": report.per_date.astype(str).to_dict(orient="records")}, args.out)
        return 0
    _warn(_screen_warnings(store, engine))
    pool = "the ranked top-N of each date" if not args.all_passers else "every passer"
    print(f"\n{spec.name}: schedule {args.start}..{args.end}, {args.freq} rebalances "
          f"({pool}{_execution_note(args)})\nforward returns of the members vs their "
          "candidate pool:")
    print(report.summary())
    if args.out:
        report.per_date.to_csv(args.out, index=False)
        print(f"\nwrote per-date detail to {args.out}")
    return 0


def _benchmarks(args) -> List[str]:
    raw = getattr(args, "benchmarks", None)
    return list(DEFAULT_BENCHMARKS) if raw is None \
        else [code.strip() for code in raw.split(",") if code.strip()]


def _execution_note(args) -> str:
    """The execution assumptions a validation ran with, for its heading."""
    parts = []
    if getattr(args, "cost_bps", 0.0):
        parts.append(f"{args.cost_bps:g} bps cost per side")
    if getattr(args, "skip_limit_up", False):
        parts.append("limit-up entries skipped")
    return "".join(f", {part}" for part in parts)


def _webhook_flags(parser: argparse.ArgumentParser) -> None:
    """``--webhook-format`` is the opt-in switch: without it no command ever
    opens the network, whatever the environment holds."""
    parser.add_argument("--webhook-format", choices=WEBHOOK_FORMATS, default=None,
                        help="push the result to a webhook in this format: json (the "
                             "command's own payload), feishu, dingtalk or slack -- the "
                             "concise text reading")
    parser.add_argument("--webhook-url", default=None, metavar="URL",
                        help="where to post (default $QF_SCREEN_WEBHOOK_URL)")
    parser.add_argument("--webhook-secret", default=None, metavar="SECRET",
                        help="the bot's signing secret (default $QF_SCREEN_WEBHOOK_SECRET): "
                             "飞书 签名校验 / 钉钉 加签")


def _push(args, event: Dict[str, object]) -> int:
    """Deliver ``event`` when the command opted in.  ``0`` when it arrived
    or was not asked for; ``EXIT_WEBHOOK`` when it was asked for and did
    not -- an alert nobody received must not look like a quiet day."""
    fmt = getattr(args, "webhook_format", None)
    if not fmt:
        return 0
    from qf_screen.notify import send

    delivery = send(fmt, {"generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
                          **event}, url=args.webhook_url, secret=args.webhook_secret)
    if delivery.ok:
        print(f"webhook: {fmt} delivered (HTTP {delivery.status})", file=sys.stderr)
        return 0
    print(f"qf-screen: webhook ({fmt}) NOT delivered -- {delivery.detail}", file=sys.stderr)
    return EXIT_WEBHOOK


def _history_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--save-history", nargs="?", const=str(history_dir()), default=None,
                        metavar="DIR",
                        help="append today's ranked members to the snapshot archive "
                             f"(default DIR {history_dir()}; ${HISTORY_ENV}) -- idempotent per "
                             "(date, preset); read it back with `qf-screen history PRESET`")


def _archive(directory: str, snapshots: Sequence[tuple]) -> None:
    """``(preset name, run)`` pairs -> the archive; what was written goes to
    stderr, so a ``--format json`` stdout stays pure JSON."""
    for name, result in snapshots:
        info = save_snapshot(directory, name, result.asof, result.members,
                             result.scores.to_numpy())
        note = f" (replaced the {info['replaced']} rows of an earlier run)" if info["replaced"] \
            else ""
        # a plain stderr line, not a log record: it must be seen whatever the log level
        print(f"history: {info['rows']} rows for {name} as of {result.asof.date()} -> "
              f"{info['file']}{note}", file=sys.stderr)


def _symbol_names(data_dir, symbols) -> Dict[str, str]:
    """Company names for the archive's symbols, from the universe census
    alone (no table is loaded).  Best effort: the symbols are enough."""
    from qf_screen.data import build_universe

    try:
        universe = build_universe(data_dir or get_data_dir())
    except Exception as exc:                              # noqa: BLE001 - decoration only
        logger.warning("names unavailable (%s) -- showing symbols only", exc)
        return {}
    return {str(symbol): str(universe.get(str(symbol), "name", "") or "")
            for symbol in symbols}


def cmd_history(args) -> int:
    from qf_screen.history import history_view, load_history

    directory = history_dir(args.history_dir)
    try:
        frame = load_history(directory, args.preset)
        view = history_view(frame, args.preset, days=args.days,
                            names=_symbol_names(args.data_dir, sorted(set(frame["symbol"]))))
    except HistoryError as exc:
        raise SystemExit(f"qf-screen history: {exc}") from exc
    if not len(frame):
        raise SystemExit(f"qf-screen history: no snapshots of {args.preset!r} under "
                         f"{directory} -- run `qf-screen run {args.preset} --save-history` "
                         "first (or point --history-dir at the archive)")
    members = view.members.copy()
    members["stayed"] = [f"{value:.0%}" for value in members["stayed"]]
    members["score"] = [f"{value:.3f}" if np.isfinite(value) else "-" for value in members["score"]]
    archived = int(frame["asof"].nunique())
    dates = sorted(frame["asof"].unique())
    head = (f"{args.preset}: {len(members)} members as of {view.asof} · "
            f"{view.new_today} new today")
    scope = (f"archive: {archived} session{'s' if archived != 1 else ''} {dates[0]}..{dates[-1]}; "
             f"counting the last {len(view.sessions)} ({view.sessions[0]}..{view.sessions[-1]})")
    caution = view.caution
    if args.format == "json":
        _write_json({"preset": args.preset, "asof": view.asof, "directory": str(directory),
                     "archived_sessions": archived, "window": view.sessions, "days": args.days,
                     "members_per_session": view.sizes, "new_today": view.new_today,
                     "note": caution,
                     "members": members.astype(str).to_dict(orient="records"),
                     "exited": view.exited.astype(str).to_dict(orient="records")},
                    args.out, "history")
        return 0
    left = view.exited
    if args.format == "markdown":
        lines = [f"## `{args.preset}` — {len(members)} members as of {view.asof}", "",
                 f"_{scope} · {view.new_today} new today_", "", markdown_table(members), ""]
        if caution:
            lines += [f"_Note: {caution}_", ""]
        if len(left):
            lines += [f"### Recently exited (in the list inside the window, gone by {view.asof})",
                      "", markdown_table(left), ""]
        _write_text("\n".join(lines), args.out, "history")
        return 0
    print(head)
    print(scope)
    if caution:
        print(f"note: {caution}")
    print()
    print(frame_text(members))
    if len(left):
        print(f"\nrecently exited (in the list inside the window, gone by {view.asof}):")
        print(frame_text(left))
    if args.out:
        members.to_csv(args.out, index=False)
        print(f"\nwrote {len(members)} rows to {args.out}")
    return 0


def cmd_compare(args) -> int:
    store = _build_store(args)
    engine = ScreenEngine(store)
    specs = _specs_for(args, store, list(dict.fromkeys(args.presets)), _OVERRIDE_KEYS)
    horizons = [int(h) for h in args.horizons.split(",")]
    table, reports = engine.compare(specs, start=args.start, end=args.end,
                                    horizons=horizons, freq=args.freq,
                                    apply_top_n=not args.all_passers,
                                    benchmarks=_benchmarks(args), cost_bps=args.cost_bps,
                                    skip_limit_up=args.skip_limit_up)
    if not len(table):
        raise SystemExit("qf-screen compare: no preset produced a schedule over the window")
    notes = sorted({note for report in reports.values() for note in report.notes})
    _warn(_screen_warnings(store, engine))
    heading = (f"presets over {args.start}..{args.end}, {args.freq} rebalances "
               f"({_execution_note(args)[2:] or 'gross'}; reference columns: the "
               "equal-weight whole market and the benchmarks, over the same sessions; "
               "each preset's own excess is against its own candidate pool)")
    if args.format == "json":
        _write_json({"start": args.start, "end": args.end, "freq": args.freq,
                     "table": table.to_dict(), "notes": notes,
                     "per_date": {name: report.per_date.astype(str).to_dict(orient="records")
                                  for name, report in reports.items()}}, args.out)
        return 0
    if args.format == "html":
        from qf_screen.ui.html_report import compare_html

        _write_text(compare_html(
            table, reports, start=args.start, end=args.end, freq=args.freq,
            execution=_execution_note(args)[2:] or "before costs", notes=notes,
            warnings=_screen_warnings(store, engine)), args.out, "HTML report")
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
        print("\nprune: qf-screen cache clear --dry-run --stale | --older-than N | --max-gb N")
        return 0

    if args.cache_command == "clear":
        kinds = list(args.what or ["all"])
        selecting = args.stale or args.older_than is not None or args.max_gb is not None
        if selecting:
            return _cache_prune(args, data_dir, market)
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


def _prune_summary(counts: Dict[str, int], older_than) -> str:
    """``9 stale, 3 not used for 30 days, 2 least recently used`` -- the
    selectors that picked the entries, in the order they apply."""
    parts = []
    if counts.get("stale"):
        parts.append(f"{counts['stale']} stale")
    if counts.get("old"):
        parts.append(f"{counts['old']} not used for {older_than:g} days")
    if counts.get("lru"):
        parts.append(f"{counts['lru']} least recently used")
    return ", ".join(parts)


def _cache_prune(args, data_dir, market: str) -> int:
    """``cache clear`` with a selector (``--stale`` / ``--older-than`` /
    ``--max-gb``): plan, show, and delete unless ``--dry-run``."""
    from qf_screen.cache_ops import cache_prune

    plan = cache_prune(data_dir, market, args.what, stale=args.stale,
                       older_than=args.older_than, max_gb=args.max_gb, dry_run=args.dry_run)
    doomed = plan["remove"]
    before, after = human_bytes(plan["total_bytes"]), human_bytes(plan["after_bytes"])
    summary = _prune_summary(plan["counts"], args.older_than)
    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} {len(doomed)} cache entries ({human_bytes(sum(int(r['bytes']) for r in doomed))})"
          f" of {len(doomed) + len(plan['keep'])} ({before} -> {after})"
          + (f": {summary}" if summary else "")
          + f" under {plan['cache_dir']} and the --live cache beside it")
    for row in doomed:
        print(f"  {row['kind']:10s} {row['name']}")
        print(f"  {'':10s} {row['detail']}")
    for note in plan["notes"]:
        print(f"note: {note}")
    if not args.dry_run and doomed:
        print("everything rebuilds on demand")
    return 0


def _write_table(frame: pd.DataFrame) -> None:
    with pd.option_context("display.width", 200, "display.max_columns", 40,
                           "display.float_format", lambda v: f"{v:,.4g}"):
        print(frame.to_string(index=False))


# ------------------------------------------------- symbol catalog commands
def _symbol_store(args) -> SymbolStore:
    return SymbolStore(args.data_dir, getattr(args, "market", "cn"))


def _emit_frame(frame: pd.DataFrame, args, what: str) -> None:
    """A listing to stdout or ``--out`` as table / csv / json."""
    fmt = getattr(args, "format", "table")
    limit = getattr(args, "limit", None)
    if limit:
        frame = frame.head(int(limit))
    if fmt == "json":
        _write_json(frame.to_dict("records"), getattr(args, "out", None), what)
        return
    text = frame.to_csv(index=False) if fmt == "csv" else frame_text(frame)
    _write_text(text, getattr(args, "out", None), what)


def cmd_symbol_sync(args) -> int:
    """``symbol sync``: download the catalogs into ``<market>/symbol/``."""
    section_filter = None
    if args.section_filter:
        wanted = {name.strip() for name in args.section_filter.split(",") if name.strip()}
        section_filter = wanted or None
    result = sync_symbols(
        store=_symbol_store(args), source=args.source,
        what=tuple(part for part in args.what.split(",") if part.strip()),
        include_members=not args.no_members, pause=args.pause,
        section_filter=section_filter, lxg_source=args.lxg_source,
        api_key=args.api_key, mirror_company_list=not args.no_mirror)
    if args.json:
        _write_json(result.as_dict(), args.out, "sync result")
    else:
        landed = ", ".join(f"{key}={rows}" for key, rows in
                           [*(result.stocks.items()), *(result.sections.items()),
                            *(result.members.items())]) or "nothing"
        print(f"symbol sync ({'+'.join(result.sources)}) -> {landed} rows "
              f"under {result.data_dir}/{result.market}/symbol/")
    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)
    for error in result.errors:
        print(f"warning: {error}", file=sys.stderr)
    return 1 if result.errors else 0


def cmd_symbol_stocks(args) -> int:
    """``symbol stocks``: the stock catalog (all sources by default)."""
    frame = _symbol_store(args).stocks(source=args.source, query=args.query)
    _emit_frame(frame, args, "stocks")
    return 0


def cmd_symbol_sections(args) -> int:
    """``symbol sections``: the section catalog (industry / concept / custom)."""
    frame = _symbol_store(args).sections(kind=args.kind, source=args.source,
                                         query=args.query)
    _emit_frame(frame, args, "sections")
    return 0


def cmd_symbol_members(args) -> int:
    """``symbol members SECTION``: one section's constituents."""
    store = _symbol_store(args)
    symbols = store.section_members(args.section, source=args.source)
    names = store.stock_names()
    frame = pd.DataFrame({
        "symbol": symbols, "code": [s[2:] for s in symbols],
        "name": [names.get(s, "") for s in symbols]})
    if not len(frame):
        raise SystemExit(f"section {args.section!r} has no members"
                         + ("" if store.present
                            else " (nothing synced: `qf-screen symbol sync`)"))
    print(f"{len(frame)} member(s)")
    _emit_frame(frame, args, "members")
    return 0


def cmd_symbol_lookup(args) -> int:
    """``symbol lookup QUERY``: stocks AND sections matching the text."""
    store = _symbol_store(args)
    stocks = store.stocks(query=args.query)
    sections = store.sections(query=args.query)
    if args.format == "json":
        _write_json({"stocks": stocks.to_dict("records"),
                     "sections": sections.to_dict("records")},
                    args.out, "lookup")
        return 0
    print(f"{len(stocks)} stock(s), {len(sections)} section(s)")
    if len(stocks):
        print(frame_text(stocks))
    if len(sections):
        print(frame_text(sections))
    if not len(stocks) and not len(sections):
        print(f"nothing matches {args.query!r}"
              + ("" if store.present
                 else " (nothing synced: `qf-screen symbol sync`)"))
    return 0


def cmd_symbol_add_section(args) -> int:
    row = _symbol_store(args).upsert_section(args.name, kind=args.kind,
                                             note=args.note or "")
    print(f"section {row['name']!r} ({row['kind']}) ready"
          f" -- add members with `qf-screen symbol add-members "
          f"{row['name']} SH600519,...`")
    return 0


def cmd_symbol_remove_section(args) -> int:
    gone = _symbol_store(args).delete_section(args.name)
    print(f"removed section {args.name!r} and {gone} member(s)")
    return 0


def cmd_symbol_add_members(args) -> int:
    symbols = [part.strip() for part in args.symbols.split(",") if part.strip()]
    added = _symbol_store(args).add_section_members(args.section, symbols)
    print(f"added {added} member(s) to {args.section!r}")
    return 0


def cmd_symbol_remove_members(args) -> int:
    symbols = [part.strip() for part in args.symbols.split(",") if part.strip()]
    gone = _symbol_store(args).remove_section_members(args.section, symbols)
    print(f"removed {gone} member(s) from {args.section!r}")
    return 0


def cmd_symbol_status(args) -> int:
    """``symbol status``: what is cataloged, how fresh."""
    status = _symbol_store(args).status()
    if args.json:
        _write_json(status, args.out, "symbol status")
        return 0
    state = "present" if status["present"] else \
        "EMPTY -- nothing synced: `qf-screen symbol sync`"
    print(f"catalog: {status['root']} ({state})")
    for source, tables in status["sources"].items():
        parts = [f"{what} {entry['rows']}"
                 + (f" (synced {entry['synced_at']})" if entry["synced_at"] else "")
                 for what, entry in tables.items()]
        print(f"  {source:6s} " + ", ".join(parts))
    return 0


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
                             "substring), or a section of the synced symbol catalog "
                             "(lxg / 东财 / your custom ones); --live adds the 东财 "
                             "industry boards")
    parser.add_argument("--concept", default=None, metavar="NAME",
                        help="restrict to a 东财 concept board: offline once "
                             "`qf-screen symbol sync` ran, else --live")
    parser.add_argument("--max-per-industry", default=None, type=int, metavar="N",
                        help="at most N members per industry (default: the preset's)")


def _execution_flags(parser: argparse.ArgumentParser) -> None:
    """``--cost-bps`` and ``--skip-limit-up``: the execution assumptions
    ``validate`` / ``compare`` can price into the portfolio path."""
    parser.add_argument("--cost-bps", type=_cost_bps, default=0.0, metavar="BPS",
                        help="transaction cost per side of a trade, in basis points "
                             "(e.g. 15 = commission + stamp duty + slippage); reports "
                             "the net figures next to the gross ones (default 0 = gross)")
    parser.add_argument("--skip-limit-up", action="store_true",
                        help="do not buy a member that closed at its daily limit-up on "
                             "the entry day (10%% main board, 20%% ChiNext / STAR, "
                             "30%% Beijing); it is left out of that holding period")


def _days(value: str) -> float:
    """``--older-than``: a non-negative number of days."""
    try:
        days = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number of days") from None
    if not 0.0 <= days < float("inf"):
        raise argparse.ArgumentTypeError("--older-than must be 0 or more days")
    return days


def _gigabytes(value: str) -> float:
    """``--max-gb``: a positive size in GB."""
    try:
        size = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a size") from None
    if not 0.0 < size < float("inf"):
        raise argparse.ArgumentTypeError("--max-gb must be greater than 0")
    return size


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

    list_parser = sub.add_parser("list", help="list the presets: built-in and user")
    list_parser.set_defaults(func=cmd_list)

    init = sub.add_parser("init-preset",
                          help="write a documented custom preset, ready to edit")
    init.add_argument("name", help="the new preset's name (also its file name)")
    init.add_argument("--from", dest="copy_from", default=None, metavar="PRESET",
                      help="copy this preset (built-in, user or a path) instead of the "
                           "documented starting screen")
    init.add_argument("--out", default=None, metavar="PATH",
                      help="where to write it (default: the user preset directory, where "
                           "every command finds it by name)")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.set_defaults(func=cmd_init_preset)

    features = sub.add_parser("features", help="list registered features")
    features.add_argument("pattern", nargs="?", default=None,
                          help="substring filter (case-insensitive)")
    features.set_defaults(func=cmd_features)

    run = sub.add_parser("run", parents=[common], help="snapshot screen run")
    run.add_argument("preset", nargs="?", default=None,
                     help="a built-in preset or a YAML path (omit for an ad-hoc screen)")
    _screen_flags(run, explain=True, universe=True, top=True)
    _live_flag(run)
    _history_flag(run)
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
    diff.add_argument("--fail-on-change", action="store_true",
                      help="exit with code 2 when anything entered or exited (for cron)")
    _webhook_flags(diff)
    diff.set_defaults(func=cmd_diff)

    inspect = sub.add_parser("inspect", parents=[common],
                             help="one stock through every preset: pass/fail, why, scorecard")
    inspect.add_argument("symbol", help="e.g. SH600519")
    inspect.add_argument("--presets", nargs="+", default=None, metavar="NAME",
                         action=_NameList,
                         help="only these presets, space- or comma-separated "
                              "(default: every preset)")
    _screen_flags(inspect, formats=("table", "json", "markdown", "md"),
                  out="markdown file (--format markdown) or JSON (--format json)")
    inspect.set_defaults(func=cmd_inspect)

    report = sub.add_parser("report", parents=[common],
                            help="the daily briefing across presets (Markdown by default)")
    report.add_argument("--presets", nargs="+", default=None, metavar="NAME",
                        action=_NameList,
                        help="presets to cover, space- or comma-separated (default: "
                             "every preset)")
    report.add_argument("--top", type=int, default=10,
                        help="top picks per preset (default 10; 0 = all)")
    report.add_argument("--format", choices=("markdown", "md", "json", "html"),
                        default="markdown")
    _screen_flags(report, universe=True, formats=(),
                  out="briefing file (default: stdout); --format html writes a "
                      "self-contained page with SVG charts")
    _live_flag(report)
    _history_flag(report)
    _webhook_flags(report)
    report.set_defaults(func=cmd_report)

    archive = sub.add_parser("history", parents=[common],
                             help="how long each member has stayed in a preset's list")
    archive.add_argument("preset")
    archive.add_argument("--days", type=int, default=30, metavar="N",
                         help="count over the last N archived sessions (default 30)")
    archive.add_argument("--history-dir", default=None, metavar="DIR",
                         help=f"where the snapshots are (default {history_dir()}; "
                               f"${HISTORY_ENV})")
    archive.add_argument("--out", default=None,
                         help="the file to write (Markdown / JSON with --format; else a CSV "
                               "of the members)")
    archive.add_argument("--format", choices=("table", "markdown", "md", "json"),
                         default="table")
    archive.set_defaults(func=cmd_history)

    watch = sub.add_parser("watch", parents=[common],
                           help="risk alerts for the names you hold (the watchlist)")
    watch.add_argument("--symbols", default=None, metavar="A,B",
                       help="symbols to watch (comma-separated)")
    watch.add_argument("--watchlist", default=None, metavar="FILE",
                       help="a YAML/TXT watchlist: symbols, an optional target preset, "
                            "custom rules")
    watch.add_argument("--preset", default=None, metavar="NAME",
                       help="alert when a holding is no longer a member of this preset")
    _screen_flags(watch, formats=(), out="the report file: Markdown (--format markdown) "
                                         "or JSON (--format json); default: stdout")
    watch.add_argument("--format", choices=("table", "markdown", "md", "json"), default="table")
    watch.add_argument("--fail-on-alert", action="store_true",
                       help="exit with code 2 when any alert fires")
    _webhook_flags(watch)
    watch.set_defaults(func=cmd_watch)

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
    _execution_flags(val)
    _screen_flags(val, universe=True, top=True, formats=("table", "json", "html"),
                  out="per-date detail CSV (JSON with --format json, or the HTML report "
                      "file with --format html)")
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
    _execution_flags(cmp)
    _screen_flags(cmp, universe=True, top=True,
                  formats=("table", "json", "markdown", "md", "html"),
                  out="comparison table CSV (Markdown / JSON / the HTML report with "
                      "--format)")
    cmp.set_defaults(func=cmd_compare)

    status = sub.add_parser("data-status", parents=[common],
                            help="coverage / freshness census")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_data_status)

    cache = sub.add_parser("cache", help="inspect / clear / prune / warm the caches")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    # the common flags sit on the sub-commands: `qf-screen cache warm --data-dir X`
    c_status = cache_sub.add_parser("status", parents=[common],
                                    help="what is cached, how big, how fresh")
    c_status.add_argument("--json", action="store_true")
    c_status.set_defaults(func=cmd_cache)
    c_clear = cache_sub.add_parser("clear", parents=[common],
                                   help="delete cache entries, or prune them (--stale, "
                                        "--older-than, --max-gb); they rebuild on demand")
    c_clear.add_argument("--what", action="append", default=None, metavar="KIND",
                         help="cache kind to remove (repeatable; default all): "
                             + ", ".join(sorted(KIND_LABELS)))
    c_clear.add_argument("--dry-run", action="store_true",
                         help="only list what would go, with the reason")
    c_clear.add_argument("--stale", action="store_true",
                         help="remove only what no run will read again: other versions' "
                             "layouts, interrupted writes, merged-store caches built from a "
                             "source census that has since moved (and the tables of those "
                             "windows); the fresh daily window stays")
    c_clear.add_argument("--older-than", type=_days, default=None, metavar="DAYS",
                         help="also remove entries not used for DAYS (last use = the newest of "
                             "an entry's write and read time)")
    c_clear.add_argument("--max-gb", type=_gigabytes, default=None, metavar="N",
                         help="then remove the least recently used entries until the cache "
                             "is under N GB (1024^3 bytes, as `cache status` prints)")
    c_clear.set_defaults(func=cmd_cache)
    c_warm = cache_sub.add_parser("warm", parents=[common], help="pre-build the caches a daily run needs")
    c_warm.add_argument("--presets", nargs="+", default=None, metavar="NAME",
                        action=_NameList,
                        help="presets to warm, space- or comma-separated (default: "
                             "every preset)")
    c_warm.add_argument("--symbols", default=None,
                        help="restrict the universe (comma-separated)")
    c_warm.add_argument("--start", default=None,
                        help="also warm a schedule window (needs --end)")
    c_warm.add_argument("--end", default=None)
    c_warm.add_argument("--freq", default=None, choices=("W", "M", "Q"))
    c_warm.set_defaults(func=cmd_cache)

    # ---------------------------------------------- the symbol catalogs
    paths = argparse.ArgumentParser(add_help=False)
    paths.add_argument("--data-dir", default=None,
                       help="store root (default $QUANT_DATA_DIR)")

    symbol = sub.add_parser(
        "symbol", help="the stock & section catalogs: sync them, query them, "
        "curate custom sections (offline --industry / --concept)")
    sym = symbol.add_subparsers(dest="symbol_command", required=True)

    s_sync = sym.add_parser(
        "sync", parents=[paths],
        help="download the catalogs (network: akshare + GeoScope; `local` "
             "imports what is already on disk, offline)")
    s_sync.add_argument("--source", default="all",
                        choices=("ak", "lxg", "local", "all"),
                        help="which source(s): akshare 东财 boards (ak), lxg "
                             "company list + 申万 industries (lxg), the "
                             "offline on-disk import (local), or all = lxg+ak")
    s_sync.add_argument("--what", default="stocks,sections", metavar="A,B",
                        help="tables to sync: stocks, sections (default both)")
    s_sync.add_argument("--no-members", action="store_true",
                        help="list the sections without walking constituents")
    s_sync.add_argument("--pause", type=float, default=0.3, metavar="S",
                        help="seconds between the per-board akshare requests "
                             "(default 0.3)")
    s_sync.add_argument("--section-filter", default=None, metavar="NAMES",
                        help="only these 东财 board names (comma-separated)")
    s_sync.add_argument("--lxg-source", default="sw_2021",
                        choices=("sw_2021", "cni"),
                        help="lxg industry classification (default sw_2021)")
    s_sync.add_argument("--api-key", default=None, metavar="KEY",
                        help="GeoScope key (default $LIXINGER_API_KEY / "
                             "$GEOSCOPE_API_KEY)")
    s_sync.add_argument("--no-mirror", action="store_true",
                        help="do not rewrite cn/lxg/company_list.csv from the "
                             "lxg company download")
    s_sync.add_argument("--json", action="store_true",
                        help="print the structured result as JSON")
    s_sync.add_argument("--out", default=None, help="write the JSON here")
    s_sync.set_defaults(func=cmd_symbol_sync)

    s_stocks = sym.add_parser("stocks", parents=[paths],
                              help="the stock catalog (ak / lxg / custom)")
    s_stocks.add_argument("--query", "-q", default=None, metavar="TEXT",
                          help="substring of symbol, code or name")
    s_stocks.add_argument("--source", default=None,
                          choices=("ak", "lxg", "custom"))
    s_stocks.add_argument("--limit", type=int, default=None, metavar="N")
    s_stocks.add_argument("--format", choices=("table", "csv", "json"),
                          default="table")
    s_stocks.add_argument("--out", default=None)
    s_stocks.set_defaults(func=cmd_symbol_stocks)

    s_sections = sym.add_parser("sections", parents=[paths],
                                help="the section catalog (industries, "
                                     "concepts, your own)")
    s_sections.add_argument("--kind", default=None,
                            choices=("industry", "concept", "custom"))
    s_sections.add_argument("--source", default=None,
                            choices=("ak", "lxg", "custom"))
    s_sections.add_argument("--query", "-q", default=None, metavar="TEXT",
                            help="substring of the section name")
    s_sections.add_argument("--limit", type=int, default=None, metavar="N")
    s_sections.add_argument("--format", choices=("table", "csv", "json"),
                            default="table")
    s_sections.add_argument("--out", default=None)
    s_sections.set_defaults(func=cmd_symbol_sections)

    s_members = sym.add_parser("members", parents=[paths],
                               help="one section's constituents")
    s_members.add_argument("section", help="the section name (e.g. 白酒)")
    s_members.add_argument("--source", default=None,
                           choices=("ak", "lxg", "custom"))
    s_members.add_argument("--limit", type=int, default=None, metavar="N")
    s_members.add_argument("--format", choices=("table", "csv", "json"),
                           default="table")
    s_members.add_argument("--out", default=None)
    s_members.set_defaults(func=cmd_symbol_members)

    s_lookup = sym.add_parser("lookup", parents=[paths],
                              help="search stocks AND sections by text")
    s_lookup.add_argument("query", help="substring of a code, name or section")
    s_lookup.add_argument("--format", choices=("table", "json"),
                          default="table")
    s_lookup.add_argument("--out", default=None)
    s_lookup.set_defaults(func=cmd_symbol_lookup)

    s_add = sym.add_parser("add-section", parents=[paths],
                           help="create (or update) a custom section")
    s_add.add_argument("name", help="the section's name")
    s_add.add_argument("--kind", default="custom",
                       choices=("industry", "concept", "custom"),
                       help="which pool it extends (default custom: a plain "
                            "basket)")
    s_add.add_argument("--note", default=None, help="a free-text note")
    s_add.set_defaults(func=cmd_symbol_add_section)

    s_rm = sym.add_parser("remove-section", parents=[paths],
                          help="delete a custom section and its members")
    s_rm.add_argument("name", help="the section's name")
    s_rm.set_defaults(func=cmd_symbol_remove_section)

    s_addm = sym.add_parser("add-members", parents=[paths],
                            help="add stocks to a custom section")
    s_addm.add_argument("section", help="the custom section's name")
    s_addm.add_argument("symbols", metavar="A,B",
                        help="comma-separated symbols in any spelling")
    s_addm.set_defaults(func=cmd_symbol_add_members)

    s_rmm = sym.add_parser("remove-members", parents=[paths],
                           help="remove stocks from a custom section")
    s_rmm.add_argument("section", help="the custom section's name")
    s_rmm.add_argument("symbols", metavar="A,B",
                       help="comma-separated symbols in any spelling")
    s_rmm.set_defaults(func=cmd_symbol_remove_members)

    s_status = sym.add_parser("status", parents=[paths],
                              help="what is cataloged, how fresh")
    s_status.add_argument("--json", action="store_true")
    s_status.add_argument("--out", default=None,
                          help="write the JSON here")
    s_status.set_defaults(func=cmd_symbol_status)
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
    except (PresetError, LiveDataError, SymbolError) as exc:
        raise SystemExit(f"qf-screen: {exc}") from exc


if __name__ == "__main__":
    sys.exit(main())
