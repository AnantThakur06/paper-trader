"""The paper broker. Every action the app or the watchman can take lives here."""
from datetime import datetime

from sqlalchemy import delete, func, select

from . import config
from .charges import delivery_charges
from .db import exits, ledger, locked, meta, sl_changes, trades
from .engine import TradeState, process
from .prices import PriceError, normalize_symbol
from .timeutil import in_market_hours, ist_date, minutes_between, now_utc

REASON_TEXT = {
    "MANUAL": "Sold manually",
    "MANUAL_OPEN": "Sold at next open",
    "SL": "SL hit",
    "GAP": "Gap down below SL",
}


class TradeError(Exception):
    """A problem the user should see (bad input, not enough cash, ...)."""


def rs(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}₹{abs(x):,.2f}"


class PaperBroker:
    def __init__(self, engine, prices, now_fn=now_utc):
        self.engine = engine
        self.prices = prices
        self.now = now_fn

    # ------------------------------------------------------------------ prices
    def quote(self, symbol):
        try:
            return self.prices.quote(symbol)
        except PriceError:
            return None

    def is_live(self, quote) -> bool:
        """Is the market open right now, with a fresh price for this stock?"""
        now = self.now()
        return (
            quote is not None
            and in_market_hours(now)
            and ist_date(quote.ts) == ist_date(now)
            and minutes_between(quote.ts, now) <= config.LIVE_PRICE_MAX_AGE_MIN
        )

    def quotes_for(self, symbols) -> dict:
        out = {}
        for s in set(symbols):
            q = self.quote(s)
            if q:
                out[s] = q
        return out

    # ------------------------------------------------------------------ money
    @staticmethod
    def _cash(conn) -> float:
        return float(conn.execute(select(func.coalesce(func.sum(ledger.c.amount), 0.0))).scalar())

    @staticmethod
    def _reserved(conn) -> float:
        q = select(func.coalesce(func.sum(trades.c.reserve_amount), 0.0)).where(
            trades.c.status == "PENDING")
        return float(conn.execute(q).scalar())

    def account(self, quotes: dict | None = None) -> dict:
        quotes = quotes or {}
        with self.engine.connect() as conn:
            cash = self._cash(conn)
            reserved = self._reserved(conn)
            open_rows = conn.execute(select(trades).where(trades.c.status == "OPEN")).mappings().all()
            start = float(conn.execute(select(func.coalesce(func.sum(ledger.c.amount), 0.0))
                                       .where(ledger.c.kind == "DEPOSIT")).scalar())
        invested = sum(r["qty_open"] * r["entry_price"] for r in open_rows)
        market_value = sum(
            r["qty_open"] * (quotes[r["symbol"]].price if r["symbol"] in quotes else r["entry_price"])
            for r in open_rows)
        return {
            "cash": cash,
            "available": cash - reserved,
            "reserved": reserved,
            "invested": invested,
            "locked": reserved + invested,
            "open_pnl": market_value - invested,
            "total": cash + market_value,
            "start": start,
        }

    # ------------------------------------------------------------------ orders
    def place_buy(self, raw_symbol, qty, order_type, sl, limit_price=None,
                  setup=None, note=None) -> str:
        symbol = normalize_symbol(raw_symbol)
        if not symbol:
            raise TradeError("Enter a stock symbol, like RELIANCE.")
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise TradeError("Quantity must be a whole number.")
        if qty < 1:
            raise TradeError("Quantity must be at least 1.")
        if order_type not in ("MARKET", "LIMIT"):
            raise TradeError("Order type must be Market or Limit.")
        if not sl or sl <= 0:
            raise TradeError("Every trade needs a stop loss.")
        if order_type == "LIMIT" and (not limit_price or limit_price <= 0):
            raise TradeError("Enter the limit price you want to buy at.")

        quote = self.quote(symbol)
        if quote is None:
            raise TradeError(f"Couldn't get a price for {symbol} on NSE. "
                             "Check the symbol (examples: RELIANCE, TMPV, SBIN, M&M).")
        now = self.now()
        live = self.is_live(quote)
        fill_now = live and (order_type == "MARKET" or quote.price <= limit_price)
        entry_ref = quote.price if (order_type == "MARKET" or fill_now) else limit_price
        if sl >= entry_ref:
            raise TradeError(f"Stop loss {rs(sl)} must be below your buy price {rs(entry_ref)}.")

        with locked(self.engine) as conn:
            available = self._cash(conn) - self._reserved(conn)
            base = dict(symbol=symbol, qty=qty, order_type=order_type,
                        limit_price=limit_price if order_type == "LIMIT" else None,
                        sl_price=float(sl), setup=setup or None, note=(note or "").strip() or None,
                        created_at=now)
            if fill_now:
                price = quote.price
                ch = delivery_charges("BUY", qty, price)["total"]
                cost = qty * price + ch
                if cost > available + 1e-6:
                    raise TradeError(f"Not enough cash. This order needs {rs(cost)}, "
                                     f"you have {rs(available)} available.")
                tid = conn.execute(trades.insert().values(
                    **base, status="OPEN", qty_open=qty, entry_price=price, entry_time=now,
                    ref_ts=quote.ts, buy_charges=ch, reserve_amount=0.0,
                    pending_sell_qty=0)).inserted_primary_key[0]
                conn.execute(ledger.insert().values(ts=now, kind="BUY", amount=-cost, trade_id=tid,
                                                    note=f"Buy {qty} {symbol} @ {price}"))
                return f"Bought {qty} {symbol} at {rs(price)}. Charges {rs(ch)}."

            if order_type == "MARKET":
                est_price = quote.price * (1 + config.AMO_BUFFER_PCT / 100)
            else:
                est_price = limit_price
            reserve = qty * est_price + delivery_charges("BUY", qty, est_price)["total"]
            if reserve > available + 1e-6:
                raise TradeError(f"Not enough cash. This order blocks {rs(reserve)}, "
                                 f"you have {rs(available)} available.")
            conn.execute(trades.insert().values(
                **base, status="PENDING", qty_open=0, reserve_amount=reserve, ref_ts=now,
                buy_charges=0.0, pending_sell_qty=0))
            if order_type == "MARKET":
                return (f"Market is closed. Order saved: {qty} {symbol} will be bought at the "
                        f"next market open price. {rs(reserve)} blocked until then.")
            return (f"Limit order placed: buys {qty} {symbol} if the price comes to "
                    f"{rs(limit_price)} or lower. {rs(reserve)} blocked.")

    def cancel_order(self, trade_id: int) -> str:
        self.sync([trade_id])
        with locked(self.engine) as conn:
            r = self._get(conn, trade_id)
            if r["status"] != "PENDING":
                raise TradeError(f"Can't cancel: this order is already {r['status'].lower()}.")
            conn.execute(trades.update().where(trades.c.id == trade_id).values(
                status="CANCELLED", reserve_amount=0.0, closed_at=self.now()))
        return f"Cancelled the {r['symbol']} order. Blocked money is back in your available cash."

    def sell(self, trade_id: int, qty: int) -> str:
        self.sync([trade_id])
        with self.engine.connect() as conn:
            symbol = self._get(conn, trade_id)["symbol"]
        quote = self.quote(symbol)
        live = self.is_live(quote)
        now = self.now()
        with locked(self.engine) as conn:
            r = self._get(conn, trade_id)
            if r["status"] != "OPEN":
                raise TradeError(self._closed_reason(conn, r))
            free = r["qty_open"] - r["pending_sell_qty"]
            qty = int(qty)
            if qty < 1 or qty > free:
                raise TradeError(f"You can sell between 1 and {free} shares of {symbol}.")
            if live:
                net = self._record_exit(conn, r, qty, quote.price, now, "MANUAL", None)
                left = r["qty_open"] - qty
                conn.execute(trades.update().where(trades.c.id == trade_id).values(
                    qty_open=left, status="CLOSED" if left == 0 else "OPEN",
                    closed_at=now if left == 0 else None))
                return f"Sold {qty} {symbol} at {rs(quote.price)}. Net P&L on these shares: {rs(net)}."
            conn.execute(trades.update().where(trades.c.id == trade_id).values(
                pending_sell_qty=r["pending_sell_qty"] + qty, pending_sell_at=now))
        return (f"Market is closed. {qty} {symbol} will be sold at the next open price "
                "(your stop loss stays active for the rest).")

    def cancel_queued_sell(self, trade_id: int) -> str:
        with locked(self.engine) as conn:
            r = self._get(conn, trade_id)
            if not r["pending_sell_qty"]:
                raise TradeError("There's no queued sell for this trade.")
            conn.execute(trades.update().where(trades.c.id == trade_id).values(
                pending_sell_qty=0, pending_sell_at=None))
        return f"Cancelled the queued sell for {r['symbol']}."

    def move_sl(self, trade_id: int, new_sl: float) -> str:
        self.sync([trade_id])
        with self.engine.connect() as conn:
            r = self._get(conn, trade_id)
        quote = self.quote(r["symbol"])
        with locked(self.engine) as conn:
            r = self._get(conn, trade_id)
            if r["status"] not in ("OPEN", "PENDING"):
                raise TradeError(self._closed_reason(conn, r))
            if not new_sl or new_sl <= 0:
                raise TradeError("Enter a valid stop loss price.")
            if r["status"] == "PENDING" and r["order_type"] == "LIMIT":
                ref, what = r["limit_price"], "limit price"
            else:
                ref, what = (quote.price if quote else None), "current price"
            if ref is not None and new_sl >= ref:
                raise TradeError(f"Stop loss must be below the {what} {rs(ref)}.")
            if abs(new_sl - r["sl_price"]) < 1e-9:
                return "Stop loss is already at that price."
            conn.execute(sl_changes.insert().values(trade_id=trade_id, old_sl=r["sl_price"],
                                                    new_sl=float(new_sl), changed_at=self.now()))
            conn.execute(trades.update().where(trades.c.id == trade_id).values(sl_price=float(new_sl)))
        return f"{r['symbol']} stop loss moved from {rs(r['sl_price'])} to {rs(new_sl)}."

    # ------------------------------------------------------------ the watchman
    def sync(self, trade_ids=None) -> tuple[list[str], list[str]]:
        """Check pending/open trades against new candles. Returns (messages, errors)."""
        now = self.now()
        q = select(trades).where(trades.c.status.in_(["PENDING", "OPEN"])).order_by(trades.c.id)
        if trade_ids:
            q = q.where(trades.c.id.in_(list(trade_ids)))
        with self.engine.connect() as conn:
            rows = conn.execute(q).mappings().all()
        if not rows:
            return [], []

        since_by_symbol: dict[str, datetime] = {}
        for r in rows:
            since = r["last_candle_ts"] or r["ref_ts"]
            s = r["symbol"]
            since_by_symbol[s] = min(since_by_symbol.get(s, since), since)

        candles, errors = {}, []
        for symbol, since in since_by_symbol.items():
            try:
                candles[symbol] = self.prices.candles(symbol, since, now)
            except PriceError as e:
                errors.append(str(e))

        messages = []
        with locked(self.engine) as conn:
            for r in conn.execute(q).mappings().all():  # re-read inside the lock
                cs = candles.get(r["symbol"])
                if cs is None:
                    continue
                state = self._state(conn, r)
                events = process(state, cs)
                messages += self._apply(conn, r, state, events)
        return messages, errors

    def _state(self, conn, r) -> TradeState:
        changes = conn.execute(select(sl_changes).where(sl_changes.c.trade_id == r["id"])
                               .order_by(sl_changes.c.changed_at, sl_changes.c.id)).mappings().all()
        first_sl = changes[0]["old_sl"] if changes else r["sl_price"]
        history = [(datetime.min, first_sl)] + [(c["changed_at"], c["new_sl"]) for c in changes]
        return TradeState(
            status=r["status"], order_type=r["order_type"], qty=r["qty"], qty_open=r["qty_open"],
            limit_price=r["limit_price"], sl_history=history,
            pending_sell_qty=r["pending_sell_qty"], pending_sell_at=r["pending_sell_at"],
            ref_ts=r["ref_ts"], last_candle_ts=r["last_candle_ts"], entry_price=r["entry_price"])

    def _apply(self, conn, r, state: TradeState, events) -> list[str]:
        msgs = []
        tid, symbol = r["id"], r["symbol"]
        closed_at = None
        for ev in events:
            if ev.kind == "FILL":
                ch = delivery_charges("BUY", ev.qty, ev.price)["total"]
                cost = ev.qty * ev.price + ch
                available = self._cash(conn) - self._reserved(conn) + r["reserve_amount"]
                if cost > available + 1e-6:
                    conn.execute(trades.update().where(trades.c.id == tid).values(
                        status="REJECTED", reserve_amount=0.0, closed_at=ev.ts,
                        status_note=f"Not enough cash when the order filled: needed {rs(cost)}, "
                                    f"had {rs(available)}."))
                    msgs.append(f"{symbol} order rejected: not enough cash at {rs(ev.price)}.")
                    return msgs
                conn.execute(ledger.insert().values(ts=ev.ts, kind="BUY", amount=-cost, trade_id=tid,
                                                    note=f"Buy {ev.qty} {symbol} @ {ev.price}"))
                conn.execute(trades.update().where(trades.c.id == tid).values(
                    status="OPEN", qty_open=ev.qty, entry_price=ev.price, entry_time=ev.ts,
                    buy_charges=ch, reserve_amount=0.0))
                r = self._get(conn, tid)
                msgs.append(f"{symbol} order filled: bought {ev.qty} at {rs(ev.price)}.")
            else:
                net = self._record_exit(conn, r, ev.qty, ev.price, ev.ts, ev.reason, ev.sl)
                conn.execute(trades.update().where(trades.c.id == tid).values(
                    qty_open=r["qty_open"] - ev.qty))
                r = self._get(conn, tid)
                closed_at = ev.ts
                msgs.append(f"{symbol}: {REASON_TEXT[ev.reason]} — sold {ev.qty} at {rs(ev.price)} "
                            f"(net {rs(net)}).")

        values = dict(status=state.status, qty_open=state.qty_open,
                      last_candle_ts=state.last_candle_ts,
                      pending_sell_qty=state.pending_sell_qty,
                      pending_sell_at=state.pending_sell_at)
        if state.status == "CLOSED":
            values["closed_at"] = closed_at
        conn.execute(trades.update().where(trades.c.id == tid).values(**values))
        return msgs

    def _record_exit(self, conn, r, qty, price, when, reason, sl) -> float:
        """Write one sell: charges, P&L, cash back. Returns net P&L."""
        symbol = r["symbol"]
        dp_times = conn.execute(
            select(exits.c.time).join(trades, trades.c.id == exits.c.trade_id)
            .where(trades.c.symbol == symbol, exits.c.dp_charged.is_(True))).scalars().all()
        dp_needed = all(ist_date(t) != ist_date(when) for t in dp_times)
        ch = delivery_charges("SELL", qty, price, include_dp=dp_needed)["total"]

        allocated = float(conn.execute(select(func.coalesce(func.sum(exits.c.buy_charges_alloc), 0.0))
                                       .where(exits.c.trade_id == r["id"])).scalar())
        if qty >= r["qty_open"]:            # last shares take whatever is left
            alloc = r["buy_charges"] - allocated
        else:
            alloc = r["buy_charges"] * qty / r["qty"]
        gross = (price - r["entry_price"]) * qty
        net = gross - ch - alloc
        conn.execute(exits.insert().values(
            trade_id=r["id"], qty=qty, price=price, time=when, reason=reason, sl_at_exit=sl,
            gross_pnl=round(gross, 2), sell_charges=ch, buy_charges_alloc=round(alloc, 4),
            net_pnl=round(net, 2), dp_charged=dp_needed))
        conn.execute(ledger.insert().values(ts=when, kind="SELL", amount=qty * price - ch,
                                            trade_id=r["id"], note=f"Sell {qty} {symbol} @ {price}"))
        return net

    # ------------------------------------------------------------------ reading
    @staticmethod
    def _get(conn, trade_id):
        r = conn.execute(select(trades).where(trades.c.id == trade_id)).mappings().first()
        if r is None:
            raise TradeError(f"Trade #{trade_id} not found.")
        return r

    @staticmethod
    def _closed_reason(conn, r) -> str:
        if r["status"] == "CLOSED":
            last = conn.execute(select(exits).where(exits.c.trade_id == r["id"])
                                .order_by(exits.c.id.desc())).mappings().first()
            if last:
                return (f"This {r['symbol']} trade is already closed "
                        f"({REASON_TEXT[last['reason']]} at {rs(last['price'])}).")
        return f"This {r['symbol']} trade is {r['status'].lower()}."

    def trades_with_status(self, *statuses):
        with self.engine.connect() as conn:
            return conn.execute(select(trades).where(trades.c.status.in_(statuses))
                                .order_by(trades.c.id)).mappings().all()

    def exits_with_trades(self):
        q = (select(exits, trades.c.symbol, trades.c.entry_price, trades.c.entry_time,
                    trades.c.order_type, trades.c.setup, trades.c.note, trades.c.status)
             .join(trades, trades.c.id == exits.c.trade_id).order_by(exits.c.time, exits.c.id))
        with self.engine.connect() as conn:
            return conn.execute(q).mappings().all()

    def sl_log(self):
        q = (select(sl_changes, trades.c.symbol).join(trades, trades.c.id == sl_changes.c.trade_id)
             .order_by(sl_changes.c.changed_at.desc()))
        with self.engine.connect() as conn:
            return conn.execute(q).mappings().all()

    def stats(self) -> dict:
        rows = self.exits_with_trades()
        per_trade: dict[int, dict] = {}
        for e in rows:
            t = per_trade.setdefault(e["trade_id"], {"net": 0.0, "setup": e["setup"] or "Not set",
                                                     "closed": e["status"] == "CLOSED"})
            t["net"] += e["net_pnl"]
        closed = [t for t in per_trade.values() if t["closed"]]
        wins = [t for t in closed if t["net"] > 0]
        losses = [t for t in closed if t["net"] <= 0]
        by_setup: dict[str, dict] = {}
        for t in closed:
            s = by_setup.setdefault(t["setup"], {"trades": 0, "wins": 0, "net": 0.0})
            s["trades"] += 1
            s["wins"] += t["net"] > 0
            s["net"] += t["net"]
        return {
            "closed_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(closed) * 100) if closed else 0.0,
            "avg_win": (sum(t["net"] for t in wins) / len(wins)) if wins else 0.0,
            "avg_loss": (sum(t["net"] for t in losses) / len(losses)) if losses else 0.0,
            "gross": sum(e["gross_pnl"] for e in rows),
            "charges": sum(e["sell_charges"] + e["buy_charges_alloc"] for e in rows),
            "net": sum(e["net_pnl"] for e in rows),
            "by_setup": by_setup,
        }

    def completed_trades(self) -> list[dict]:
        """One row per finished trade (all its sells combined), newest first."""
        moves: dict[int, list] = {}
        for m in reversed(self.sl_log()):          # oldest change first
            moves.setdefault(m["trade_id"], []).append(m)
        grouped: dict[int, list] = {}
        for e in self.exits_with_trades():
            if e["status"] == "CLOSED":
                grouped.setdefault(e["trade_id"], []).append(e)

        out = []
        for tid, ex in grouped.items():
            first, last = ex[0], ex[-1]
            qty = sum(x["qty"] for x in ex)
            net = sum(x["net_pnl"] for x in ex)
            mv = moves.get(tid, [])
            reasons = {x["reason"] for x in ex}
            manual = {"MANUAL", "MANUAL_OPEN"}
            if last["reason"] == "GAP":
                result, kind = "Gap down below SL", "loss"
                detail = f"SL was {rs(last['sl_at_exit'])}, opened {rs(last['price'])}"
            elif last["reason"] == "SL":
                if mv and last["price"] >= first["entry_price"]:
                    result, kind = "Trailing SL hit, in profit", "trail"
                    detail = f"SL moved {rs(mv[0]['old_sl'])} → {rs(mv[-1]['new_sl'])}"
                else:
                    result, kind = f"SL hit at {rs(last['price'])}", "loss"
                    detail = ""
            else:
                result, kind = ("Profit booked", "win") if net > 0 else ("Sold at a loss", "loss")
                detail = ""
            if reasons & manual and last["reason"] in ("SL", "GAP"):
                result = "Part booked, then " + (result if result.startswith("SL")
                                                 else result[0].lower() + result[1:])
            if len(ex) > 1 and not detail:
                detail = f"{len(ex)} sells"
            out.append({
                "trade_id": tid, "symbol": first["symbol"], "setup": first["setup"],
                "note": first["note"], "order_type": first["order_type"], "qty": qty,
                "entry_price": first["entry_price"], "entry_time": first["entry_time"],
                "sell_avg": sum(x["qty"] * x["price"] for x in ex) / qty,
                "sold_at": last["time"],
                "held_days": (ist_date(last["time"]) - ist_date(first["entry_time"])).days,
                "result": result, "kind": kind, "detail": detail,
                "gross": sum(x["gross_pnl"] for x in ex),
                "charges": sum(x["sell_charges"] + x["buy_charges_alloc"] for x in ex),
                "net": net, "exits": ex,
            })
        out.sort(key=lambda r: r["sold_at"], reverse=True)
        return out

    # ------------------------------------------------------------------ misc
    def get_meta(self, key, default=None):
        with self.engine.connect() as conn:
            v = conn.execute(select(meta.c.value).where(meta.c.key == key)).scalar()
        return default if v is None else v

    def set_meta(self, key, value):
        with locked(self.engine) as conn:
            conn.execute(delete(meta).where(meta.c.key == key))
            conn.execute(meta.insert().values(key=key, value=str(value)))

    def reset(self, amount: float = config.STARTING_BALANCE) -> str:
        with locked(self.engine) as conn:
            for table in (exits, sl_changes, ledger, trades):
                conn.execute(delete(table))
            conn.execute(ledger.insert().values(ts=self.now(), kind="DEPOSIT", amount=amount,
                                                note="Starting balance (reset)"))
        return f"Account reset. Balance is {rs(amount)} again."
