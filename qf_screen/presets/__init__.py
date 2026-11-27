"""Preset loader & registry: the built-in screens plus the user's own.

A preset is a YAML file in this directory, or one directory deep in a
family folder (``undervalue_tailwind/`` — named by its file stem either
way), or an absolute path::

    name: value_dividend
    description: cheap, profitable, paying
    universe:
      boards: [main]          # sh_main/sz_main/chinext/star/bj, or groups
      exclude_st: true        # drop ST/*ST per the company-list snapshot
      min_listed_days: 365
    filters:                  # ordered funnel; expressions are DSL boolean
      - {name: price,  expr: "close_raw >= 2"}
      - {name: liquid, expr: "amount_ma20 >= 5e7"}
      - {name: value,  expr: "pe_ttm > 0 and pe_ttm < 15"}
      - {name: quality, expr: "roe > 0.12 and debt_assets < 0.6"}
      - {name: pay,    expr: "div_yield_ttm >= 0.04"}
    rank:                     # higher = better; weighted percentile blend
      - {expr: "pct_rank(earnings_yield)", weight: 1.0}
      - {expr: "pct_rank(div_yield_ttm)", weight: 1.0}
    top_n: 30
    max_per_industry: 5      # optional diversity cap: at most 5 names per industry

Every preset must start from the shared hygiene block (traded, price
floor, liquidity floor) — :func:`load_preset` enforces it by prepending
any that are missing, so a preset cannot silently screen penny or
untradeable names.

A filter may carry ``na_pass: true``: names for which EVERY feature the
expression mentions is missing (no lxg statements on disk, say) then pass
instead of fail — "no KNOWN problem" rather than "known fine".  The funnel
reports how many names each rule could not judge, so the choice is visible
in every run.

**User presets.**  Beside the built-ins, every ``*.yaml`` / ``*.yml`` in
``$QF_SCREEN_PRESETS_DIR`` (default ``~/.config/qf_screen/presets/``) is a
preset named by its file stem, discovered by every command that takes a
preset name — ``run``, ``diff``, ``inspect``, ``report``, ``pit``,
``validate``, ``compare``, ``cache warm`` — and listed by ``qf-screen
list``.  A user file named like a built-in (``value_dividend.yaml``)
REPLACES it; any other name extends the list.  ``qf-screen init-preset
NAME [--from PRESET]`` writes a documented starting file there.  Nothing
is installed or copied into the package: the directory is the registry.

Validation rejects what would otherwise fail late or, worse, silently
no-op in a hand-edited file: unknown keys at the top level, inside
``universe`` and inside each ``filter`` / ``rank`` entry, a ``na_pass``
that is not a boolean, a rank entry without an expression or with a
non-positive weight, a board no screener knows.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from qf_screen.rules import ExprFilter

#: the directory the built-in presets live in
PRESET_DIR = Path(__file__).resolve().parent

#: environment variable naming the user preset directory
USER_PRESETS_ENV = "QF_SCREEN_PRESETS_DIR"
_SUFFIXES = (".yaml", ".yml")

#: hygiene expressions every preset must start with (name -> expr).  The
#: listing-age floor is NOT here -- it is a universe knob
#: (``universe.min_listed_days``) because a preset may legitimately want a
#: different threshold.
REQUIRED_HYGIENE = (
    ("traded", "close_raw > 0"),
    ("price_floor", "close_raw >= 2"),
    ("liquidity", "amount_ma20 >= 5e7"),
)

#: the keys each level of a preset file may carry
_TOP_KEYS = frozenset({"name", "description", "universe", "filters", "rank", "top_n", "freq",
                       "max_per_industry"})
_UNIVERSE_KEYS = frozenset({"boards", "symbols", "exclude_st", "min_listed_days", "st_band"})
_FILTER_KEYS = frozenset({"name", "expr", "description", "na_pass"})
_RANK_KEYS = frozenset({"expr", "weight"})


class PresetError(ValueError):
    """A preset file that is not a valid screen."""


# ----------------------------------------------------------- the registry
def user_preset_dir() -> Path:
    """Where user presets are discovered: ``$QF_SCREEN_PRESETS_DIR``, else
    ``$XDG_CONFIG_HOME/qf_screen/presets/`` (default
    ``~/.config/qf_screen/presets/``)."""
    raw = (os.environ.get(USER_PRESETS_ENV) or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(config) / "qf_screen" / "presets"


def user_preset_files() -> Dict[str, Path]:
    """``{preset name: file}`` of the user directory (file stem = name)."""
    found: Dict[str, Path] = {}
    try:
        entries = sorted(os.scandir(user_preset_dir()), key=lambda entry: entry.name)
    except OSError:
        return found
    for entry in entries:
        path = Path(entry.path)
        if entry.is_file() and path.suffix.lower() in _SUFFIXES and path.stem:
            found.setdefault(path.stem, path)
    return found


def _built_in_files() -> Dict[str, Path]:
    """``{preset name: file}`` of the shipped presets: every ``*.yaml``
    directly in :data:`PRESET_DIR` plus one directory deep (family
    folders, e.g. ``undervalue_tailwind/``); a preset is named by its
    file stem either way, and a top-level file wins a stem collision
    with a folder member."""
    found: Dict[str, Path] = {}
    for pattern in ("*.yaml", "*/*.yaml"):
        for path in sorted(PRESET_DIR.glob(pattern)):
            if path.is_file() and path.stem:
                found.setdefault(path.stem, path)
    return found


def built_in_presets() -> List[str]:
    """Names of the presets shipped with the package (sorted)."""
    return sorted(_built_in_files())


def list_presets() -> List[str]:
    """Every preset a command may name: the built-ins plus the user's, a
    user file overriding the built-in of the same name (sorted)."""
    names = set(built_in_presets()) | set(user_preset_files())
    return sorted(names)


def preset_origin(name: str) -> Optional[Dict[str, object]]:
    """Where the preset ``name`` resolves: ``{"kind": ..., "path": ...}``
    with kind ``"built-in"``, ``"user"`` or ``"override"`` (a user preset
    that replaces a built-in) — ``None`` when no preset has that name."""
    user = user_preset_files().get(str(name))
    if user is not None:
        overridden = str(name) in _built_in_files()
        return {"kind": "override" if overridden else "user", "path": user}
    built_in = _built_in_files().get(str(name))
    return {"kind": "built-in", "path": built_in} if built_in is not None else None


def preset_path(name_or_path: str) -> Path:
    """Resolve a preset name to its file — a user preset wins over the
    built-in of the same name — or accept an absolute / relative path to a
    preset file."""
    candidate = Path(name_or_path)
    if candidate.suffix.lower() in _SUFFIXES and candidate.exists():
        return candidate
    user = user_preset_files().get(str(name_or_path))
    if user is not None:
        return user
    built_in = _built_in_files().get(str(name_or_path))
    if built_in is not None:
        return built_in
    if candidate.exists():
        return candidate
    raise PresetError(
        f"preset {name_or_path!r} not found (built-ins: "
        f"{', '.join(built_in_presets())}; user presets in {user_preset_dir()}: "
        f"{', '.join(sorted(set(user_preset_files()) - set(built_in_presets()))) or 'none'}; "
        "or pass a path to a *.yaml file)")


# -------------------------------------------------------------- loading
def read_preset(path) -> Dict:
    """The preset file as written: parsed and validated, but with no
    hygiene prepended and no ``source`` added (what ``init-preset --from``
    copies)."""
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PresetError(f"{path}: unreadable YAML ({exc})") from exc
    if not isinstance(doc, dict):
        raise PresetError(f"{path}: preset must be a mapping, got {type(doc).__name__}")
    _validate(doc, path)
    return doc


def load_preset(name_or_path: str) -> Dict:
    """Parse and validate one preset into a spec dict.

    Validation: ``name``, ``filters`` (a non-empty list of DSL boolean
    expressions) and the required hygiene block; unknown keys are rejected
    at every level so a typo'd knob cannot silently no-op (the same
    contract as qf_factor's screen settings).
    """
    path = preset_path(name_or_path)
    doc = read_preset(path)
    doc.setdefault("name", path.stem)
    doc.setdefault("description", "")
    doc["filters"] = _with_hygiene(doc["filters"])
    doc["source"] = str(path)
    return doc


def _keys(where: str, allowed: frozenset, mapping, path) -> None:
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    if unknown:
        raise PresetError(f"{path}: unknown key(s) {unknown} in {where} "
                          f"(known: {', '.join(sorted(allowed))})")


def _validate(doc: Dict, path) -> None:
    """Every structural check, with the message naming the offending key."""
    _keys("the preset", _TOP_KEYS, doc, path)
    cap = doc.get("max_per_industry")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 1):
        raise PresetError(f"{path}: max_per_industry must be a positive integer, got {cap!r}")
    top = doc.get("top_n")
    if top is not None and (isinstance(top, bool) or not isinstance(top, int) or top < 0):
        raise PresetError(f"{path}: top_n must be 0 or a positive integer, got {top!r}")
    universe = doc.get("universe")
    if universe is None:
        universe = {}
    if not isinstance(universe, dict):
        raise PresetError(f"{path}: 'universe' must be a mapping, got "
                          f"{type(universe).__name__}")
    _keys("universe", _UNIVERSE_KEYS, universe, path)
    age = universe.get("min_listed_days")
    if age is not None and (isinstance(age, bool) or not isinstance(age, int) or age < 0):
        raise PresetError(f"{path}: universe.min_listed_days must be 0 or a positive "
                          f"integer, got {age!r}")
    boards = universe.get("boards")
    if boards is not None:
        from qf_screen.data.universe import resolve_boards

        if isinstance(boards, str) or not isinstance(boards, (list, tuple)):
            raise PresetError(f"{path}: universe.boards must be a list, got {boards!r}")
        try:
            resolve_boards([str(board) for board in boards])
        except ValueError as exc:
            raise PresetError(f"{path}: {exc}") from exc
    symbols = universe.get("symbols")
    if symbols is not None and (not isinstance(symbols, (list, tuple))
                                or not all(isinstance(s, str) for s in symbols)):
        raise PresetError(f"{path}: universe.symbols must be a list of symbol strings")

    if not isinstance(doc.get("filters"), list) or not doc["filters"]:
        raise PresetError(f"{path}: 'filters' must be a non-empty list")
    for i, entry in enumerate(doc["filters"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("expr"), str):
            raise PresetError(f"{path}: filters[{i}] must be a mapping with an "
                              f"'expr' string")
        _keys(f"filters[{i}]", _FILTER_KEYS, entry, path)
        na_pass = entry.get("na_pass", False)
        if not isinstance(na_pass, bool):
            raise PresetError(f"{path}: filters[{i}].na_pass must be true or false, "
                              f"got {na_pass!r}")
    rank = doc.get("rank")
    if rank is None:
        rank = []
    if not isinstance(rank, list):
        raise PresetError(f"{path}: 'rank' must be a list")
    for i, entry in enumerate(rank):
        if not isinstance(entry, dict) or not isinstance(entry.get("expr"), str):
            raise PresetError(f"{path}: rank[{i}] must be a mapping with an 'expr' string")
        _keys(f"rank[{i}]", _RANK_KEYS, entry, path)
        weight = entry.get("weight", 1.0)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
            raise PresetError(f"{path}: rank[{i}].weight must be a positive number, "
                              f"got {weight!r}")


def _with_hygiene(filters: List[Dict]) -> List[Dict]:
    """Prepend any required hygiene filter the preset does not already
    carry (matched by its expression, so a preset may tighten one by
    including it itself)."""
    present = {str(f.get("expr", "")).replace(" ", "") for f in filters}
    missing = [name for name, expr in REQUIRED_HYGIENE
               if expr.replace(" ", "") not in present]
    out = list(filters)
    for name in reversed(missing):
        expr = next(e for n, e in REQUIRED_HYGIENE if n == name)
        out.insert(0, {"name": name, "expr": expr, "auto": True})
    return out


def validate_filters(filters: List[Dict]) -> None:
    """Compile every filter expression (raises on unknown features or
    non-boolean expressions) — used by the engine at spec-build time."""
    for entry in filters:
        ExprFilter(str(entry.get("name", "filter")), str(entry["expr"]),
                   na_pass=bool(entry.get("na_pass", False)))


# ------------------------------------------------------------ init-preset
_NAME_RE = re.compile(r"[\w][\w.-]*")

#: the documented starting screen ``init-preset`` writes without ``--from``
STARTING_SCREEN: Dict[str, object] = {
    "description": "One line on what this screen looks for (shown by `qf-screen list`).",
    "universe": {"boards": ["main"], "exclude_st": True, "min_listed_days": 365},
    "filters": [
        {"name": "liquid", "expr": "amount_ma20 >= 1e8"},
        {"name": "value", "expr": "pe_ttm > 0 and pe_ttm < 15"},
        {"name": "balance", "expr": "debt_assets < 0.6", "na_pass": True}],
    "rank": [{"expr": "pct_rank(earnings_yield)", "weight": 1.0}],
    "top_n": 30,
}


def _scalar(value) -> str:
    """One YAML scalar: text quoted the JSON way (valid YAML, whatever the
    expression contains), numbers and booleans plain."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _fold(text: str, width: int = 72, indent: str = "  ") -> str:
    """``text`` as the indented body of a ``>`` block scalar, one paragraph
    wrapped at ``width`` characters."""
    lines, line = [], ""
    for word in str(text).split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return "\n".join(indent + item for item in lines + [line])


