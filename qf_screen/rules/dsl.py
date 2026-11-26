"""The safe expression language rules and presets are written in.

An expression is compiled ONCE with :func:`compile_expr` into a Python
closure that evaluates vectorized over a whole cross-section (an env of
``{feature_name: ndarray}``) — there is no ``eval``, no attribute access,
no subscripting and no name that is not on a whitelist.  Compilation walks
the AST and only accepts the node kinds a cross-sectional screen can
legitimately need::

    pe_ttm > 0 and pe_ttm < 15 and roe > 0.15
    pct_rank(earnings_yield) + 0.5 * industry_rank(div_yield_ttm)
    not (max21 > 0.05) and turnover_ma20 < 8

Semantics:

* arithmetic / comparisons are numpy element-wise; any NaN operand makes
  that name's comparison False (conservative NA, see :mod:`.base`);
* ``not`` inverts that literally — ``not (pe_ttm > 15)`` PASSES a name
  whose P/E is unknown (unknown is not greater than 15).  Write
  ``pe_ttm > 0 and pe_ttm < 15`` when unknown must stay out;
* ``and`` / ``or`` operate on boolean arrays (Kleene semantics are NOT
  used — the operands of a boolean operator must already be boolean, which
  a comparison produces);
* the function whitelist is cross-sectional (``rank``, ``pct_rank``,
  ``zscore``, ``industry_rank``) plus scalar math (``abs``, ``clip``,
  ``fillna``, ``log``, ``sqrt``, ``sign``, ``minimum``, ``maximum``);
* unknown names, calls to anything else, attributes, subscripts, lambdas,
  comprehensions ... are rejected at COMPILE time with :class:`DslError`.
"""
from __future__ import annotations

import ast
import math
from typing import Callable, Dict, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

__all__ = ["DslError", "compile_expr", "DSL_FUNCTIONS", "allowed_functions"]


class DslError(ValueError):
    """An expression the DSL refuses to compile (unsafe node, unknown
    name, wrong arity ...) or cannot evaluate (missing feature)."""


# ------------------------------------------------------- vector functions
def _rank(values: np.ndarray) -> np.ndarray:
    """Cross-sectional rank (1 = smallest), NaN excluded."""
    return pd.Series(values).rank().to_numpy()


def _pct_rank(values: np.ndarray) -> np.ndarray:
    """Cross-sectional percentile rank in [0, 1] (1.0 = largest), NaN
    excluded."""
    return pd.Series(values).rank(pct=True).to_numpy()


def _zscore(values: np.ndarray) -> np.ndarray:
    """Cross-sectional z-score; NaN excluded from mean/std and stays NaN."""
    ok = np.isfinite(values)
    out = np.full(values.shape, np.nan)
    if ok.sum() >= 2:
        std = values[ok].std()
        if std > 0:
            out[ok] = (values[ok] - values[ok].mean()) / std
    return out


def _industry_rank(values: np.ndarray, industry: Optional[np.ndarray] = None,
                   ascending: bool = True) -> np.ndarray:
    """Percentile rank of ``values`` within each industry group (same
    orientation as :func:`_pct_rank`: largest value -> ~1.0 when
    ``ascending``).

    ``industry`` is the parallel categorical array (the DSL binds it from
    the env's ``_industry`` key); names without an industry or a value get
    NaN — they fail an industry-relative filter conservatively rather than
    being ranked globally.
    """
    out = np.full(values.shape, np.nan)
    if industry is None:
        return out
    ok = np.isfinite(values)
    for group in pd.unique(industry[ok]):
        mask = ok & (industry == group)
        if mask.sum() >= 2:
            out[mask] = pd.Series(values[mask]).rank(pct=True,
                                                     ascending=ascending).to_numpy()
    return out


