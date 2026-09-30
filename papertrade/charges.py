"""Charges for NSE equity delivery trades (buy today, sell another day)."""
from decimal import ROUND_HALF_UP, Decimal

from . import config


def _round(x: float, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


def delivery_charges(side: str, qty: int, price: float, include_dp: bool = False,
                     cfg: dict | None = None) -> dict:
    """Return a breakdown of charges for one executed order.

    side: "BUY" or "SELL". include_dp: charge the DP fee (only on the first
    sell of a stock on a given day).
    """
    cfg = cfg or config.CHARGES
    turnover = qty * price

    brokerage = turnover * cfg["brokerage_pct"] / 100
    if cfg["brokerage_cap"]:
        brokerage = min(brokerage, cfg["brokerage_cap"])
    stt = _round(turnover * cfg["stt_pct"] / 100, 0)
    exchange = turnover * cfg["exchange_pct"] / 100
    sebi = turnover * cfg["sebi_per_crore"] / 1e7
    stamp = turnover * cfg["stamp_pct_buy"] / 100 if side == "BUY" else 0.0
    gst = (brokerage + exchange + sebi) * cfg["gst_pct"] / 100
    dp = cfg["dp_per_scrip"] if (side == "SELL" and include_dp) else 0.0

    parts = {
        "brokerage": _round(brokerage),
        "stt": stt,
        "exchange": _round(exchange),
        "sebi": _round(sebi),
        "stamp": _round(stamp),
        "gst": _round(gst),
        "dp": _round(dp),
    }
    parts["total"] = _round(sum(parts.values()))
    return parts
