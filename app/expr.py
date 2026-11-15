"""Scan expression language: parse, evaluate and explain.

Expressions use Python syntax but only an allow-list of names, functions and operators:

    max(close, 5) > max(close, 120)[6] * 1.05
    volume > sma(volume, 5) and close > close[1]
    crossed_above(ema(close, 9), ema(close, 21))
    100 < close < 2000
    close / close[60] > nifty.close / nifty.close[60]

`x[n]` means "x, n trading days ago".

Truth values are three-valued: 1.0 (true), 0.0 (false), NaN (unknown, not enough data).
Only 1.0 counts as a match, so a symbol without enough history never matches, even
inside `not (...)`.
"""

from __future__ import annotations

import ast
import difflib
import math
import operator
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

import numpy as np
import pandas as pd

from . import functions as F
from .datastore import MarketData

MAX_EXPR_LENGTH = 600
MAX_SHIFT = 5000
MAX_PERIOD = 2500


class ExprError(ValueError):
    """Raised for any invalid expression. The message is safe to show to users."""


# --------------------------------------------------------------------------- values


@dataclass
class Val:
    data: pd.DataFrame | float
    kind: str  # "num" or "bool"

    @property
    def is_frame(self) -> bool:
        return isinstance(self.data, pd.DataFrame)


def _is_nan(x) -> bool:
    return isinstance(x, float) and math.isnan(x)


def _valid(x):
    return x.notna() if isinstance(x, pd.DataFrame) else not _is_nan(x)


def _shift(x, n: int):
    return x.shift(n) if isinstance(x, pd.DataFrame) else x


def compare(a, b, op: Callable) -> pd.DataFrame | float:
    """Elementwise comparison returning 1.0 / 0.0 / NaN."""
    if not isinstance(a, pd.DataFrame) and not isinstance(b, pd.DataFrame):
        if _is_nan(a) or _is_nan(b):
            return math.nan
        return 1.0 if op(a, b) else 0.0
    result = op(a, b).astype(float)
    va, vb = _valid(a), _valid(b)
    if isinstance(va, bool):
        mask = vb if va else vb & False
    elif isinstance(vb, bool):
        mask = va if vb else va & False
    else:
        mask = va & vb
    return result.where(mask)


