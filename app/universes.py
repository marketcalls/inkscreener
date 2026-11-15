"""Index constituent lists (Nifty 50 / 100 / 200 / 500) downloaded from NSE archives.

NSE publishes each list as a CSV with the columns:
Company Name, Industry, Symbol, Series, ISIN Code
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from .config import INDICES_DIR

NSE_ARCHIVE_HOSTS = (
    "https://nsearchives.nseindia.com/content/indices/",
    "https://archives.nseindia.com/content/indices/",
)

# universe id -> (display name, NSE file name)
INDEX_FILES: dict[str, tuple[str, str]] = {
    "nifty50": ("Nifty 50", "ind_nifty50list.csv"),
    "nifty100": ("Nifty 100", "ind_nifty100list.csv"),
    "nifty200": ("Nifty 200", "ind_nifty200list.csv"),
    "nifty500": ("Nifty 500", "ind_nifty500list.csv"),
}

ALL_UNIVERSE = "historify"

# NSE rejects requests without a browser-like User-Agent. A generic one is used on purpose.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/csv,text/plain,*/*",
}

_EXPECTED_COLUMNS = {"Company Name", "Industry", "Symbol"}


@dataclass
class Constituent:
    symbol: str
    company: str
    industry: str


def _parse(text: str) -> list[Constituent]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not _EXPECTED_COLUMNS.issubset({f.strip() for f in reader.fieldnames}):
        raise ValueError(f"Unexpected NSE CSV header: {reader.fieldnames}")
    rows = []
    for raw in reader:
        row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        if row.get("Symbol"):
            rows.append(Constituent(row["Symbol"], row.get("Company Name", ""), row.get("Industry", "")))
    if not rows:
        raise ValueError("NSE CSV contained no symbols")
    return rows


def local_path(universe_id: str) -> Path:
    return INDICES_DIR / f"{universe_id}.csv"


def download_all(timeout: float = 20.0) -> dict[str, dict]:
    """Download every index list from NSE and save it under data/indices/<id>.csv."""
    INDICES_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, dict] = {}
    with httpx.Client(headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
        for uid, (name, filename) in INDEX_FILES.items():
            errors = []
            for host in NSE_ARCHIVE_HOSTS:
                url = host + filename
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    text = resp.content.decode("utf-8-sig")
                    rows = _parse(text)
                    local_path(uid).write_text(text, encoding="utf-8")
                    report[uid] = {"name": name, "url": url, "count": len(rows), "status": "ok"}
                    break
                except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
                    errors.append(f"{url}: {exc}")
            else:
                report[uid] = {"name": name, "status": "error", "errors": errors}
    return report


def load(universe_id: str) -> list[Constituent]:
    path = local_path(universe_id)
    if not path.exists():
        raise FileNotFoundError(
            f"{path.name} not found. Click 'Update index lists' in the header to download the index lists from NSE."
        )
    return _parse(path.read_text(encoding="utf-8-sig"))


def industry_map() -> dict[str, str]:
    """Symbol -> NSE industry, taken from the widest list available."""
    for uid in ("nifty500", "nifty200", "nifty100", "nifty50"):
        if local_path(uid).exists():
            return {c.symbol: c.industry for c in load(uid)}
    return {}


def list_universes() -> list[dict]:
    out = [{"id": ALL_UNIVERSE, "name": "All Historify symbols", "available": True, "updated": None}]
    for uid, (name, _) in INDEX_FILES.items():
        path = local_path(uid)
        out.append(
            {
                "id": uid,
                "name": name,
                "available": path.exists(),
                "updated": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
                if path.exists()
                else None,
            }
        )
    return out


if __name__ == "__main__":
    for uid, info in download_all().items():
        print(uid, info)
