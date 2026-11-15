"""Paths and settings. Every value can be overridden with an environment variable or a .env file."""

import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR.parent  # repository root

load_dotenv(BASE_DIR / ".env")

# Default: an OpenAlgo checkout next to this repository (../openalgo/db/historify.duckdb).
HISTORIFY_DB_PATH = Path(
    os.getenv("HISTORIFY_DB_PATH", BASE_DIR.parent / "openalgo" / "db" / "historify.duckdb")
).expanduser()
SCANS_DIR = Path(os.getenv("INKSCREENER_SCANS_DIR", BASE_DIR / "scans"))
INDICES_DIR = Path(os.getenv("INKSCREENER_INDICES_DIR", BASE_DIR / "data" / "indices"))
STATIC_DIR = APP_DIR / "static"

# Download the Nifty index lists from NSE on first start when none are present.
AUTO_DOWNLOAD_INDICES = os.getenv("INKSCREENER_AUTO_DOWNLOAD", "1") != "0"

# Symbol Historify uses for the Nifty 50 index (exchange NSE_INDEX).
BENCHMARK_SYMBOL = os.getenv("INKSCREENER_BENCHMARK", "NIFTY")
