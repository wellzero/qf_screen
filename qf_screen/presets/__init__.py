"""Preset loader & registry.

A preset is a YAML file in this directory (or an absolute path)::

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
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import yaml

from qf_screen.rules import ExprFilter

#: the directory the built-in presets live in
PRESET_DIR = Path(__file__).resolve().parent

#: hygiene expressions every preset must start with (name -> expr).  The
#: listing-age floor is NOT here -- it is a universe knob
#: (``universe.min_listed_days``) because a preset may legitimately want a
#: different threshold.
REQUIRED_HYGIENE = (
    ("traded", "close_raw > 0"),
    ("price_floor", "close_raw >= 2"),
    ("liquidity", "amount_ma20 >= 5e7"),
)


class PresetError(ValueError):
    """A preset file that is not a valid screen."""


def list_presets() -> List[str]:
    """Names of the built-in presets (sorted)."""
    return sorted(p.stem for p in PRESET_DIR.glob("*.yaml"))


def preset_path(name_or_path: str) -> Path:
    """Resolve a preset name to its built-in file, or accept an absolute /
    relative path to a user preset."""
    candidate = Path(name_or_path)
    if candidate.suffix.lower() in (".yaml", ".yml") and candidate.exists():
        return candidate
    built_in = PRESET_DIR / f"{name_or_path}.yaml"
    if built_in.exists():
        return built_in
    if candidate.exists():
        return candidate
    raise PresetError(
        f"preset {name_or_path!r} not found (built-ins: {', '.join(list_presets())}; "
        "or pass a path to a *.yaml file)")


def load_preset(name_or_path: str) -> Dict:
    """Parse and validate one preset into a spec dict.

    Validation: ``name``, ``filters`` (a non-empty list of DSL boolean
    expressions) and the required hygiene block; unknown top-level keys are
    rejected so a typo'd knob cannot silently no-op (the same contract as
    qf_factor's screen settings).
    """
    path = preset_path(name_or_path)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PresetError(f"{path}: unreadable YAML ({exc})") from exc
    if not isinstance(doc, dict):
        raise PresetError(f"{path}: preset must be a mapping, got "
                          f"{type(doc).__name__}")
    known = {"name", "description", "universe", "filters", "rank", "top_n", "freq",
             "max_per_industry"}
    unknown = set(doc) - known
    if unknown:
        raise PresetError(f"{path}: unknown key(s) {sorted(unknown)} "
                          f"(known: {sorted(known)})")
    cap = doc.get("max_per_industry")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 1):
        raise PresetError(f"{path}: max_per_industry must be a positive integer, "
                          f"got {cap!r}")
    if not isinstance(doc.get("filters"), list) or not doc["filters"]:
        raise PresetError(f"{path}: 'filters' must be a non-empty list")
    for i, entry in enumerate(doc["filters"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("expr"), str):
            raise PresetError(f"{path}: filters[{i}] must be a mapping with an "
                              "'expr' string")
    doc.setdefault("name", path.stem)
    doc.setdefault("description", "")
    doc["filters"] = _with_hygiene(doc["filters"])
    doc["source"] = str(path)
    return doc


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