def _row(code: str, comment: str = "", column: int = 27) -> str:
    """A YAML line with its explanation aligned behind it."""
    return f"{code:<{column}}  # {comment}" if comment else code


def preset_text(name: str, doc: Optional[Dict] = None) -> str:
    """A preset as YAML with every key documented: ``doc`` when given (a
    copy of another preset), else the starting screen."""
    doc = dict(doc) if doc is not None else dict(STARTING_SCREEN)
    universe = dict(doc.get("universe") or {})
    filters = list(doc.get("filters") or [])
    rank = list(doc.get("rank") or [])
    boards = universe.get("boards") or ["main"]
    top = 30 if doc.get("top_n") is None else doc["top_n"]
    out = [
        "# qf_screen preset -- ONE screen: universe + ordered filters + a ranking.",
        "#",
        f"#   qf-screen run {name} --top 10 --explain SH600519    today's members, rule by rule",
        f"#   qf-screen validate {name} --start 2026-01-05 --end 2026-09-30 --cost-bps 15",
        "#   qf-screen list                                         built-in and user presets",
        "#",
        "# `qf-screen features` lists every feature these expressions can read; the",
        "# README covers the expression DSL.  Unknown keys are rejected when the file",
        "# loads, so a typo cannot silently switch a knob off.  A file named like a",
        "# built-in preset (value_dividend.yaml ...) REPLACES that preset.",
        "",
        f"name: {name}",
        "description: >",
        _fold(str(doc.get("description") or "").strip() or "What this screen looks for."),
        "",
        _row("universe:", "who may enter at all, before any filter"),
        _row("  boards: [" + ", ".join(str(board) for board in boards) + "]",
             "sh_main, sz_main, chinext, star, bj; groups: main, a_share, all"),
        _row(f"  exclude_st: {_scalar(universe.get('exclude_st', True))}",
             "drop ST/*ST names (company-list snapshot, forward-only)"),
        _row(f"  min_listed_days: {_scalar(universe.get('min_listed_days', 365))}",
             "listing-age floor; delete the line for none"),
        _row("  st_band: true" if universe.get("st_band") else "  # st_band: true",
             "opt-in: historical ST state from the price-band detector (~20 s, built once)"),
    ]
    symbols = universe.get("symbols")
    out.append(_row("  symbols: [" + ", ".join(_scalar(item) for item in symbols) + "]"
                    if symbols else "  # symbols: [SH600519, SZ000858]",
                    "an explicit list instead of boards"))
    out += [
        "",
        _row("filters:", "the ordered funnel; every expression must be boolean"),
        "  # The shared hygiene block (traded, close_raw >= 2, amount_ma20 >= 5e7) is",
        "  # prepended automatically -- repeat a line here only to TIGHTEN it.",
    ]
    for entry in filters:
        code = (f"  - {{name: {_scalar(entry.get('name', 'filter'))}, "
                f"expr: {_scalar(entry['expr'])}")
        out.append(code + (", na_pass: true}" if entry.get("na_pass") else "}"))
    out += [
        '  # A missing value FAILS a rule; "na_pass: true" on it lets the names it',
        '  # cannot judge through (a balance rule where no statements exist, say).',
        "",
        _row("rank:", "higher is better: a weighted blend of percentile ranks"),
    ]
    out += [f"  - {{expr: {_scalar(entry['expr'])}, "
            f"weight: {_scalar(entry.get('weight', 1.0))}}}" for entry in rank]
    out += [
        '  # - {expr: "industry_rank(div_yield_ttm)", weight: 0.5}',
        "",
        _row(f"top_n: {_scalar(top)}", "members kept per run (0 = no limit)"),
        _row(f"max_per_industry: {int(doc['max_per_industry'])}"
             if doc.get("max_per_industry") else "# max_per_industry: 5",
             "at most N names per industry; the next-best fill the slots"),
    ]
    return "\n".join(out) + "\n"