def k_and(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    false = (a == 0) | (b == 0)
    true = (a == 1) & (b == 1)
    return pd.DataFrame(np.where(false, 0.0, np.where(true, 1.0, np.nan)), index=a.index, columns=a.columns)


def k_or(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    true = (a == 1) | (b == 1)
    false = (a == 0) & (b == 0)
    return pd.DataFrame(np.where(true, 1.0, np.where(false, 0.0, np.nan)), index=a.index, columns=a.columns)


def k_not(a):
    return 1.0 - a


def k_at_least(parts: list[pd.DataFrame], n: int) -> pd.DataFrame:
    """True when at least n parts are true, false when that is impossible, else unknown."""
    stacked = np.stack([p.to_numpy() for p in parts])
    trues = (stacked == 1).sum(axis=0)
    unknown = np.isnan(stacked).sum(axis=0)
    out = np.where(trues >= n, 1.0, np.where(trues + unknown < n, 0.0, np.nan))
    ref = parts[0]
    return pd.DataFrame(out, index=ref.index, columns=ref.columns)


# --------------------------------------------------------------------------- context


FIELD_LABELS = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "pct_change": "% change",
    "change": "change",
    "hl2": "(high + low) / 2",
    "hlc3": "(high + low + close) / 3",
    "ohlc4": "(open + high + low + close) / 4",
}
BENCHMARK_NAME = "nifty"


class Context:
    """Evaluation context: lazy, cached fields for one MarketData."""

    def __init__(self, data: MarketData):
        self.data = data
        self._cache: dict[str, pd.DataFrame] = {}

    def _raw(self, name: str, benchmark: bool) -> pd.DataFrame:
        return self.data.benchmark_frame(name) if benchmark else self.data.frames[name]

    def field(self, name: str, benchmark: bool = False) -> pd.DataFrame:
        key = ("bench:" if benchmark else "") + name
        if key in self._cache:
            return self._cache[key]
        raw = lambda f: self._raw(f, benchmark)  # noqa: E731
        if name in ("open", "high", "low", "close", "volume"):
            value = raw(name)
        elif name == "pct_change":
            close = raw("close")
            value = (close / close.shift(1) - 1) * 100
        elif name == "change":
            close = raw("close")
            value = close - close.shift(1)
        elif name == "hl2":
            value = (raw("high") + raw("low")) / 2
        elif name == "hlc3":
            value = (raw("high") + raw("low") + raw("close")) / 3
        elif name == "ohlc4":
            value = (raw("open") + raw("high") + raw("low") + raw("close")) / 4
        else:
            raise ExprError(f"Unknown field '{name}'")
        self._cache[key] = value
        return value

    def full(self, value: float) -> pd.DataFrame:
        close = self.data.frames["close"]
        return pd.DataFrame(value, index=close.index, columns=close.columns)

    def as_frame(self, val: Val) -> pd.DataFrame:
        return val.data if val.is_frame else self.full(float(val.data))


# --------------------------------------------------------------------------- functions

REQUIRED = object()


@dataclass
class Fn:
    params: list[tuple[str, str, object]]  # (name, kind, default)
    impl: Callable
    describe: Callable[[list[str]], str]
    returns: str = "num"
    ohlc: bool = False  # impl receives high, low, close before the other args
    variadic: bool = False
    doc: str = ""


def _series(name="x"):
    return (name, "series", REQUIRED)


def _period(name="n", default=REQUIRED):
    return (name, "int", default)


FUNCTIONS: dict[str, Fn] = {
    "sma": Fn([_series(), _period()], F.sma, lambda a: f"{a[1]}-day SMA of {a[0]}",
              doc="Simple moving average"),
    "ema": Fn([_series(), _period()], F.ema, lambda a: f"{a[1]}-day EMA of {a[0]}",
              doc="Exponential moving average"),
    "wma": Fn([_series(), _period()], F.wma, lambda a: f"{a[1]}-day WMA of {a[0]}",
              doc="Weighted moving average"),
    "rma": Fn([_series(), _period()], F.rma, lambda a: f"{a[1]}-day Wilder average of {a[0]}",
              doc="Wilder (rolling) moving average"),
    "max": Fn([_series(), _period()], F.rolling_max, lambda a: f"highest {a[0]} of the last {a[1]} days",
              doc="Highest value over n days (Chartink Max)"),
    "min": Fn([_series(), _period()], F.rolling_min, lambda a: f"lowest {a[0]} of the last {a[1]} days",
              doc="Lowest value over n days (Chartink Min)"),
    "sum": Fn([_series(), _period()], F.rolling_sum, lambda a: f"sum of {a[0]} over the last {a[1]} days",
              doc="Sum over n days"),
    "std": Fn([_series(), _period()], F.rolling_std, lambda a: f"{a[1]}-day standard deviation of {a[0]}",
              doc="Standard deviation over n days"),
    "rsi": Fn([_series(), _period("n", 14)], F.rsi, lambda a: f"{a[1]}-day RSI of {a[0]}",
              doc="Relative Strength Index (Wilder)"),
    "atr": Fn([_period("n", 14)], F.atr, lambda a: f"{a[0]}-day ATR", ohlc=True,
              doc="Average True Range"),
    "adx": Fn([_period("n", 14)], F.adx, lambda a: f"{a[0]}-day ADX", ohlc=True,
              doc="Average Directional Index"),
    "plus_di": Fn([_period("n", 14)], F.plus_di, lambda a: f"{a[0]}-day +DI", ohlc=True,
                  doc="ADX DI positive"),
    "minus_di": Fn([_period("n", 14)], F.minus_di, lambda a: f"{a[0]}-day -DI", ohlc=True,
                   doc="ADX DI negative"),
    "supertrend": Fn([_period("n", 10), ("mult", "num", 3.0)], F.supertrend,
                     lambda a: f"Supertrend({a[0]}, {a[1]})", ohlc=True,
                     doc="Supertrend line (ATR period, multiplier)"),
    "macd_line": Fn([_series(), _period("fast", 12), _period("slow", 26)], F.macd_line,
                    lambda a: f"MACD line({a[1]}, {a[2]}) of {a[0]}", doc="MACD line"),
    "macd_signal": Fn([_series(), _period("fast", 12), _period("slow", 26), _period("signal", 9)],
                      F.macd_signal, lambda a: f"MACD signal({a[1]}, {a[2]}, {a[3]}) of {a[0]}",
                      doc="MACD signal line"),
    "macd_hist": Fn([_series(), _period("fast", 12), _period("slow", 26), _period("signal", 9)],
                    F.macd_hist, lambda a: f"MACD histogram({a[1]}, {a[2]}, {a[3]}) of {a[0]}",
                    doc="MACD histogram"),
    "bb_upper": Fn([_series(), _period("n", 20), ("k", "num", 2.0)], F.bb_upper,
                   lambda a: f"upper Bollinger band({a[1]}, {a[2]}) of {a[0]}", doc="Upper Bollinger band"),
    "bb_lower": Fn([_series(), _period("n", 20), ("k", "num", 2.0)], F.bb_lower,
                   lambda a: f"lower Bollinger band({a[1]}, {a[2]}) of {a[0]}", doc="Lower Bollinger band"),
    "stoch_k": Fn([_period("n", 14)], F.stoch_k, lambda a: f"{a[0]}-day stochastic %K", ohlc=True,
                  doc="Stochastic %K"),
    "stoch_d": Fn([_period("n", 14), _period("d", 3)], F.stoch_d,
                  lambda a: f"stochastic %D({a[0]}, {a[1]})", ohlc=True, doc="Stochastic %D"),
    "count": Fn([("cond", "cond", REQUIRED), _period()], None,
                lambda a: f"number of days in the last {a[1]} on which {a[0]}",
                doc="Days in the last n on which a condition was true (Chartink Count)"),
    "abs": Fn([("x", "value", REQUIRED)], None, lambda a: f"absolute value of {a[0]}", doc="Absolute value"),
    "round": Fn([("x", "value", REQUIRED), ("digits", "int0", 0)], None,
                lambda a: f"{a[0]} rounded to {a[1]} decimals", doc="Round"),
    "greatest": Fn([], None, lambda a: "the greatest of " + ", ".join(a), variadic=True,
                   doc="Largest of several values"),
    "least": Fn([], None, lambda a: "the least of " + ", ".join(a), variadic=True,
                doc="Smallest of several values"),
    "crossed_above": Fn([("a", "value", REQUIRED), ("b", "value", REQUIRED)], None,
                        lambda a: f"{a[0]} crossed above {a[1]}", returns="bool",
                        doc="a was at or below b yesterday and is above b today"),
    "crossed_below": Fn([("a", "value", REQUIRED), ("b", "value", REQUIRED)], None,
                        lambda a: f"{a[0]} crossed below {a[1]}", returns="bool",
                        doc="a was at or above b yesterday and is below b today"),
}


def _signature(name: str, fn: Fn) -> str:
    if fn.variadic:
        return f"{name}(a, b, ...)"
    parts = []
    for pname, _, default in fn.params:
        parts.append(pname if default is REQUIRED else f"{pname}={_fmt_num(default)}")
    return f"{name}({', '.join(parts)})"


def reference() -> dict:
    """Fields and functions, for the UI help panel."""
    return {
        "fields": [{"name": k, "label": v} for k, v in FIELD_LABELS.items()],
        "benchmark": f"{BENCHMARK_NAME}.<field>, for example {BENCHMARK_NAME}.close",
        "offset": "x[n] is x as of n trading days ago, for example close[1]",
        "operators": ["+", "-", "*", "/", ">", ">=", "<", "<=", "==", "!=", "and", "or", "not"],
        "functions": [{"name": k, "signature": _signature(k, f), "doc": f.doc} for k, f in FUNCTIONS.items()],
    }


# --------------------------------------------------------------------------- parsing

_ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load, ast.Attribute, ast.Subscript,
    ast.UnaryOp, ast.USub, ast.UAdd, ast.Not, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.Compare, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq,
    ast.BoolOp, ast.And, ast.Or, ast.Call,
)

