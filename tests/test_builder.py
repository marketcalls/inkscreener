import numpy as np
import pytest

from app import builder
from app.expr import Context, evaluate
from app.scan import ScanDef, list_scans, load_scan, run_scan
from conftest import make_data


def all_saved_expressions():
    out = []
    for s in list_scans():
        scan = load_scan(s["id"])
        stack = [scan.group]
        while stack:
            g = stack.pop()
            for f in g.filters:
                if f.group:
                    stack.append(f.group)
                else:
                    out.append(("condition", f.expr))
        out += [("operand", c.expr) for c in scan.columns]
    return out


@pytest.fixture(scope="module")
def ctx():
    rng = np.random.default_rng(7)
    walk = lambda: list(100 + np.cumsum(rng.normal(0, 1, 400)))  # noqa: E731
    return Context(make_data({"A": walk(), "B": walk(), "C": walk()}, bench=walk()))


@pytest.mark.parametrize("kind, expr", all_saved_expressions() + [("condition", p[1]) for p in builder.PRESETS])
def test_round_trip_is_clickable_and_equivalent(kind, expr, ctx):
    if kind == "condition":
        tree = builder.condition_tree(expr)
        back = builder.condition_expr(tree)
    else:
        tree = builder.operand_tree(expr)
        back = builder.operand_expr(tree)
    assert tree["type"] != "advanced", f"{expr} should be editable by clicking"
    a, b = evaluate(expr, ctx).data, evaluate(back, ctx).data
    np.testing.assert_array_equal(np.isnan(a.to_numpy()), np.isnan(b.to_numpy()))
    np.testing.assert_allclose(np.nan_to_num(a.to_numpy()), np.nan_to_num(b.to_numpy()))


def test_short_term_breakout_tree_shape():
    t = builder.condition_tree("max(close, 5) > max(close, 120)[6] * 1.05")
    assert t["op"] == ">"
    assert t["left"] == {"type": "fn", "name": "max", "offset": 0,
                         "args": [{"type": "field", "name": "close", "offset": 0}, {"type": "num", "value": 5}]}
    right = t["right"]
    assert right["type"] == "bin" and right["op"] == "*" and right["right"] == {"type": "num", "value": 1.05}
    assert right["left"]["offset"] == 6


def test_defaults_are_filled_in():
    t = builder.condition_tree("rsi(close) < 30")
    assert t["left"]["args"][1] == {"type": "num", "value": 14}


def test_unrepresentable_falls_back_to_advanced():
    for expr in ["close > open and volume > 0", "100 < close < 2000", "count(close > open, 5) >= 3",
                 "not (close > open)"]:
        assert builder.condition_tree(expr) == {"type": "advanced", "expr": expr}


@pytest.mark.parametrize("tree, fragment", [
    ({"type": "compare", "op": ";", "left": {"type": "num", "value": 1}, "right": {"type": "num", "value": 2}}, "comparison"),
    ({"type": "compare", "op": ">", "left": {"type": "field", "name": "__class__"}, "right": {"type": "num", "value": 2}}, "Unknown field"),
    ({"type": "compare", "op": ">", "left": {"type": "fn", "name": "eval", "args": []}, "right": {"type": "num", "value": 2}}, "Unknown function"),
    ({"type": "compare", "op": ">", "left": {"type": "num", "value": "1); import os"}, "right": {"type": "num", "value": 2}}, "valid number"),
    ({"type": "compare", "op": ">", "left": {"type": "field", "name": "close", "offset": -1}, "right": {"type": "num", "value": 2}}, "Offset"),
    ({"type": "compare", "op": ">", "left": {"type": "fn", "name": "sma", "args": [{"type": "field", "name": "close"}, {"type": "num", "value": 2.5}]}, "right": {"type": "num", "value": 2}}, "whole number"),
])
def test_bad_trees_are_rejected(tree, fragment):
    with pytest.raises(builder.TreeError) as err:
        builder.condition_expr(tree)
    assert fragment in str(err.value)


def test_scan_accepts_trees(store_factory):
    store = store_factory({"A": [1, 2, 3], "B": [3, 2, 1]})
    tree = {"type": "compare", "op": ">", "left": {"type": "field", "name": "close", "offset": 0},
            "right": {"type": "field", "name": "close", "offset": 1}}
    scan = ScanDef.model_validate({"universe": "historify", "group": {"filters": [{"tree": tree}]},
                                   "columns": [{"tree": {"type": "field", "name": "close"}, "as": "Close"}]})
    assert scan.group.filters[0].expr == "close > close[1]"
    assert "tree" not in scan.model_dump()["group"]["filters"][0]
    assert [r["symbol"] for r in run_scan(scan, store)["rows"]] == ["A"]


def test_palette_lists_presets_as_trees():
    p = builder.palette()
    assert any(s["title"] == "Momentum" for s in p["sections"])
    assert all(x["tree"]["type"] != "advanced" for x in p["presets"])
