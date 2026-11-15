"""Integration tests against the real Historify database. Skipped when it is not available."""

import pytest

from app.config import HISTORIFY_DB_PATH
from app.datastore import DataStore
from app.scan import list_scans, load_scan, run_scan
from app.universes import local_path

pytestmark = pytest.mark.skipif(not HISTORIFY_DB_PATH.exists(), reason="Historify database not found")


@pytest.fixture(scope="module")
def store():
    s = DataStore()
    s.load()
    return s


def test_data_shape(store):
    summary = store.data.summary()
    assert summary["symbols"] > 400
    assert summary["benchmark"] == "NIFTY"


@pytest.mark.skipif(not local_path("nifty500").exists(), reason="Nifty 500 list not downloaded")
def test_short_term_breakouts_matches_chartink(store):
    """On 2026-11-13 Chartink's 'Short term breakouts' returned 21 cash-segment stocks.
    Of those, MCX, MEESHO, ATHERENERG, PNB and NAVINFLUOR are in Historify, and they
    should be exactly what this scan returns in the Nifty 500 universe."""
    if store.data.dates[-1] < __import__("pandas").Timestamp("2026-11-13"):
        pytest.skip("Historify data ends before 2026-11-13")
    r = run_scan(load_scan("short_term_breakouts"), store, as_of="2026-11-13", universe="nifty500")
    assert [row["symbol"] for row in r["rows"]] == ["MCX", "MEESHO", "ATHERENERG", "PNB", "NAVINFLUOR"]


@pytest.mark.skipif(not local_path("nifty500").exists(), reason="Index lists not downloaded")
def test_every_saved_scan_runs(store):
    for s in list_scans():
        r = run_scan(load_scan(s["id"]), store)
        assert r["active"] > 0, s["id"]
