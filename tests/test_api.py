import pytest
from fastapi.testclient import TestClient

from app import main
from conftest import make_data


@pytest.fixture
def client():
    main.store.set(make_data({"A": [10, 11, 12, 13], "B": [13, 12, 11, 10]}, bench=[100, 101, 102, 103]))
    with TestClient(main.app) as c:
        yield c


def body(filters, **extra):
    return {"scan": {"name": "api test", "universe": "historify",
                     "group": {"match": "all", "filters": filters}}, **extra}


def test_status(client):
    r = client.get("/api/status").json()
    assert r["loaded"] and r["data"]["symbols"] == 2 and r["data"]["benchmark"] == "NIFTY"


def test_run_inline_scan(client):
    r = client.post("/api/run", json=body([{"expr": "close > close[1]"}]))
    assert r.status_code == 200
    data = r.json()
    assert [row["symbol"] for row in data["rows"]] == ["A"]
    assert data["funnel"][0]["cumulative"] == 1


def test_run_error_points_at_filter(client):
    r = client.post("/api/run", json=body([{"expr": "close > 1"}, {"expr": "clos > 1"}]))
    assert r.status_code == 400
    assert r.json()["detail"]["path"] == [1]


def test_history(client):
    r = client.post("/api/history", json=body([{"expr": "close > close[1]"}], period="3m"))
    assert r.status_code == 200 and r.json()["total_signals"] == 3


def test_saved_scans_are_listed_and_valid(client):
    scans = client.get("/api/scans").json()
    ids = {s["id"] for s in scans}
    assert "short_term_breakouts" in ids
    assert not any(s.get("error") for s in scans)
    assert client.get("/api/scans/short_term_breakouts").json()["group"]["filters"][0]["expr"]


def test_scan_id_is_validated(client):
    assert client.put("/api/scans/..%2Fevil", json={"name": "x"}).status_code in (400, 404)


def test_reference(client):
    ref = client.get("/api/reference").json()
    assert any(f["name"] == "sma" for f in ref["functions"])


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "InkScreener" in r.text
