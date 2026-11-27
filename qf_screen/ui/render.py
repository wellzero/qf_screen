"""Human-facing formatting shared by the CLI views: feature units, text
tables, Markdown tables.  Rendering only — nothing here reads data.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

#: feature-name patterns -> human display units.  Yields / returns /
#: ROE-ish fractions print as %; CNY levels print as 亿元.
_PCT_HINTS = ("yield", "ret_", "mom_", "roe", "roa", "growth", "margin",
              "_chg", "chg_", "ratio", "high252", "vol_", "max21", "score",
              "turnover", "debt_assets", "cash_assets", "pledge", "discount",
              "f_score", "survival")
_CNY_HINTS = ("market_cap", "amount")
#: statement amounts in CNY (the lxg bridge's canonical fields)
_CNY_NAMES = frozenset({"ocf_ttm", "capex_ttm", "fcf_ttm", "revenue_ttm", "profit_ttm",
                        "assets", "liabilities", "equity", "interest_debt", "cash"})

#: features stored in PERCENT POINTS (``15.0`` == 15 %).  Every other
#: percentage-like feature is a FRACTION (``0.15`` == 15 %), which is what
#: the ``_PCT_HINTS`` patterns assume -- matching these by substring
#: printed a 1.4 % turnover rate as "138.1%".
_PCT_POINT_NAMES = frozenset({"turnoverrate", "turnover_ma20", "turnover_ma60"})
#: fraction-valued features whose names carry none of the ``_PCT_HINTS``
_FRACTION_NAMES = frozenset({"dyr"})
#: plain numbers that would otherwise catch a substring hint by accident
_FLAT_NAMES = frozenset({"pct_rank", "rank", "rank_prev", "rank_now", "change",
                         "covered", "members", "sessions", "rebalances",
                         "hit_rate", "dates"})


def _is_missing(value) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, float):
        return not np.isfinite(value)
    try:
        return bool(pd.isna(value)) if not isinstance(value, str) else False
    except (TypeError, ValueError):
        return False


def fmt_value(name: str, value) -> str:
    """One feature value in its natural human unit.

    An explicit unit SUFFIX on the name wins over the substring hints:
    ``amount_ma20_mn`` is ALREADY in 百万元, and letting the ``amount`` in
    its name reach the CNY rule scaled it down a second time ("0.0亿").
    """
    if _is_missing(value):
        return "-"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return f"{value:,}"                      # a count or a rank: no decimals
    low = str(name).lower()
    if low in _PCT_POINT_NAMES:
        return f"{value:,.2f}%"
    if low.endswith("_bn"):
        return f"{value:,.1f}亿"
    if low.endswith("_mn"):
        return f"{value:,.0f}百万"
    if low in _FLAT_NAMES:
        return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.3f}"
    if low in _CNY_NAMES or any(h in low for h in _CNY_HINTS):
        return f"{value / 1e8:,.1f}亿"
    if low in _FRACTION_NAMES or any(h in low for h in _PCT_HINTS):
        return f"{value:.1%}"
    if abs(value) >= 1000:
        return f"{value:,.1f}"
    return f"{value:,.3g}"


#: the name the tests and the older CLI import
_fmt_value = fmt_value


def _cell(value, name) -> str:
    """One table cell: ``-`` for a missing value, text as it is, a number
    in its feature's unit."""
    if _is_missing(value):
        return "-"
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float, np.integer, np.floating)):
        return str(value) if isinstance(value, bool) else fmt_value(str(name), value)
    return str(value)


#: the public name of the cell formatter (the HTML report builds its
#: table cells with it too)
cell_text = _cell


def fmt_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Result table with unit-aware cell formatting (numeric and
    all-NaN-object columns alike — an all-NaN pandas column arrives as
    object dtype holding ``None``).  The ``symbol`` column is never
    touched."""
    out = frame.copy()
    for column in out.columns:
        kind = out[column].dtype.kind
        if kind in "fc" or (kind == "O" and column != "symbol"):
            out[column] = [_cell(value, column) for value in out[column]]
    return out


def frame_text(frame: pd.DataFrame, limit: Optional[int] = None) -> str:
    """A result table as terminal text (unit-aware cells)."""
    shown = frame if limit is None or len(frame) <= limit else frame.head(limit)
    with pd.option_context("display.width", 220, "display.max_columns", 60):
        text = fmt_frame(shown).to_string(index=False)
    if limit is not None and len(frame) > limit:
        text += f"\n  ... {len(frame) - limit} more (--top {len(frame)} shows all)"
    return text


def markdown_table(frame: pd.DataFrame) -> str:
    """A result table as a GitHub-Flavored Markdown table (unit-aware)."""
    if frame.empty:
        return "_(empty)_"
    names = [str(c) for c in frame.columns]
    header = "| " + " | ".join(names) + " |"
    sep = "|" + "|".join("---" for _ in names) + "|"
    rows = []
    for values in frame.itertuples(index=False, name=None):
        rows.append("| " + " | ".join(_cell(v, name) for v, name in zip(values, names))
                    + " |")
    return "\n".join([header, sep, *rows])


def pct(value, digits: int = 1, signed: bool = False) -> str:
    """A fraction as a percentage; ``n/a`` for a missing value."""
    if value is None or not np.isfinite(value):
        return "n/a"
    return f"{value:+.{digits}%}" if signed else f"{value:.{digits}%}"


def num(value, digits: int = 2) -> str:
    """A plain number; ``n/a`` for a missing value."""
    if value is None or not np.isfinite(value):
        return "n/a"
    return f"{value:.{digits}f}"


def human_bytes(size: float) -> str:
    """``1.2 GB`` / ``220 MB`` / ``0.3 MB``."""
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} TB"


def industry_line(mix: pd.Series, top: int = 5) -> str:
    """An industry breakdown as one line: the ``top`` industries, then the
    rest as ``其他``, unclassified names last."""
    if mix is None or len(mix) == 0:
        return "no members"
    total = float(mix.sum())
    leading = list(mix.items())[:top]
    tail = [(label, value) for label, value in list(mix.items())[top:]
            if label != "未分类"]
    unclassified = [(label, value) for label, value in mix.items() if label == "未分类"]
    parts = [(label, float(value)) for label, value in leading if label != "未分类"]
    rest = sum(value for _label, value in tail)
    parts += [("其他", rest)] if rest else []
    parts += unclassified
    return " · ".join(f"{label} {int(value)} ({value / total:.0%})"
                      for label, value in parts if value)


__all__ = [
    "cell_text", "fmt_frame", "fmt_value", "frame_text", "human_bytes", "industry_line",
    "markdown_table", "num", "pct",
]
