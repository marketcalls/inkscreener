"""Load Historify daily OHLCV into wide frames (rows = trading dates, columns = symbols).

The Historify DuckDB file is opened read-only, read in one query, and closed straight away
so OpenAlgo can keep writing to it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from .config import BENCHMARK_SYMBOL, HISTORIFY_DB_PATH

FIELDS = ("open", "high", "low", "close", "volume")

_QUERY = """
SELECT symbol, exchange, timestamp // 86400 AS day_number,
       open, high, low, close, volume
FROM market_data
WHERE interval = 'D' AND exchange IN ('NSE', 'NSE_INDEX')
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY symbol, exchange, timestamp // 86400 ORDER BY timestamp DESC
) = 1
"""


@dataclass
class MarketData:
    """Wide daily frames for a set of symbols plus the benchmark index series."""

    frames: dict[str, pd.DataFrame]  # field -> DataFrame (date x symbol)
    benchmark: dict[str, pd.Series]  # field -> Series (date), may be empty
    loaded_at: datetime
    source: str
    _broadcast_cache: dict[str, pd.DataFrame] = field(default_factory=dict, repr=False)

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.frames["close"].index

    @property
    def symbols(self) -> list[str]:
        return list(self.frames["close"].columns)

    def subset(self, symbols: list[str]) -> "MarketData":
        cols = [s for s in symbols if s in self.frames["close"].columns]
        return MarketData(
            frames={k: v[cols] for k, v in self.frames.items()},
            benchmark=self.benchmark,
            loaded_at=self.loaded_at,
            source=self.source,
        )

    def benchmark_frame(self, name: str) -> pd.DataFrame:
        """Benchmark field repeated across every symbol column (for expressions like nifty.close)."""
        if not self.benchmark:
            raise KeyError(f"Benchmark {BENCHMARK_SYMBOL} is not in the Historify data")
        if name not in self._broadcast_cache:
            series = self.benchmark[name].reindex(self.dates)
            values = np.repeat(series.to_numpy(dtype=float)[:, None], len(self.symbols), axis=1)
            self._broadcast_cache[name] = pd.DataFrame(values, index=self.dates, columns=self.symbols)
        return self._broadcast_cache[name]

    def summary(self) -> dict:
        close = self.frames["close"]
        last = close.index[-1]
        return {
            "source": self.source,
            "loaded_at": self.loaded_at.isoformat(timespec="seconds"),
            "symbols": len(self.symbols),
            "first_date": close.index[0].date().isoformat(),
            "last_date": last.date().isoformat(),
            "symbols_on_last_date": int(close.loc[last].notna().sum()),
            "trading_days": len(close.index),
            "benchmark": BENCHMARK_SYMBOL if self.benchmark else None,
        }


def _read_rows(retries: int = 5) -> pd.DataFrame:
    import duckdb

    if not HISTORIFY_DB_PATH.exists():
        raise FileNotFoundError(f"Historify database not found at {HISTORIFY_DB_PATH}")
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            con = duckdb.connect(str(HISTORIFY_DB_PATH), read_only=True)
            try:
                return con.execute(_QUERY).df()
            finally:
                con.close()
        except duckdb.IOException as exc:  # file locked by a writer
            last_error = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Historify database is busy, try again shortly: {last_error}")


def build(rows: pd.DataFrame, source: str) -> MarketData:
    """Turn long OHLCV rows into wide frames on a single trading calendar."""
    rows = rows.copy()
    rows["date"] = pd.to_datetime(rows["day_number"], unit="D")

    bench_rows = rows[(rows["exchange"] == "NSE_INDEX") & (rows["symbol"] == BENCHMARK_SYMBOL)]
    stock_rows = rows[rows["exchange"] == "NSE"]

    # Trading calendar: the benchmark's dates. Without a benchmark, use dates on which at
    # least 20% of symbols traded (drops stray rows that would break rolling windows).
    if not bench_rows.empty:
        calendar = pd.DatetimeIndex(sorted(bench_rows["date"].unique()))
    else:
        per_day = stock_rows.groupby("date")["symbol"].count()
        calendar = pd.DatetimeIndex(sorted(per_day[per_day >= 0.2 * stock_rows["symbol"].nunique()].index))

    frames = {
        f: stock_rows.pivot(index="date", columns="symbol", values=f).reindex(calendar).astype(float)
        for f in FIELDS
    }
    for f in FIELDS:
        frames[f] = frames[f].sort_index(axis=1)
        frames[f].index.name = "date"

    benchmark = {}
    if not bench_rows.empty:
        b = bench_rows.set_index("date").sort_index()
        benchmark = {f: b[f].astype(float).reindex(calendar) for f in FIELDS}

    return MarketData(frames=frames, benchmark=benchmark, loaded_at=datetime.now(), source=source)


class DataStore:
    """Holds the loaded data and per-universe subsets. Thread-safe reload."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: MarketData | None = None
        self._subsets: dict[tuple, MarketData] = {}
        self.error: str | None = None

    def load(self) -> MarketData:
        rows = _read_rows()
        data = build(rows, source=f"duckdb:{HISTORIFY_DB_PATH}")
        with self._lock:
            self._data = data
            self._subsets = {}
            self.error = None
        return data

    def set(self, data: MarketData) -> None:
        """Inject data directly (used by tests)."""
        with self._lock:
            self._data = data
            self._subsets = {}
            self.error = None

    @property
    def data(self) -> MarketData:
        if self._data is None:
            raise RuntimeError(self.error or "Historify data is not loaded yet")
        return self._data

    @property
    def loaded(self) -> bool:
        return self._data is not None

    def for_symbols(self, key: str, symbols: list[str] | None) -> MarketData:
        data = self.data
        if symbols is None:
            return data
        cache_key = (key, id(data))
        with self._lock:
            if cache_key not in self._subsets:
                self._subsets[cache_key] = data.subset(symbols)
            return self._subsets[cache_key]

    def clear_subsets(self) -> None:
        with self._lock:
            self._subsets = {}
