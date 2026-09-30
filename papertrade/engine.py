"""The watchman's brain. Pure logic: no database, no internet.

Given one trade and the price candles that came after it, decide what
happened: did a pending order fill? did the stop loss hit? was there a gap?
"""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Candle:
    ts: datetime            # candle start, naive UTC
    open: float
    high: float
    low: float
    close: float
    session_open: bool = False  # first candle of the trading day (09:15 IST)


@dataclass
class TradeState:
    status: str                         # PENDING / OPEN / CLOSED
    order_type: str                     # MARKET / LIMIT
    qty: int
    qty_open: int
    limit_price: float | None
    # [(effective_from, sl_price), ...] sorted by time; first entry = original SL
    sl_history: list = field(default_factory=list)
    pending_sell_qty: int = 0
    pending_sell_at: datetime | None = None
    ref_ts: datetime | None = None      # only candles AFTER this time matter
    last_candle_ts: datetime | None = None
    entry_price: float | None = None


@dataclass
class Event:
    kind: str       # FILL or EXIT
    ts: datetime
    price: float
    qty: int
    reason: str     # FILL: LIMIT / NEXT_OPEN.  EXIT: SL / GAP / MANUAL_OPEN
    sl: float | None = None


def sl_at(sl_history: list, ts: datetime) -> float:
    """Stop loss that was active at time ts."""
    sl = sl_history[0][1]
    for effective_from, value in sl_history[1:]:
        if effective_from <= ts:
            sl = value
        else:
            break
    return sl


def process(state: TradeState, candles: list[Candle]) -> list[Event]:
    """Walk candles in time order and update `state` in place.

    Rules (long trades only):
    - Pending MARKET order (placed after hours) fills at the next candle's open.
    - Pending LIMIT buy fills at the open if the stock opens at/below the limit,
      otherwise at the limit price if the candle's low touches it.
    - Queued manual sell fills at the next candle's open.
    - Stop loss: if a candle OPENS at/below SL we exit at that open price
      (gap = real, bigger loss). Else if the LOW touches SL we exit at SL.
    - If a fill and an SL touch happen in the same candle we assume the SL
      hit too (we can't know the order inside a candle, so we stay cautious).
    """
    events: list[Event] = []
    start_after = state.last_candle_ts or state.ref_ts

    for c in candles:
        if start_after is not None and c.ts <= start_after:
            continue

        if state.status == "PENDING":
            fill_price = None
            reason = "LIMIT"
            if state.order_type == "MARKET":
                fill_price, reason = c.open, "NEXT_OPEN"
            elif c.open <= state.limit_price:
                fill_price = c.open
            elif c.low <= state.limit_price:
                fill_price = state.limit_price

            if fill_price is None:
                state.last_candle_ts = c.ts
                continue

            events.append(Event("FILL", c.ts, fill_price, state.qty, reason))
            state.status = "OPEN"
            state.qty_open = state.qty
            state.entry_price = fill_price

        if state.status == "OPEN":
            if state.pending_sell_qty and state.pending_sell_at and c.ts > state.pending_sell_at:
                q = min(state.pending_sell_qty, state.qty_open)
                events.append(Event("EXIT", c.ts, c.open, q, "MANUAL_OPEN"))
                state.qty_open -= q
                state.pending_sell_qty = 0
                state.pending_sell_at = None

            if state.qty_open > 0:
                sl = sl_at(state.sl_history, c.ts)
                if c.open <= sl:
                    reason = "GAP" if c.session_open else "SL"
                    events.append(Event("EXIT", c.ts, c.open, state.qty_open, reason, sl))
                    state.qty_open = 0
                elif c.low <= sl:
                    events.append(Event("EXIT", c.ts, sl, state.qty_open, "SL", sl))
                    state.qty_open = 0

            if state.qty_open == 0:
                state.status = "CLOSED"
                state.pending_sell_qty = 0
                state.last_candle_ts = c.ts
                break

        state.last_candle_ts = c.ts

    return events
