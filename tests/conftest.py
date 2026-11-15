import os
from datetime import datetime

os.environ.setdefault("INKSCREENER_AUTO_DOWNLOAD", "0")  # tests never call NSE

import numpy as np
import pandas as pd
import pytest

from app.datastore import DataStore, MarketData


def make_data(closes: dict[str, list[float]], volumes: dict[str, list[float]] | None = None,
              bench: list[float] | None = None) -> MarketData:
    """Small synthetic MarketData. NaN in a close list means 'no bar that day'."""
    n = len(next(iter(closes.values())))
    dates = pd.bdate_range("2024-01-01", periods=n)
    close = pd.DataFrame(closes, index=dates, dtype=float)
    open_ = close.shift(1).fillna(close)
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    if volumes is None:
        volume = pd.DataFrame(1000.0, index=dates, columns=close.columns).where(close.notna())
    else:
        volume = pd.DataFrame(volumes, index=dates, dtype=float)
    frames = {"open": open_, "high": high, "low": low, "close": close, "volume": volume}
    benchmark = {}
    if bench is not None:
        b = pd.Series(bench, index=dates, dtype=float)
        benchmark = {"open": b, "high": b * 1.01, "low": b * 0.99, "close": b, "volume": b * 0}
    return MarketData(frames=frames, benchmark=benchmark, loaded_at=datetime.now(), source="test")


@pytest.fixture
def store_factory():
    def _make(*args, **kwargs) -> DataStore:
        store = DataStore()
        store.set(make_data(*args, **kwargs))
        return store
    return _make
