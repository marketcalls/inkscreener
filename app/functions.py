"""Vectorised indicator functions on wide frames (rows = dates, columns = symbols).

Every function keeps NaN where there is not enough history, so a symbol without enough
bars simply does not match instead of producing a wrong value.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

Frame = pd.DataFrame


def sma(x: Frame, n: int) -> Frame:
    return x.rolling(n).mean()


def ema(x: Frame, n: int) -> Frame:
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def rma(x: Frame, n: int) -> Frame:
    """Wilder's moving average (used by RSI, ATR, ADX)."""
    return x.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def wma(x: Frame, n: int) -> Frame:
    total = sum((n - i) * x.shift(i) for i in range(n))
    return total / (n * (n + 1) / 2)


def rolling_max(x: Frame, n: int) -> Frame:
    return x.rolling(n).max()


def rolling_min(x: Frame, n: int) -> Frame:
    return x.rolling(n).min()


def rolling_sum(x: Frame, n: int) -> Frame:
    return x.rolling(n).sum()


def rolling_std(x: Frame, n: int) -> Frame:
    return x.rolling(n).std(ddof=0)


def rsi(x: Frame, n: int) -> Frame:
    delta = x.diff()
    gain = rma(delta.clip(lower=0), n)
    loss = rma((-delta).clip(lower=0), n)
    out = 100 - 100 / (1 + gain / loss)
    return out.where(loss != 0, 100.0).where(gain.notna() & loss.notna())


def true_range(high: Frame, low: Frame, close: Frame) -> Frame:
    prev = close.shift(1)
    hl = (high - low).to_numpy()
    hc = (high - prev).abs().to_numpy()
    lc = (low - prev).abs().to_numpy()
    tr = np.fmax(hl, np.fmax(hc, lc))  # fmax ignores the NaN previous close on the first bar
    tr[np.isnan(hl)] = np.nan
    return pd.DataFrame(tr, index=close.index, columns=close.columns)


def atr(high: Frame, low: Frame, close: Frame, n: int) -> Frame:
    return rma(true_range(high, low, close), n)


def _directional(high: Frame, low: Frame, close: Frame, n: int) -> tuple[Frame, Frame, Frame]:
    up = high.diff()
    down = -low.diff()
    known = up.notna() & down.notna()
    plus_dm = up.where((up > down) & (up > 0), 0.0).where(known)
    minus_dm = down.where((down > up) & (down > 0), 0.0).where(known)
    atr_n = atr(high, low, close, n)
    plus_di = 100 * rma(plus_dm, n) / atr_n
    minus_di = 100 * rma(minus_dm, n) / atr_n
    return plus_di, minus_di, atr_n


def plus_di(high: Frame, low: Frame, close: Frame, n: int) -> Frame:
    return _directional(high, low, close, n)[0]


def minus_di(high: Frame, low: Frame, close: Frame, n: int) -> Frame:
    return _directional(high, low, close, n)[1]


def adx(high: Frame, low: Frame, close: Frame, n: int) -> Frame:
    p, m, _ = _directional(high, low, close, n)
    dx = 100 * (p - m).abs() / (p + m)
    return rma(dx, n)


def supertrend(high: Frame, low: Frame, close: Frame, n: int, mult: float) -> Frame:
    """Supertrend line. Path dependent, so it loops over dates but stays vectorised across symbols."""
    h, l, c = high.to_numpy(), low.to_numpy(), close.to_numpy()
    atr_n = atr(high, low, close, n).to_numpy()
    hl2 = (h + l) / 2
    upper = hl2 + mult * atr_n
    lower = hl2 - mult * atr_n

    rows, cols = c.shape
    out = np.full((rows, cols), np.nan)
    prev_u = np.full(cols, np.nan)
    prev_l = np.full(cols, np.nan)
    prev_c = np.full(cols, np.nan)
    prev_trend = np.zeros(cols)  # 1 = up, -1 = down, 0 = not started

    with np.errstate(invalid="ignore"):
        for t in range(rows):
            u, lo, ct = upper[t], lower[t], c[t]
            valid = ~np.isnan(u) & ~np.isnan(ct)
            keep_u = ~np.isnan(prev_u) & (u >= prev_u) & (prev_c <= prev_u)
            keep_l = ~np.isnan(prev_l) & (lo <= prev_l) & (prev_c >= prev_l)
            fu = np.where(keep_u, prev_u, u)
            fl = np.where(keep_l, prev_l, lo)
            trend = np.where(
                prev_trend == 0,
                np.where(ct >= hl2[t], 1.0, -1.0),
                np.where(
                    (prev_trend == -1) & (ct > fu),
                    1.0,
                    np.where((prev_trend == 1) & (ct < fl), -1.0, prev_trend),
                ),
            )
            trend = np.where(valid, trend, 0.0)
            out[t] = np.where(trend == 1, fl, np.where(trend == -1, fu, np.nan))
            prev_u = np.where(valid, fu, np.nan)
            prev_l = np.where(valid, fl, np.nan)
            prev_c = ct
            prev_trend = trend
    return pd.DataFrame(out, index=close.index, columns=close.columns)


def macd_line(x: Frame, fast: int, slow: int) -> Frame:
    return ema(x, fast) - ema(x, slow)


def macd_signal(x: Frame, fast: int, slow: int, signal: int) -> Frame:
    return ema(macd_line(x, fast, slow), signal)


def macd_hist(x: Frame, fast: int, slow: int, signal: int) -> Frame:
    line = macd_line(x, fast, slow)
    return line - ema(line, signal)


def bb_upper(x: Frame, n: int, k: float) -> Frame:
    return sma(x, n) + k * rolling_std(x, n)


def bb_lower(x: Frame, n: int, k: float) -> Frame:
    return sma(x, n) - k * rolling_std(x, n)


def stoch_k(high: Frame, low: Frame, close: Frame, n: int) -> Frame:
    lowest = low.rolling(n).min()
    highest = high.rolling(n).max()
    rng = (highest - lowest).replace(0, np.nan)
    return 100 * (close - lowest) / rng


def stoch_d(high: Frame, low: Frame, close: Frame, n: int, d: int) -> Frame:
    return sma(stoch_k(high, low, close, n), d)
