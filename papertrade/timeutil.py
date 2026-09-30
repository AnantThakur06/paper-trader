"""Time helpers.

Rule used everywhere: store times as UTC without timezone info,
show them to the user in IST.
"""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(ts) -> datetime:
    """Accepts a tz-aware datetime / pandas Timestamp and returns naive UTC."""
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def to_ist(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(IST)


def ist_date(dt: datetime):
    return to_ist(dt).date()


def fmt_ist(dt, with_date=True) -> str:
    if dt is None:
        return ""
    local = to_ist(dt)
    return local.strftime("%d %b, %H:%M") if with_date else local.strftime("%H:%M")


def in_market_hours(now: datetime) -> bool:
    local = to_ist(now)
    return local.weekday() < 5 and MARKET_OPEN <= local.time() < MARKET_CLOSE


def is_session_open_ts(ts_utc: datetime) -> bool:
    """True for the first candle of the trading day (09:15 IST)."""
    return to_ist(ts_utc).time() == MARKET_OPEN


def minutes_between(a: datetime, b: datetime) -> float:
    return (b - a) / timedelta(minutes=1)