_NODE_HINTS = {
    ast.Pow: "use x * x instead of x ** 2",
    ast.BitAnd: "use 'and' instead of '&'",
    ast.BitOr: "use 'or' instead of '|'",
    ast.Invert: "use 'not' instead of '~'",
}


def _suggest(name: str) -> str:
    options = list(FIELD_LABELS) + list(FUNCTIONS) + [BENCHMARK_NAME]
    close = difflib.get_close_matches(name, options, n=1)
    return f" Did you mean '{close[0]}'?" if close else ""


def _check(node: ast.AST) -> None:
    for child in ast.walk(node):
        if not isinstance(child, _ALLOWED_NODES):
            hint = _NODE_HINTS.get(type(child), "")
            raise ExprError(f"'{type(child).__name__}' is not allowed in a scan expression. {hint}".strip())
        if isinstance(child, ast.Constant) and (
            isinstance(child.value, bool) or not isinstance(child.value, (int, float))
        ):
            raise ExprError(f"Only numbers are allowed as constants, got {child.value!r}")
        if isinstance(child, ast.Call):
            if not isinstance(child.func, ast.Name):
                raise ExprError("Only plain function calls like sma(close, 20) are allowed")
            if child.func.id not in FUNCTIONS:
                raise ExprError(f"Unknown function '{child.func.id}'.{_suggest(child.func.id)}")
            if child.keywords:
                raise ExprError(f"{child.func.id}(): use positional arguments, for example sma(close, 20)")
        if isinstance(child, ast.Attribute):
            if not (isinstance(child.value, ast.Name) and child.value.id == BENCHMARK_NAME):
                raise ExprError(f"Only {BENCHMARK_NAME}.<field> attributes are allowed")
            if child.attr not in FIELD_LABELS:
                raise ExprError(f"Unknown field '{BENCHMARK_NAME}.{child.attr}'.{_suggest(child.attr)}")
        if isinstance(child, ast.Subscript):
            idx = child.slice
            if not (isinstance(idx, ast.Constant) and isinstance(idx.value, int) and not isinstance(idx.value, bool)
                    and 0 <= idx.value <= MAX_SHIFT):
                raise ExprError("Offsets must be whole numbers, for example close[1] for yesterday's close")


