"""Point-and-click builder support.

The UI edits filters as small JSON trees (tokens), never as typed text:

    condition  {"type": "compare", "op": ">", "left": <operand>, "right": <operand>}
               {"type": "cross", "dir": "above" | "below", "left": <operand>, "right": <operand>}
               {"type": "advanced", "expr": "..."}           (anything the builder cannot show)
    operand    {"type": "num", "value": 1.05}
               {"type": "field", "name": "close", "offset": 0, "bench": false}
               {"type": "fn", "name": "max", "args": [<operand>, {"type": "num", "value": 120}], "offset": 6}
               {"type": "bin", "op": "*", "left": <operand>, "right": <operand>}

This module converts trees to expression strings (validated against allow-lists, then parsed
again by expr.parse) and expression strings back to trees, and serves the builder palette.
"""

from __future__ import annotations

import ast
import math

from .expr import BENCHMARK_NAME, FIELD_LABELS, FUNCTIONS, MAX_SHIFT, REQUIRED, ExprError, parse


class TreeError(ValueError):
    pass


# --------------------------------------------------------------------------- palette

FIELD_ITEMS = [
    ("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"), ("volume", "Volume"),
    ("pct_change", "% Change"), ("change", "Change"), ("hl2", "HL2"), ("hlc3", "HLC3"), ("ohlc4", "OHLC4"),
]

# section -> [(function, label, description, default overrides for required params)]
FUNCTION_SECTIONS = [
    ("Moving averages", [
        ("sma", "SMA", "Simple moving average", {"n": 20}),
        ("ema", "EMA", "Exponential moving average", {"n": 20}),
        ("wma", "WMA", "Weighted moving average", {"n": 20}),
        ("rma", "Wilder MA", "Wilder's rolling average", {"n": 14}),
    ]),
    ("Highest, lowest, totals", [
        ("max", "Max", "Highest value in the last n days", {"n": 20}),
        ("min", "Min", "Lowest value in the last n days", {"n": 20}),
        ("sum", "Sum", "Total over the last n days", {"n": 20}),
        ("std", "Std dev", "Standard deviation over n days", {"n": 20}),
    ]),
    ("Momentum", [
        ("rsi", "RSI", "Relative Strength Index", {}),
        ("stoch_k", "Stochastic %K", "Stochastic oscillator", {}),
        ("stoch_d", "Stochastic %D", "Smoothed stochastic", {}),
        ("macd_line", "MACD line", "MACD line", {}),
        ("macd_signal", "MACD signal", "MACD signal line", {}),
        ("macd_hist", "MACD histogram", "MACD minus its signal", {}),
    ]),
    ("Trend", [
        ("supertrend", "Supertrend", "Supertrend line", {}),
        ("adx", "ADX", "Trend strength", {}),
        ("plus_di", "+DI", "Positive directional index", {}),
        ("minus_di", "-DI", "Negative directional index", {}),
    ]),
    ("Volatility", [
        ("atr", "ATR", "Average True Range", {}),
        ("bb_upper", "Upper Bollinger band", "SMA plus k standard deviations", {}),
        ("bb_lower", "Lower Bollinger band", "SMA minus k standard deviations", {}),
    ]),
]

PARAM_LABELS = {"x": "of", "n": "period", "fast": "fast", "slow": "slow", "signal": "signal",
                "mult": "multiplier", "k": "std devs", "d": "smoothing"}

COMPARE_OPERATORS = [
    (">", "Greater than"), (">=", "Greater than or equal to"), ("<", "Less than"),
    ("<=", "Less than or equal to"), ("==", "Equals"), ("!=", "Not equals"),
]
CROSS_OPERATORS = [("above", "Crossed above"), ("below", "Crossed below")]
MATH_OPERATORS = [("+", "plus"), ("-", "minus"), ("*", "times"), ("/", "divided by")]