def init_preset(name: str, base: Optional[str] = None, out=None, force: bool = False
                ) -> Tuple[Path, List[str]]:
    """Write a documented preset file for ``name`` and return
    ``(path, notes)``.

    Without ``base`` the file is the starting screen; with it a copy of
    that preset (built-in, user or a path) under the new name.  The file
    lands in the user preset directory (so every command finds it by name)
    unless ``out`` names another path, and is never overwritten without
    ``force``.  What was written is loaded back through the real loader
    before the function returns, so a scaffold always compiles.
    """
    name = str(name).strip()
    for suffix in _SUFFIXES:                        # `init-preset my.yaml` -> my
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    if not _NAME_RE.fullmatch(name):
        raise PresetError(
            f"{name!r} is not a preset name: letters, digits, '_', '-' and '.' only "
            "(the name is what you type after `qf-screen run`)")
    doc = read_preset(preset_path(base)) if base else None
    notes: List[str] = []
    if name in _built_in_files():
        notes.append(f"a user preset named {name!r} REPLACES the built-in "
                     f"{name} in every command; delete the file to get it back")
    target = Path(out).expanduser() if out else user_preset_dir() / f"{name}.yaml"
    if PRESET_DIR in target.resolve().parents:
        raise PresetError("the built-in presets are part of the package -- write the "
                          f"override to {user_preset_dir() / target.name} instead")
    if target.exists() and not force:
        raise PresetError(f"{target} already exists -- pass --force to overwrite it")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(preset_text(name, doc), encoding="utf-8")
    if out and target.stem != name:
        notes.append(f"the file is listed and run as {target.stem!r} (its file name), "
                     f"not {name!r}: keep the two the same outside the preset directory")
    from qf_screen.engine import ScreenSpec

    ScreenSpec.from_yaml(str(target))               # the scaffold must compile
    return target, notes


__all__ = ["PRESET_DIR", "REQUIRED_HYGIENE", "USER_PRESETS_ENV", "PresetError",
           "built_in_presets", "init_preset", "list_presets", "load_preset", "preset_origin",
           "preset_path", "preset_text", "read_preset", "user_preset_dir",
           "user_preset_files", "validate_filters"]