@lru_cache(maxsize=1024)
def parse(src: str) -> ast.Expression:
    src = (src or "").strip()
    if not src:
        raise ExprError("Expression is empty")
    if len(src) > MAX_EXPR_LENGTH:
        raise ExprError(f"Expression is longer than {MAX_EXPR_LENGTH} characters")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"Syntax error at column {exc.offset}: {exc.msg}") from None
    _check(tree)
    # Names that are neither fields nor the benchmark (functions are checked above).
    for child in ast.walk(tree):
        if isinstance(child, ast.Name):
            parent_ok = child.id in FIELD_LABELS or child.id in FUNCTIONS or child.id == BENCHMARK_NAME
            if not parent_ok:
                raise ExprError(f"Unknown name '{child.id}'.{_suggest(child.id)}")
    return tree


# --------------------------------------------------------------------------- evaluation

_CMP = {
    ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Eq: operator.eq, ast.NotEq: operator.ne,
}
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def evaluate(src: str, ctx: Context) -> Val:
    return _eval(parse(src).body, ctx)


def _const_arg(node: ast.AST, fname: str, pname: str, kind: str):
    value = None
    if isinstance(node, ast.Constant):
        value = node.value
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        value = -node.operand.value
    if kind == "num":
        if not isinstance(value, (int, float)):
            raise ExprError(f"{fname}(): '{pname}' must be a number")
        return float(value)
    if not isinstance(value, int) or (kind == "int" and not 1 <= value <= MAX_PERIOD) or (kind == "int0" and value < 0):
        lower = 0 if kind == "int0" else 1
        raise ExprError(f"{fname}(): '{pname}' must be a whole number of at least {lower}")
    return value