PRESETS = [
    ("Close above 200 day SMA", "close > sma(close, 200)"),
    ("Close above 50 day EMA", "close > ema(close, 50)"),
    ("RSI 14 below 30", "rsi(close, 14) < 30"),
    ("RSI 14 above 60", "rsi(close, 14) > 60"),
    ("Volume above 20 day average", "volume > sma(volume, 20)"),
    ("Volume twice the average", "volume > sma(volume, 20) * 2"),
    ("Up more than 3% today", "pct_change > 3"),
    ("Gap up", "open > high[1]"),
    ("New 52 week high", "high >= max(high, 250)"),
    ("Within 5% of 52 week high", "close > max(high, 250) * 0.95"),
    ("EMA 9 crossed above EMA 21", "crossed_above(ema(close, 9), ema(close, 21))"),
    ("Close above Supertrend", "close > supertrend(10, 3)"),
    ("MACD above signal", "macd_line(close, 12, 26) > macd_signal(close, 12, 26, 9)"),
    ("ADX above 25", "adx(14) > 25"),
    ("Beating Nifty over 60 days", "close / close[60] > nifty.close / nifty.close[60]"),
    ("Close above 100", "close > 100"),
]


def _param_default(fn_name: str, pname: str, kind: str, default, overrides: dict):
    if kind == "series":
        return {"type": "field", "name": "close", "offset": 0}
    if default is REQUIRED:
        default = overrides.get(pname, 14)
    return default


def palette() -> dict:
    sections = [{"title": "Price and volume",
                 "items": [{"kind": "field", "name": n, "label": l} for n, l in FIELD_ITEMS]}]
    for title, items in FUNCTION_SECTIONS:
        out = []
        for name, label, doc, overrides in items:
            fn = FUNCTIONS[name]
            params = [{"name": p, "kind": k, "label": PARAM_LABELS.get(p, p),
                       "default": _param_default(name, p, k, d, overrides)} for p, k, d in fn.params]
            out.append({"kind": "fn", "name": name, "label": label, "doc": doc, "params": params})
        sections.append({"title": title, "items": out})
    sections.append({"title": "Nifty 50 index", "items": [
        {"kind": "bench", "name": n, "label": f"Nifty 50 {l}"}
        for n, l in FIELD_ITEMS if n in ("open", "high", "low", "close", "pct_change")]})
    sections.append({"title": "Number", "items": [{"kind": "num", "label": "Number"}]})
    return {
        "sections": sections,
        "compare": [{"op": o, "label": l} for o, l in COMPARE_OPERATORS],
        "cross": [{"dir": d, "label": l} for d, l in CROSS_OPERATORS],
        "math": [{"op": o, "label": l} for o, l in MATH_OPERATORS],
        "presets": [{"label": l, "expr": e, "tree": condition_tree(e)} for l, e in PRESETS],
    }


# --------------------------------------------------------------------------- tree -> expression

_CMP = {o for o, _ in COMPARE_OPERATORS}
_MATH = {o for o, _ in MATH_OPERATORS}
_SPECIAL = {"crossed_above", "crossed_below", "count", "greatest", "least", "abs", "round"}


