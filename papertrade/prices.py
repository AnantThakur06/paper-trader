"""Price data. Yahoo Finance via yfinance (free, NSE about 15 min delayed)."""
import logging
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import config
from .engine import Candle
from .timeutil import IST, is_session_open_ts, now_utc, to_ist, to_utc_naive

log = logging.getLogger(__name__)


@dataclass
class Quote:
    price: float
    ts: datetime  # naive UTC time of the candle the price came from
    prev_close: float | None = None  # yesterday's close, for the day's change


@dataclass
class IndexSnap:
    name: str
    value: float
    change: float
    change_pct: float
    series: list  # today's closes, for the small chart
    ts: datetime  # naive UTC time of the latest value


INDICES = {"NIFTY 50": "^NSEI", "NIFTY BANK": "^NSEBANK",
           "SENSEX": "^BSESN", "INDIA VIX": "^INDIAVIX"}


class PriceError(Exception):
    pass


def yahoo_symbol(symbol: str) -> str:
    return f"{symbol}.NS"


def normalize_symbol(raw: str) -> str:
    s = (raw or "").strip().upper().replace(" ", "")
    for suffix in (".NS", ".NSE"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    if s.startswith("NSE:"):
        s = s[4:]
    return s


def _prev_close(df):
    """Close of the last candle before the newest day in the data."""
    last_day = df.index[-1].date()
    before = df[[ts.date() < last_day for ts in df.index]]
    return round(float(before["Close"].iloc[-1]), 2) if len(before) else None


def final_only(candles: list[Candle], interval_min: int, now: datetime) -> list[Candle]:
    """Drop the newest candle if it might still be forming (delayed data)."""
    if not candles:
        return candles
    last = candles[-1]
    ready_at = last.ts + timedelta(minutes=interval_min + config.LAST_CANDLE_GRACE_MIN)
    if now < ready_at:
        return candles[:-1]
    return candles


class YahooPrices:
    def __init__(self, cache_seconds: int = 45):
        self.cache_seconds = cache_seconds
        self._cache: dict = {}

    # -- raw download -------------------------------------------------------
    def _history(self, symbol: str, raw: bool = False, **kwargs):
        key = (symbol, raw, tuple(sorted(kwargs.items())))
        hit = self._cache.get(key)
        if hit and _time.monotonic() - hit[0] < self.cache_seconds:
            return hit[1]
        import yfinance as yf  # imported here so tests don't need internet

        try:
            df = yf.Ticker(symbol if raw else yahoo_symbol(symbol)).history(
                auto_adjust=False, actions=False, prepost=False, **kwargs
            )
        except Exception as e:  # network trouble, rate limits, bad symbol
            raise PriceError(f"Yahoo data failed for {symbol}: {e}") from e
        if df is None or df.empty:
            raise PriceError(f"No price data from Yahoo for {symbol}")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        self._cache[key] = (_time.monotonic(), df)
        return df

    # -- public API -------------------------------------------------------------
    def quote(self, symbol: str) -> Quote:
        df = self._history(symbol, period="5d", interval="1m")
        ts = df.index[-1]
        return Quote(price=round(float(df["Close"].iloc[-1]), 2), ts=to_utc_naive(ts),
                     prev_close=_prev_close(df))

    def index_snapshot(self, name: str, ticker: str) -> IndexSnap:
        df = self._history(ticker, raw=True, period="5d", interval="15m")
        last_day = df.index[-1].date()
        today = df[[ts.date() == last_day for ts in df.index]]
        value = float(today["Close"].iloc[-1])
        prev = _prev_close(df) or float(today["Open"].iloc[0])
        return IndexSnap(name, round(value, 2), round(value - prev, 2),
                         round((value / prev - 1) * 100, 2),
                         [round(float(x), 2) for x in today["Close"]], to_utc_naive(df.index[-1]))

    def candles(self, symbol: str, since: datetime, now: datetime | None = None) -> list[Candle]:
        """Finished candles that started after `since` (naive UTC).

        Uses 1-minute candles when possible (last 7 days), then 5-minute
        (last ~2 months), then daily, so a long gap can still be caught up.
        """
        now = now or now_utc()
        age = now - since
        if age <= timedelta(days=7):
            interval, minutes = "1m", 1
        elif age <= timedelta(days=58):
            interval, minutes = "5m", 5
        else:
            interval, minutes = "1d", 24 * 60

        start = to_ist(since).date().isoformat()
        df = self._history(symbol, start=start, interval=interval)

        out = []
        for ts, row in df.iterrows():
            ts_utc = to_utc_naive(ts)
            if interval == "1d":
                # daily bars are stamped at midnight; treat the bar as the whole day
                if ts.date() <= to_ist(since).date():
                    continue
                if ts.date() >= to_ist(now).date():
                    continue  # today's daily bar isn't finished
                session_open = True
            else:
                if ts_utc <= since:
                    continue
                session_open = is_session_open_ts(ts_utc)
            out.append(Candle(ts_utc, float(row["Open"]), float(row["High"]),
                              float(row["Low"]), float(row["Close"]), session_open))
        if interval != "1d":
            out = final_only(out, minutes, now)
        return out


class FakePrices:
    """Used in tests and demo mode. You set the candles and quotes yourself."""

    def __init__(self):
        self.series: dict[str, list[Candle]] = {}
        self.quotes: dict[str, Quote] = {}
        self.fail: set[str] = set()
        self.indices: dict = {}  # ticker -> (value, change, series)

    def set_quote(self, symbol, price, ts):
        self.quotes[symbol] = Quote(price, ts)

    def add_candle(self, symbol, ts, o, h, l, c):
        self.series.setdefault(symbol, []).append(
            Candle(ts, o, h, l, c, is_session_open_ts(ts)))
        self.series[symbol].sort(key=lambda x: x.ts)

    def quote(self, symbol):
        if symbol in self.fail or symbol not in self.quotes:
            raise PriceError(f"No price data for {symbol}")
        return self.quotes[symbol]

    def index_snapshot(self, name, ticker):
        if ticker not in self.indices:
            raise PriceError(f"No index data for {name}")
        value, change, series = self.indices[ticker]
        return IndexSnap(name, value, change, round(change / (value - change) * 100, 2),
                         series, now_utc())

    def candles(self, symbol, since, now=None):
        if symbol in self.fail:
            raise PriceError(f"No price data for {symbol}")
        now = now or now_utc()
        out = [c for c in self.series.get(symbol, []) if c.ts > since]
        return final_only(out, 1, now)


__all__ = ["YahooPrices", "FakePrices", "Quote", "IndexSnap", "INDICES", "PriceError",
           "normalize_symbol", "IST"]