def _call(node: ast.Call, ctx: Context) -> Val:
    name = node.func.id
    fn = FUNCTIONS[name]

    if fn.variadic:
        if len(node.args) < 2:
            raise ExprError(f"{name}() needs at least two values")
        vals = [ctx.as_frame(_eval(a, ctx)) for a in node.args]
        stacked = np.stack([v.to_numpy() for v in vals])
        out = stacked.max(axis=0) if name == "greatest" else stacked.min(axis=0)
        return Val(pd.DataFrame(out, index=vals[0].index, columns=vals[0].columns), "num")

    if len(node.args) > len(fn.params):
        raise ExprError(f"{name}() takes at most {len(fn.params)} arguments: {_signature(name, fn)}")

    args = []
    for i, (pname, kind, default) in enumerate(fn.params):
        if i >= len(node.args):
            if default is REQUIRED:
                raise ExprError(f"{name}() is missing '{pname}': {_signature(name, fn)}")
            args.append(default)
            continue
        arg_node = node.args[i]
        if kind in ("int", "int0", "num"):
            args.append(_const_arg(arg_node, name, pname, kind))
            continue
        val = _eval(arg_node, ctx)
        if kind == "series":
            if not val.is_frame:
                raise ExprError(f"{name}(): '{pname}' must be a price series such as close, not a number")
            args.append(val.data)
        elif kind == "cond":
            if val.kind != "bool":
                raise ExprError(f"{name}(): '{pname}' must be a condition such as close > open")
            args.append(ctx.as_frame(val))
        else:  # value: a series or a plain number (conditions count as 1 / 0)
            args.append(val.data)

    if name == "count":
        return Val(args[0].rolling(args[1]).sum(), "num")
    if name == "abs":
        return Val(abs(args[0]), "num")
    if name == "round":
        return Val(args[0].round(args[1]) if isinstance(args[0], pd.DataFrame) else round(args[0], args[1]), "num")
    if name in ("crossed_above", "crossed_below"):
        a, b = args
        now_op, prev_op = (operator.gt, operator.le) if name == "crossed_above" else (operator.lt, operator.ge)
        now = ctx.as_frame(Val(compare(a, b, now_op), "bool"))
        prev = ctx.as_frame(Val(compare(_shift(a, 1), _shift(b, 1), prev_op), "bool"))
        return Val(k_and(now, prev), "bool")
    if fn.ohlc:
        out = fn.impl(ctx.field("high"), ctx.field("low"), ctx.field("close"), *args)
    else:
        out = fn.impl(*args)
    return Val(out, fn.returns)