def _log(values: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.log(np.where(values > 0, values, np.nan))
    return out.astype(float)


def _sqrt(values: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return np.sqrt(np.where(values >= 0, values, np.nan)).astype(float)


def _sign(values: np.ndarray) -> np.ndarray:
    return np.sign(np.nan_to_num(values, nan=0.0))


def _clip(values: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip(values, lo, hi)


def _fillna(values: np.ndarray, value: float) -> np.ndarray:
    out = np.array(values, dtype=float, copy=True)
    out[~np.isfinite(out)] = value
    return out


def _minimum(a: np.ndarray, b) -> np.ndarray:
    return np.minimum(a, b)


def _maximum(a: np.ndarray, b) -> np.ndarray:
    return np.maximum(a, b)


#: name -> (function, arity, "cross-sectional rank helper that needs the
#: industry/group array bound from the env")
DSL_FUNCTIONS: Dict[str, Callable] = {
    "rank": _rank,
    "pct_rank": _pct_rank,
    "zscore": _zscore,
    "industry_rank": _industry_rank,
    "abs": np.abs,
    "clip": _clip,
    "fillna": _fillna,
    "log": _log,
    "sqrt": _sqrt,
    "sign": _sign,
    "minimum": _minimum,
    "maximum": _maximum,
    # numeric constants screens commonly threshold against
    "pi": math.pi,
    "e": math.e,
    # the qf_factor CSV bridge — NOT a value function: visit_Call compiles
    # factor("<family>", "<column>") into a dynamic feature lookup
    # (factor_<family>__<column>), so it needs string-literal arguments
    "factor": None,
}

#: dynamic feature prefix the ``factor(...)`` call compiles into
_FACTOR_PREFIX = "factor_"


def factor_feature_name(family: str, column: str) -> str:
    """``("finance_Q_factor", "roe") -> "factor_finance_Q_factor__roe"``."""
    if "__" in column:
        raise DslError(f"factor column {column!r} must not contain '__'")
    return f"{_FACTOR_PREFIX}{family}__{column}"


def allowed_functions() -> Sequence[str]:
    """The callable names an expression may use."""
    return sorted(DSL_FUNCTIONS)


#: names that ride in the env but are not features (underscore prefix is
#  reserved for them)
_RESERVED = frozenset({"_industry"})

# numeric leaf types accepted as constants
_NUM_CONST = (int, float, bool)


# ---------------------------------------------------------------- compile
class _Compiler(ast.NodeVisitor):
    """AST -> closure.  Each ``visit_*`` returns a function ``env -> value``
    (``value`` = ndarray or scalar); anything not whitelisted raises."""

    def __init__(self, expression: str):
        self.expression = expression

    def fail(self, node: ast.AST, why: str) -> None:
        raise DslError(f"{self.expression!r}: {why} (line {getattr(node, 'lineno', '?')})")

    def _numeric(self, value) -> np.ndarray:
        """An operand as a float array; a bare function name (``rank + 1``)
        or other non-numeric leaf is a compile-time-style error, not a
        TypeError from inside numpy."""
        if callable(value) or isinstance(value, bool):
            raise DslError(f"{self.expression!r}: operand must be numeric -- a "
                           "function name is not a value")
        try:
            return np.asarray(value, dtype=float)
        except (TypeError, ValueError) as exc:
            raise DslError(f"{self.expression!r}: operand is not numeric "
                           f"({exc})") from exc

    # ------------------------------------------------------------ leaves
    def visit_Expression(self, node: ast.Expression) -> Callable:
        inner = self.visit(node.body)
        return inner

    def visit_Name(self, node: ast.Name) -> Callable:
        if node.id in _RESERVED:
            self.fail(node, f"{node.id!r} is reserved for the engine")
        if node.id in DSL_FUNCTIONS:
            fn = DSL_FUNCTIONS[node.id]
            return lambda env: fn
        return lambda env, name=node.id: self._feature(env, name)

    def _feature(self, env: Mapping[str, np.ndarray], name: str) -> np.ndarray:
        try:
            return env[name]
        except KeyError:
            raise DslError(
                f"unknown feature {name!r} in {self.expression!r} -- not in the "
                f"panel registry") from None

    def visit_Constant(self, node: ast.Constant) -> Callable:
        if isinstance(node.value, str):
            # string literals live ONLY in factor("<family>", "<column>")
            # (handled in _factor_call straight off the AST); a stray text
            # operand stays a compile-time error
            self.fail(node, f"string constant {node.value!r} is only valid "
                            'inside factor("<family>", "<column>")')
        if isinstance(node.value, _NUM_CONST) and not isinstance(node.value, bool):
            return lambda env, v=float(node.value): v
        if isinstance(node.value, bool):
            return lambda env, v=node.value: v
        self.fail(node, f"constant {node.value!r} is not a number/bool")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Callable:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            def run(env):
                value = operand(env)
                if value.dtype != bool:
                    self.fail(node, "'not' needs a boolean operand (compare first)")
                return ~value
            return run
        if isinstance(node.op, ast.USub):
            return lambda env: -np.asarray(operand(env), dtype=float)
        if isinstance(node.op, ast.UAdd):
            return lambda env: +np.asarray(operand(env), dtype=float)
        self.fail(node, f"unary {type(node.op).__name__} is not allowed")

    _BINOPS = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply,
               ast.Div: np.true_divide, ast.Mod: np.mod, ast.FloorDiv: np.floor_divide,
               ast.Pow: np.power}

    def visit_BinOp(self, node: ast.BinOp) -> Callable:
        left, right = self.visit(node.left), self.visit(node.right)
        fn = self._BINOPS.get(type(node.op))
        if fn is None:
            self.fail(node, f"operator {type(node.op).__name__} is not allowed")

        def run(env):
            a = self._numeric(left(env))
            b = self._numeric(right(env))
            if fn in (np.floor_divide, np.mod):
                with np.errstate(invalid="ignore", divide="ignore"):
                    return fn(a, np.where(b == 0, np.nan, b))
            with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
                return fn(a, b)
        return run

    _CMPOPS = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater,
               ast.GtE: np.greater_equal}

    def visit_Compare(self, node: ast.Compare) -> Callable:
        if len(node.ops) != 1 or len(node.comparators) != 1:
            self.fail(node, "chained comparisons (a < b < c) are not supported; "
                            "use 'and'")
        left, right = self.visit(node.left), self.visit(node.comparators[0])
        fn = self._CMPOPS.get(type(node.ops[0]))
        if fn is None:
            self.fail(node, f"comparison {type(node.ops[0]).__name__} is not allowed "
                            "(== / != are meaningless on NaN-cross-sections)")
        with np.errstate(invalid="ignore"):
            def run(env):
                a = self._numeric(left(env))
                b = self._numeric(right(env))
                # NaN on either side is already False; the isfinite guard
                # makes that explicit rather than relying on numpy semantics
                return fn(a, b) & np.isfinite(a) & np.isfinite(b)
            return run

    def visit_BoolOp(self, node: ast.BoolOp) -> Callable:
        if not isinstance(node.op, (ast.And, ast.Or)):
            self.fail(node, f"boolean {type(node.op).__name__} is not allowed")
        parts = [self.visit(v) for v in node.values]
        combine = (lambda xs: np.logical_and.reduce(xs)) \
            if isinstance(node.op, ast.And) else (lambda xs: np.logical_or.reduce(xs))

        def run(env):
            values = [part(env) for part in parts]
            for value in values:
                if not (isinstance(value, np.ndarray) and value.dtype == bool):
                    self.fail(node, "and/or need boolean operands (each side must "
                                    "be a comparison)")
            return combine(values)
        return run

    def visit_IfExp(self, node: ast.IfExp) -> Callable:
        # the condition must be structurally boolean (a comparison, a
        # boolean op, or a negation) -- a bare feature name is numeric and
        # is rejected here, at compile time, rather than at evaluation
        is_not = isinstance(node.test, ast.UnaryOp) \
            and isinstance(node.test.op, ast.Not)
        if not (isinstance(node.test, (ast.Compare, ast.BoolOp)) or is_not):
            self.fail(node, "if/else condition must be a comparison or boolean "
                            "expression (a bare feature name is numeric)")
        test, body, orelse = self.visit(node.test), self.visit(node.body), \
            self.visit(node.orelse)

        def run(env):
            cond = test(env)
            if not (isinstance(cond, np.ndarray) and cond.dtype == bool):
                self.fail(node, "if/else condition must be boolean")
            a = self._numeric(body(env))
            b = self._numeric(orelse(env))
            return np.where(cond, a, b)
        return run

    # ------------------------------------------------------------ calls
    def visit_Call(self, node: ast.Call) -> Callable:
        if not isinstance(node.func, ast.Name):
            self.fail(node, "only plain function names can be called")
        name = node.func.id
        if name not in DSL_FUNCTIONS:
            self.fail(node, f"function {name!r} is not on the whitelist "
                            f"(known: {', '.join(allowed_functions())})")
        if node.keywords:
            self.fail(node, "keyword arguments are not supported")
        if name == "factor":
            return self._factor_call(node)
        args = [self.visit(a) for a in node.args]
        # arity check per function (industry_rank may bind _industry itself)
        arity = {"clip": (3,), "fillna": (2,), "minimum": (2,), "maximum": (2,),
                 "industry_rank": (1,), "rank": (1,), "pct_rank": (1,), "zscore": (1,),
                 "abs": (1,), "log": (1,), "sqrt": (1,), "sign": (1,)}
        if name in arity and len(args) not in arity[name]:
            self.fail(node, f"{name}() takes {arity[name][0]} argument(s), got "
                            f"{len(args)}")

        def run(env):
            values = [a(env) for a in args]
            fn = DSL_FUNCTIONS[name]
            if name == "industry_rank":
                return fn(values[0], env.get("_industry"))
            return fn(*values)
        return run

    def _factor_call(self, node: ast.Call) -> Callable:
        """``factor("<family>", "<column>")`` -> a dynamic feature lookup.

        Both arguments must be STRING LITERALS — the feature name is fixed
        at compile time (a per-symbol family/column makes no sense in a
        cross-sectional expression), and the resulting name is exactly
        what :func:`expr_names` reports so the env pre-fetches it.
        """
        if len(node.args) != 2 or not all(isinstance(a, ast.Constant)
                                          and isinstance(a.value, str)
                                          for a in node.args):
            self.fail(node, 'factor() takes two string literals, e.g. '
                            'factor("finance_Q_factor", "roe")')
        family, column = node.args[0].value, node.args[1].value
        feature = factor_feature_name(family, column)

        def run(env):
            try:
                return env[feature]
            except KeyError:
                raise DslError(
                    f"factor feature {feature!r} is not in the env") from None
        return run

    # ------------------------------------------------------------ rejects
    def generic_visit(self, node: ast.AST) -> None:  # everything unlisted
        self.fail(node, f"{type(node).__name__} is not allowed in the DSL")


def expr_names(expression: str) -> list:
    """The FEATURE names an expression mentions (function names like
    ``rank`` excluded; ``factor("f","c")`` contributes its dynamic
    ``factor_f__c`` name).  Used for NA bookkeeping: a name is "unknown"
    for the expression only when every feature it mentions is missing."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise DslError(f"{expression!r}: syntax error: {exc.msg}") from exc
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in DSL_FUNCTIONS \
                and node.id not in _RESERVED:
            names.append(node.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "factor" and len(node.args) == 2 \
                and all(isinstance(a, ast.Constant) and isinstance(a.value, str)
                        for a in node.args):
            names.append(factor_feature_name(node.args[0].value,
                                             node.args[1].value))
    return sorted(set(names))


def compile_expr(expression: str) -> Callable[[Mapping[str, np.ndarray]], np.ndarray]:
    """Compile ``expression`` into ``env -> ndarray``.

    Raises :class:`DslError` on any unsafe or unknown construct.  The
    returned closure evaluates vectorized; for a boolean expression the
    result dtype is ``bool`` (NaN comparisons False), for a ranking
    expression ``float``.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise DslError("expression must be a non-empty string")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise DslError(f"{expression!r}: syntax error: {exc.msg} "
                       f"(line {exc.lineno})") from exc
    if isinstance(tree.body, ast.Name) and tree.body.id in DSL_FUNCTIONS:
        raise DslError(f"{expression!r}: a bare function name is not an expression")
    compiler = _Compiler(expression)
    fn = compiler.visit(tree)

    def checked(env):
        out = fn(env)
        if isinstance(out, bool):
            out = np.full(len(next(iter(env.values()))), out)
        return out

    return checked


def _top_level_kind(expression: str) -> str:
    """``"bool"`` / ``"numeric"`` / ``"callable"`` by the shape of the
    expression's top node -- known at COMPILE time, before any env exists."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise DslError(f"{expression!r}: syntax error: {exc.msg}") from exc
    node = tree.body
    if isinstance(node, ast.Name) and node.id in DSL_FUNCTIONS:
        return "callable"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return "bool"
    return "bool" if isinstance(node, (ast.BoolOp, ast.Compare)) else "numeric"


def compile_bool(expression: str) -> Callable[[Mapping[str, np.ndarray]], np.ndarray]:
    """:func:`compile_expr` that requires a BOOLEAN expression (what a
    filter needs — ``pe_ttm`` alone would rank, not filter; a bare function
    name is not an expression at all)."""
    kind = _top_level_kind(expression)
    if kind != "bool":
        raise DslError(
            f"{expression!r} must be boolean (comparisons joined by and/or) to "
            "work as a filter" if kind == "numeric" else
            f"{expression!r} is a bare function name, not an expression")
    fn = compile_expr(expression)

    def checked(env):
        out = fn(env)
        if not (isinstance(out, np.ndarray) and out.dtype == bool):
            raise DslError(f"{expression!r} did not evaluate to a boolean array")
        return out
    return checked


def compile_score(expression: str) -> Callable[[Mapping[str, np.ndarray]], np.ndarray]:
    """:func:`compile_expr` that requires a numeric expression (a ranker)."""
    kind = _top_level_kind(expression)
    if kind != "numeric":
        raise DslError(f"{expression!r} must be numeric to work as a score" if kind == "bool"
                       else f"{expression!r} is a bare function name, not an expression")
    fn = compile_expr(expression)

    def checked(env):
        out = fn(env)
        if isinstance(out, np.ndarray) and out.dtype == bool:
            raise DslError(f"{expression!r} did not evaluate to a numeric array")
        return np.asarray(out, dtype=float)
    return checked
