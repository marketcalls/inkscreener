import pytest

from app.scan import GroupNode, ScanDef, ScanError, run_scan, scan_history

NAN = float("nan")


def scan(filters, match="all", negate=False, **kw):
    return ScanDef(name="t", universe="historify", group={"match": match, "negate": negate, "filters": filters}, **kw)


@pytest.fixture
def store(store_factory):
    # A rises every day, B falls every day, C has only the last two bars.
    return store_factory({
        "A": [10, 11, 12, 13, 14, 15],
        "B": [15, 14, 13, 12, 11, 10],
        "C": [NAN, NAN, NAN, NAN, 20, 21],
    })


def symbols(result):
    return sorted(r["symbol"] for r in result["rows"])


def test_all_filters(store):
    r = run_scan(scan([{"expr": "close > close[1]"}, {"expr": "close > 12"}]), store)
    assert symbols(r) == ["A", "C"]
    assert r["as_of"] == "2024-01-08"


def test_insufficient_history_never_matches(store):
    r = run_scan(scan([{"expr": "close > close[3]"}]), store)
    assert symbols(r) == ["A"]
    assert r["insufficient_history"] == 1  # C


def test_negate_does_not_match_unknown(store):
    r = run_scan(scan([{"expr": "close > close[3]"}], negate=True), store)
    assert symbols(r) == ["B"]


def test_any_n(store):
    filters = [{"expr": "close > close[1]"}, {"expr": "close > 14"}, {"expr": "close < 11"}]
    assert symbols(run_scan(scan(filters, match="any 1"), store)) == ["A", "B", "C"]
    assert symbols(run_scan(scan(filters, match="any 2"), store)) == ["A", "C"]


def test_disabled_filter_is_ignored(store):
    r = run_scan(scan([{"expr": "close > close[1]"}, {"expr": "close > 1000", "enabled": False}]), store)
    assert symbols(r) == ["A", "C"]


def test_nested_group(store):
    inner = {"group": {"match": "any 1", "filters": [{"expr": "close > 20"}, {"expr": "close < 11"}]}}
    r = run_scan(scan([{"expr": "close > 0"}, inner]), store)
    assert symbols(r) == ["B", "C"]


def test_value_filter_points_at_row(store):
    with pytest.raises(ScanError) as err:
        run_scan(scan([{"expr": "close > 1"}, {"expr": "sma(close, 2)"}]), store)
    assert err.value.path == [1] and "not a condition" in err.value.message


def test_bad_expression_points_at_row(store):
    with pytest.raises(ScanError) as err:
        run_scan(scan([{"expr": "close >"}]), store)
    assert err.value.path == [0]


def test_as_of_snaps_to_previous_trading_day(store):
    r = run_scan(scan([{"expr": "close > 0"}]), store, as_of="2024-01-06")  # Saturday
    assert r["as_of"] == "2024-01-05"
    with pytest.raises(ScanError):
        run_scan(scan([{"expr": "close > 0"}]), store, as_of="2023-01-01")


def test_funnel_is_cumulative(store):
    r = run_scan(scan([{"expr": "close > 0"}, {"expr": "close > close[1]"}, {"expr": "close > 20"}]), store)
    assert [f["cumulative"] for f in r["funnel"]] == [3, 2, 1]
    assert [f["pass"] for f in r["funnel"]] == [3, 2, 1]


def test_columns_and_sort(store):
    s = scan([{"expr": "close > 0"}], columns=[{"expr": "close", "as": "Close"}], sort={"by": "Close", "desc": False})
    r = run_scan(s, store)
    assert [row["symbol"] for row in r["rows"]] == ["B", "A", "C"]
    assert r["rows"][0]["values"]["Close"] == 10


def test_history_counts(store):
    h = scan_history(scan([{"expr": "close > close[1]"}]), store, period="9m")
    days = {d["date"]: d["count"] for d in h["days"]}
    assert days["2024-01-01"] == 0
    assert days["2024-01-02"] == 1  # only A has a previous bar and rose
    assert days["2024-01-08"] == 2  # A and C
    assert h["stocks"][0]["symbol"] == "A" and h["stocks"][0]["count"] == 5


def test_match_validation():
    assert GroupNode(match="ANY 2").match == "any 2"
    with pytest.raises(ValueError):
        GroupNode(match="most")
