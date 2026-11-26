"""Hygiene filters: tradability guards every screen starts with.

These are cheap (store tables only) and fail conservative: a name without
the data does not pass.
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from qf_screen.data.universe import resolve_boards

from .base import FeaturePanel, Filter, register_feature
from .dsl import compile_bool, expr_names


class ExprFilter(Filter):
    """A filter written as a DSL boolean expression over features.

    With ``na_pass=True`` a name passes when EVERY feature the expression
    mentions is missing for it (the rule explicitly declines to judge);
    with the default ``na_pass=False`` missing data fails the filter.
    Either way :meth:`evaluate` also reports WHICH names the rule could not
    judge, and the engine's funnel shows the count per rule.
    """

    def __init__(self, name: str, expr: str, description: str = "",
                 na_pass: bool = False):
        super().__init__(name, description or expr)
        self.expr = expr
        self.na_pass = bool(na_pass)
        self.features = expr_names(expr)
        unknown = [f for f in self.features
                   if f not in _known_features() and not _is_dynamic(f)]
        if unknown:
            from .base import feature_names
            raise ValueError(f"filter {name!r} references unknown feature(s) "
                             f"{unknown}; known: {', '.join(feature_names())} "
                             '(or factor_<family>__<column>)')
        self._fn = compile_bool(expr)

    def evaluate(self, panel: FeaturePanel, pos: int
                 ) -> Tuple[np.ndarray, Optional[np.ndarray]]:
        """``(passed, unknown)``.

        ``unknown`` is the mask of names for which EVERY feature the
        expression mentions is missing — the names the rule cannot judge
        (no lxg statements on disk, say).  With ``na_pass`` those names are
        added to the verdict; either way the funnel reports how many they
        are, so a screen that quietly steps aside for three quarters of the
        market says so.
        """
        env = panel.env(pos, self.features)
        out = self._fn(env).astype(bool)
        unknown: Optional[np.ndarray] = None
        for name in self.features:
            value = env.get(name)
            if value is None:
                continue
            if value.dtype == bool:            # a flag is never "missing"
                missing = np.zeros(len(out), dtype=bool)
            elif value.dtype == object:       # categorical: missing = ""
                missing = pd.isna(value) | (value == "")
            else:
                missing = ~np.isfinite(value)
            unknown = missing if unknown is None else (unknown & missing)
        if unknown is not None and self.na_pass:
            out = out | unknown
        return out, unknown

    def passes(self, panel: FeaturePanel, pos: int) -> np.ndarray:
        return self.evaluate(panel, pos)[0]

    def __repr__(self) -> str:
        return f"<ExprFilter {self.name}: {self.expr}>"


def _known_features() -> frozenset:
    from .base import FEATURES
    return frozenset(FEATURES)


def _is_dynamic(name: str) -> bool:
    from .base import is_dynamic_feature
    return is_dynamic_feature(name)


class BoardFilter(Filter):
    """Restrict to A-share boards (``sh_main`` ... or groups ``main`` /
    ``a_share`` / ``all``)."""

    def __init__(self, boards: Sequence[str] = ("all",)):
        self.boards = resolve_boards(boards)
        super().__init__("boards", "boards: " + "+".join(self.boards))

    def passes(self, panel: FeaturePanel, pos: int) -> np.ndarray:
        board = panel.table("board").iloc[:, 0].reindex(panel.symbols).to_numpy()
        return np.isin(board.astype(str), np.array(self.boards, dtype=object))


class SymbolSetFilter(Filter):
    """Restrict membership to an explicit symbol set (the universe-level
    ``symbols:`` knob, and the CLI's ``--industry`` / ``--concept`` board
    restriction).  Names outside the set fail THIS rule only — the funnel
    shows the restriction as its own step, and cross-sectional features
    still rank against the full loaded market."""

    def __init__(self, symbols, name: str = "symbols"):
        self.allowed = frozenset(symbols)
        label = f"{len(self.allowed)} names" if len(self.allowed) > 3 \
            else "+".join(sorted(self.allowed)) or "none"
        super().__init__(name, f"restricted to {label}")

    def passes(self, panel: FeaturePanel, pos: int) -> np.ndarray:
        return np.fromiter((s in self.allowed for s in panel.symbols),
                           dtype=bool, count=len(panel.symbols))


class NotSuspended(Filter):
    """Traded on the as-of session (a close exists)."""

    def __init__(self):
        super().__init__("traded", "has a close on the as-of session")

    def passes(self, panel: FeaturePanel, pos: int) -> np.ndarray:
        close = panel.store.wide("close_raw").iloc[pos].to_numpy(dtype=float)
        return np.isfinite(close) & (close > 0)


def register_hygiene_features() -> None:
    """Static cross-sectional features (board / name / industry / listing
    age) and the ST snapshot flag.

    ``st_flag`` is a TODAY list: True only from the snapshot's date FORWARD,
    never backdated.  Before that date the honest value is False (unknown),
    so a historical schedule does not exclude names that were fine then —
    the same forward-only rule qf_factor's names-snapshot enrichment
    applies.  For a pre-snapshot historical ST exclusion use the
    price-band detector via the engine's PIT options.
    """
    register_feature(
        "board", _static_column("board"), "listing board of the symbol (static)",
        kind="static", replace=True)
    register_feature(
        "name", _static_column("name"),
        "company name from the company-list snapshot (static)", kind="static")
    register_feature(
        "industry_l1", _static_column("industry_l1"),
        "申万 2021 first-level industry (static; blank when unclassified)",
        kind="static")
    register_feature(
        "industry_l2", _static_column("industry_l2"),
        "申万 2021 second-level industry (static)", kind="static")
    register_feature(
        "financial", _financial_flag,
        "1 for a bank / securities firm / insurer / other financial company "
        "per the company list (fsTableType), 0 for everyone else, NaN for a "
        "name the list does not carry (static)", kind="static")
    register_feature(
        "listed_days", _listed_days,
        "calendar days since listing (IPO date, else first stored bar)",
        kind="table")
    register_feature(
        "st_flag", _st_flag_table,
        "ST/*ST per the company-list TODAY snapshot -- True only from the "
        "snapshot date forward (never backdated)", kind="table")


def _static_column(column: str):
    """A census text column as a static feature.  A name the census does not
    classify stays MISSING (NaN in the object column) -- ``astype(str)``
    used to turn that into the text ``"nan"``, which then ranked as an
    industry of its own in ``industry_rank`` and counted against an
    industry cap."""
    def build(panel: FeaturePanel) -> pd.DataFrame:
        values = panel.store.universe.meta.get(column)
        if values is None:
            return pd.DataFrame({column: [None] * len(panel.symbols)},
                                index=panel.symbols)
        text = values.reindex(panel.symbols).astype(object)
        text = text.where(text.notna(), None).map(lambda v: v if isinstance(v, str) else v)
        return text.to_frame(column)
    return build


#: ``fsTableType`` values of the company list that mark a financial company
#: (whose balance sheet is levered by construction: a ratio rule written
#: for an industrial company says nothing about it)
_FINANCIAL_TYPES = frozenset({"bank", "security", "insurance", "other_financial"})


def _financial_flag(panel: FeaturePanel) -> pd.DataFrame:
    meta = panel.store.universe.meta
    if "fs_type" not in meta.columns:
        return pd.DataFrame({"financial": np.full(len(panel.symbols), np.nan)},
                            index=panel.symbols)
    kinds = meta["fs_type"].reindex(panel.symbols)
    flag = pd.Series(np.nan, index=panel.symbols, dtype=float)
    text = kinds.astype(object).where(kinds.notna(), None)
    is_text = text.map(lambda value: isinstance(value, str) and value.strip() != "")
    known = is_text[is_text].index
    flag.loc[known] = text.loc[known].map(
        lambda value: 1.0 if str(value).strip().lower() in _FINANCIAL_TYPES else 0.0)
    return flag.to_frame("financial")


def _listed_days(panel: FeaturePanel) -> pd.DataFrame:
    meta = panel.store.universe.meta
    list_date = pd.to_datetime(meta["list_date"]).reindex(panel.symbols) \
        if "list_date" in meta.columns else pd.Series(pd.NaT, index=panel.symbols)
    first_bar = panel.store.provenance["first_date"].reindex(panel.symbols)
    anchor = list_date.where(list_date.notna(), first_bar)
    offsets = (panel.calendar.to_numpy()[:, None]
               - anchor.to_numpy(dtype="datetime64[ns]")[None, :])
    # NaT anchors propagate through the cast to NaN (unknown listing age)
    days = offsets.astype("timedelta64[D]").astype(np.float64)
    return pd.DataFrame(days, index=panel.calendar, columns=panel.symbols)


def _st_flag_table(panel: FeaturePanel) -> pd.DataFrame:
    universe = panel.store.universe
    flag = pd.DataFrame(False, index=panel.calendar, columns=panel.symbols)
    named = [s for s in universe.st_named if s in flag.columns]
    if not named or universe.names_asof is None:
        return flag
    pos = int(panel.calendar.searchsorted(universe.names_asof))
    if pos < len(panel.calendar):
        flag.iloc[pos:, flag.columns.get_indexer(named)] = True
    return flag


# hygiene filter factories used by presets / the engine defaults
def price_floor(min_price: float = 2.0) -> Filter:
    return ExprFilter("price_floor", f"close_raw >= {min_price}",
                      f"unadjusted close >= {min_price} CNY")


def liquidity_floor(min_amount: float = 5e7) -> Filter:
    return ExprFilter("liquidity", f"amount_ma20 >= {min_amount:.4g}",
                      f"20d mean traded value >= {min_amount:.4g} CNY")


def listing_age(min_days: int = 365) -> Filter:
    return ExprFilter("listing_age", f"listed_days >= {min_days}",
                      f"listed at least {min_days} days")


def exclude_st() -> Filter:
    return ExprFilter("not_st", "not st_flag",
                      "not ST/*ST in the company-list snapshot "
                      "(forward-only; see the st_flag feature)")


def turnover_ceiling(max_turnover: float = 15.0) -> Filter:
    return ExprFilter("turnover_cap", f"turnover_ma20 <= {max_turnover}",
                      f"20d mean turnover <= {max_turnover}% (manipulation guard)")


register_hygiene_features()