def _eval(node: ast.AST, ctx: Context) -> Val:
    if isinstance(node, ast.Constant):
        return Val(float(node.value), "num")
    if isinstance(node, ast.Name):
        if node.id in FUNCTIONS:
            raise ExprError(f"'{node.id}' is a function, call it like {_signature(node.id, FUNCTIONS[node.id])}")
        if node.id == BENCHMARK_NAME:
            raise ExprError(f"Use a field of the benchmark, for example {BENCHMARK_NAME}.close")
        return Val(ctx.field(node.id), "num")
    if isinstance(node, ast.Attribute):
        try:
            return Val(ctx.field(node.attr, benchmark=True), "num")
        except KeyError as exc:
            raise ExprError(str(exc).strip("'\"")) from None
    if isinstance(node, ast.Subscript):
        inner = _eval(node.value, ctx)
        return Val(_shift(inner.data, node.slice.value), inner.kind)
    if isinstance(node, ast.UnaryOp):
        inner = _eval(node.operand, ctx)
        if isinstance(node.op, ast.Not):
            if inner.kind != "bool":
                raise ExprError("'not' needs a condition, for example not (close > open)")
            return Val(k_not(inner.data), "bool")
        return Val(-inner.data if isinstance(node.op, ast.USub) else inner.data, "num")
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, ctx), _eval(node.right, ctx)
        with np.errstate(divide="ignore", invalid="ignore"):
            try:
                out = _BIN[type(node.op)](left.data, right.data)
            except ZeroDivisionError:
                out = math.nan
        if isinstance(out, pd.DataFrame):
            out = out.replace([np.inf, -np.inf], np.nan)
        elif isinstance(out, float) and math.isinf(out):
            out = math.nan
        return Val(out, "num")
    if isinstance(node, ast.Compare):
        values = [_eval(node.left, ctx)] + [_eval(c, ctx) for c in node.comparators]
        result = None
        for op, a, b in zip(node.ops, values, values[1:]):
            part = ctx.as_frame(Val(compare(a.data, b.data, _CMP[type(op)]), "bool"))
            result = part if result is None else k_and(result, part)
        return Val(result, "bool")
    if isinstance(node, ast.BoolOp):
        parts = [_eval(v, ctx) for v in node.values]
        for p in parts:
            if p.kind != "bool":
                word = "and" if isinstance(node.op, ast.And) else "or"
                raise ExprError(f"'{word}' joins conditions, for example close > open {word} volume > 0")
        frames = [ctx.as_frame(p) for p in parts]
        combine = k_and if isinstance(node.op, ast.And) else k_or
        out = frames[0]
        for f in frames[1:]:
            out = combine(out, f)
        return Val(out, "bool")
    if isinstance(node, ast.Call):
        return _call(node, ctx)
    raise ExprError(f"Unsupported expression element: {type(node).__name__}")


# --------------------------------------------------------------------------- explain

_CMP_WORDS = {
    ast.Gt: "is greater than", ast.GtE: "is at least", ast.Lt: "is less than",
    ast.LtE: "is at most", ast.Eq: "equals", ast.NotEq: "does not equal",
}
_BIN_WORDS = {ast.Add: "plus", ast.Sub: "minus", ast.Mult: "times", ast.Div: "divided by"}


def _fmt_num(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def explain(src: str) -> str:
    return _explain(parse(src).body)


def _explain(node: ast.AST) -> str:
    def wrap(n: ast.AST) -> str:
        text = _explain(n)
        return f"({text})" if isinstance(n, (ast.BinOp, ast.BoolOp, ast.Compare)) else text

    if isinstance(node, ast.Constant):
        return _fmt_num(node.value)
    if isinstance(node, ast.Name):
        return FIELD_LABELS.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"Nifty 50 {FIELD_LABELS.get(node.attr, node.attr)}"
    if isinstance(node, ast.Subscript):
        n = node.slice.value
        return f"{wrap(node.value)} {n} day{'s' if n != 1 else ''} ago"
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return f"not {wrap(node.operand)}"
        return f"minus {wrap(node.operand)}" if isinstance(node.op, ast.USub) else _explain(node.operand)
    if isinstance(node, ast.BinOp):
        return f"{wrap(node.left)} {_BIN_WORDS[type(node.op)]} {wrap(node.right)}"
    if isinstance(node, ast.Compare):
        parts = []
        left = node.left
        for op, right in zip(node.ops, node.comparators):
            parts.append(f"{wrap(left)} {_CMP_WORDS[type(op)]} {wrap(right)}")
            left = right
        return " and ".join(parts)
    if isinstance(node, ast.BoolOp):
        word = " and " if isinstance(node.op, ast.And) else " or "
        return word.join(wrap(v) for v in node.values)
    if isinstance(node, ast.Call):
        fn = FUNCTIONS[node.func.id]
        if fn.variadic:
            return fn.describe([_explain(a) for a in node.args])
        rendered = []
        for i, (_, _, default) in enumerate(fn.params):
            if i < len(node.args):
                a = node.args[i]
                rendered.append(_explain(a) if not isinstance(a, ast.UnaryOp) else "-" + _explain(a.operand))
            else:
                rendered.append(_fmt_num(default))
        return fn.describe(rendered)
    return ast.unparse(node)