def _num(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TreeError(f"Not a valid number: {value!r}")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return repr(value)


def _offset(node: dict) -> str:
    k = node.get("offset", 0) or 0
    if isinstance(k, bool) or not isinstance(k, int) or not 0 <= k <= MAX_SHIFT:
        raise TreeError(f"Offset must be a whole number of days, got {k!r}")
    return f"[{k}]" if k else ""


def operand_expr(node: dict) -> str:
    if not isinstance(node, dict):
        raise TreeError("Operand must be an object")
    t = node.get("type")
    if t == "num":
        return _num(node.get("value"))
    if t == "field":
        name = node.get("name")
        if name not in FIELD_LABELS:
            raise TreeError(f"Unknown field {name!r}")
        prefix = f"{BENCHMARK_NAME}." if node.get("bench") else ""
        return f"{prefix}{name}{_offset(node)}"
    if t == "fn":
        name = node.get("name")
        if name not in FUNCTIONS or name in _SPECIAL:
            raise TreeError(f"Unknown function {name!r}")
        params = FUNCTIONS[name].params
        args = node.get("args") or []
        if len(args) != len(params):
            raise TreeError(f"{name} needs {len(params)} settings, got {len(args)}")
        parts = []
        for (pname, kind, _), arg in zip(params, args):
            if kind in ("int", "int0", "num"):
                if not isinstance(arg, dict) or arg.get("type") != "num":
                    raise TreeError(f"{name}: '{pname}' must be a number")
                value = arg.get("value")
                if kind in ("int", "int0") and (not isinstance(value, (int, float)) or float(value) != int(value)):
                    raise TreeError(f"{name}: '{pname}' must be a whole number")
                parts.append(_num(int(value) if kind != "num" else value))
            else:
                parts.append(operand_expr(arg))
        return f"{name}({', '.join(parts)}){_offset(node)}"
    if t == "bin":
        op = node.get("op")
        if op not in _MATH:
            raise TreeError(f"Unknown math operator {op!r}")
        wrap = lambda n: f"({operand_expr(n)})" if isinstance(n, dict) and n.get("type") == "bin" else operand_expr(n)  # noqa: E731
        return f"{wrap(node.get('left'))} {op} {wrap(node.get('right'))}"
    if t == "advanced":
        return str(node.get("expr", ""))
    raise TreeError(f"Unknown operand type {t!r}")


def condition_expr(node: dict) -> str:
    if not isinstance(node, dict):
        raise TreeError("Condition must be an object")
    t = node.get("type")
    if t == "compare":
        if node.get("op") not in _CMP:
            raise TreeError(f"Unknown comparison {node.get('op')!r}")
        return f"{operand_expr(node.get('left'))} {node['op']} {operand_expr(node.get('right'))}"
    if t == "cross":
        if node.get("dir") not in ("above", "below"):
            raise TreeError("Cross direction must be 'above' or 'below'")
        return f"crossed_{node['dir']}({operand_expr(node.get('left'))}, {operand_expr(node.get('right'))})"
    if t == "advanced":
        return str(node.get("expr", ""))
    raise TreeError(f"Unknown condition type {t!r}")


# --------------------------------------------------------------------------- expression -> tree


class _Unsupported(Exception):
    pass


_CMP_AST = {ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=", ast.Eq: "==", ast.NotEq: "!="}
_MATH_AST = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}


def _const(node) -> float | int:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_const(node.operand)
    raise _Unsupported


def _operand(node: ast.AST, offset: int = 0) -> dict:
    if isinstance(node, (ast.Constant, ast.UnaryOp)):
        if offset:
            raise _Unsupported
        return {"type": "num", "value": _const(node)}
    if isinstance(node, ast.Name):
        return {"type": "field", "name": node.id, "offset": offset}
    if isinstance(node, ast.Attribute):
        return {"type": "field", "name": node.attr, "bench": True, "offset": offset}
    if isinstance(node, ast.Subscript):
        if isinstance(node.value, (ast.Name, ast.Attribute, ast.Call)):
            return _operand(node.value, offset + node.slice.value)
        raise _Unsupported
    if isinstance(node, ast.Call):
        name = node.func.id
        if name in _SPECIAL:
            raise _Unsupported
        fn = FUNCTIONS[name]
        args = []
        for i, (_, kind, default) in enumerate(fn.params):
            if i < len(node.args):
                a = node.args[i]
                args.append({"type": "num", "value": _const(a)} if kind in ("int", "int0", "num") else _operand(a))
            else:
                args.append({"type": "num", "value": default})
        return {"type": "fn", "name": name, "args": args, "offset": offset}
    if isinstance(node, ast.BinOp) and not offset:
        return {"type": "bin", "op": _MATH_AST[type(node.op)], "left": _operand(node.left),
                "right": _operand(node.right)}
    raise _Unsupported


def condition_tree(expr: str) -> dict:
    try:
        node = parse(expr).body
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            return {"type": "compare", "op": _CMP_AST[type(node.ops[0])],
                    "left": _operand(node.left), "right": _operand(node.comparators[0])}
        if isinstance(node, ast.Call) and node.func.id in ("crossed_above", "crossed_below") and len(node.args) == 2:
            return {"type": "cross", "dir": node.func.id.split("_")[1],
                    "left": _operand(node.args[0]), "right": _operand(node.args[1])}
    except (_Unsupported, ExprError):
        pass
    return {"type": "advanced", "expr": expr}


def operand_tree(expr: str) -> dict:
    try:
        return _operand(parse(expr).body)
    except (_Unsupported, ExprError):
        return {"type": "advanced", "expr": expr}
