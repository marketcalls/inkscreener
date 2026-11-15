import math

import numpy as np
import pytest

from app.expr import Context, ExprError, evaluate, explain, parse
from conftest import make_data


def ctx_for(**closes):
    return Context(make_data(closes))


def test_offset_is_previous_bar():
    ctx = ctx_for(A=[1, 2, 3, 4])
    out = evaluate("close[1]", ctx).data["A"].tolist()
    assert math.isnan(out[0]) and out[1:] == [1, 2, 3]


def test_rolling_max_needs_full_window():
    ctx = ctx_for(A=[1, 5, 2, 3, 4])
    out = evaluate("max(close, 3)", ctx).data["A"].tolist()
    assert all(math.isnan(v) for v in out[:2]) and out[2:] == [5, 5, 4]


def test_comparison_is_three_valued():
    ctx = ctx_for(A=[1, 2, 3])
    cond = evaluate("close > close[1]", ctx)
    assert cond.kind == "bool"
    vals = cond.data["A"].tolist()
    assert math.isnan(vals[0]) and vals[1:] == [1.0, 1.0]
    # not(unknown) stays unknown, so a stock without history never matches a negated filter
    neg = evaluate("not (close > close[1])", ctx).data["A"].tolist()
    assert math.isnan(neg[0]) and neg[1:] == [0.0, 0.0]


def test_and_or_with_unknown():
    ctx = ctx_for(A=[1, 2, 3])
    # unknown and false -> false ; unknown or true -> true
    assert evaluate("close > close[1] and close > 100", ctx).data["A"].iloc[0] == 0.0
    assert evaluate("close > close[1] or close > 0", ctx).data["A"].iloc[0] == 1.0


def test_chained_comparison():
    ctx = ctx_for(A=[50, 150, 2500])
    assert evaluate("100 < close < 2000", ctx).data["A"].tolist() == [0.0, 1.0, 0.0]


def test_crossed_above():
    ctx = ctx_for(A=[1, 2, 3, 4, 5], B=[5, 4, 3, 2, 1])
    out = evaluate("crossed_above(close, 2.5)", ctx).data
    assert out["A"].tolist()[1:] == [0.0, 1.0, 0.0, 0.0]
    assert out["B"].tolist()[1:] == [0.0, 0.0, 0.0, 0.0]


def test_division_by_zero_is_unknown():
    ctx = ctx_for(A=[0, 1])
    vals = evaluate("1 / close", ctx).data["A"].tolist()
    assert math.isnan(vals[0]) and vals[1] == 1.0
    assert math.isnan(evaluate("close / 0", ctx).data["A"].iloc[1])


def test_benchmark_is_broadcast():
    data = make_data({"A": [1, 2, 3], "B": [3, 2, 1]}, bench=[10, 11, 12])
    out = evaluate("nifty.close", Context(data)).data
    assert out["A"].tolist() == out["B"].tolist() == [10, 11, 12]


def test_indicators_basic_shapes():
    ctx = ctx_for(A=list(range(1, 61)))
    assert evaluate("rsi(close, 14)", ctx).data["A"].iloc[-1] == pytest.approx(100.0)
    st = evaluate("supertrend(10, 3)", ctx).data["A"]
    assert st.iloc[-1] < 60  # steady uptrend: supertrend sits below price
    for src in ["sma(close, 5)", "ema(close, 5)", "wma(close, 5)", "atr(14)", "adx(14)", "stoch_k(14)",
                "macd_hist(close)", "bb_upper(close, 20, 2)", "count(close > close[1], 10)"]:
        assert not np.isnan(evaluate(src, ctx).data["A"].iloc[-1]), src


@pytest.mark.parametrize("src, fragment", [
    ("__import__('os')", "Unknown function"),
    ("close.__class__", "attributes"),
    ("(lambda: 1)()", "allowed"),
    ("[c for c in close]", "not allowed"),
    ("close ** 2", "x * x"),
    ("sma(close)", "missing 'n'"),
    ("sma(close, 0)", "at least 1"),
    ("sma(close, n)", "Unknown name 'n'"),
    ("close > 'a'", "Only numbers"),
    ("closee > 1", "Did you mean 'close'"),
    ("close >", "Syntax error"),
    ("close[x]", "whole numbers"),
    ("sma(close, length=5)", "positional"),
])
def test_rejects_bad_expressions(src, fragment):
    ctx = ctx_for(A=[1, 2, 3])
    with pytest.raises(ExprError) as err:
        evaluate(src, ctx)
    assert fragment in str(err.value)


def test_explain_reads_like_chartink():
    text = explain("max(close, 5) > max(close, 120)[6] * 1.05")
    assert text == ("highest close of the last 5 days is greater than "
                    "(highest close of the last 120 days 6 days ago times 1.05)")


def test_parse_is_cached():
    assert parse("close > 1") is parse("close > 1")
