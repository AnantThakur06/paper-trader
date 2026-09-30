"""Trade page: place orders, manage active trades, see pending orders."""
import streamlit as st

import ui
from papertrade import config
from papertrade.charges import delivery_charges
from papertrade.prices import normalize_symbol
from papertrade.service import rs
from papertrade.timeutil import fmt_ist, ist_date

ctx = st.session_state["ctx"]
broker, acct = ctx["broker"], ctx["acct"]

ui.index_strip(st.session_state.get("indices", []))


# ------------------------------------------------------------------ helpers
def total_risk(q, entry, sl):
    """Loss if the SL hits, including buy and sell charges."""
    return ((entry - sl) * q + delivery_charges("BUY", q, entry)["total"]
            + delivery_charges("SELL", q, sl, include_dp=True)["total"])


def qty_for_risk(risk, entry, sl, max_q):
    if not risk or entry <= sl:
        return 0
    q = min(int(risk // (entry - sl)), max_q)
    while q > 0 and total_risk(q, entry, sl) > risk:
        q -= 1
    return q


def set_state(key, value):
    st.session_state[key] = value


def apply_quick_sl(sl_key, pick_key, entry):
    pick = st.session_state.get(pick_key)
    if pick:
        st.session_state[sl_key] = round(entry * (1 - int(pick[1]) / 100), 2)
    st.session_state[pick_key] = None


def rows_html(pairs):
    cells = "".join(
        f'<div style="display:flex;justify-content:space-between;gap:12px;padding:9px 0;'
        f'border-bottom:1px solid #EEF1F5"><span style="color:#4E5B6B">{k}</span>'
        f'<span style="text-align:right">{v}</span></div>' for k, v in pairs)
    return f'<div style="display:flex;flex-direction:column;font-size:14px">{cells}</div>'


# ------------------------------------------------------------------ place order + preview
left, right = st.columns([1.35, 1], gap="large")

with left, st.container(border=True):
    st.subheader("Place order")
    st.caption("Equity delivery on NSE. Buy first, sell later.")
    symbol = normalize_symbol(st.text_input("Stock", placeholder="RELIANCE", key="sym"))
    quote = broker.quote(symbol) if symbol else None
    live = broker.is_live(quote) if quote else False
    if symbol and not quote:
        st.error(f"Couldn't find {symbol} on NSE. Check the spelling (examples: RELIANCE, TMPV, SBIN, M&M).")
    elif quote:
        change = ""
        if quote.prev_close:
            d = quote.price - quote.prev_close
            change = " " + ui.pnl_md(d, d / quote.prev_close * 100)
        st.markdown(f"Last price **{rs(quote.price)}**{change} at "
                    f"{fmt_ist(quote.ts, with_date=False)} IST")

    order_type = st.segmented_control("Order type", ["Market", "Limit"], default="Market",
                                      key="otype", required=True)
    st.caption("Buys now at the latest price. When the market is closed, buys at the next open."
               if order_type == "Market" else "Waits until the price comes down to your limit.")

    limit = None
    if order_type == "Limit":
        limit_key = f"limit_{symbol}"
        st.session_state.setdefault(limit_key, float(quote.price) if quote else None)
        limit = st.number_input("Limit price (₹)", min_value=0.01, step=0.05, format="%.2f",
                                key=limit_key)

    fills_now = bool(quote) and live and (order_type == "Market" or bool(limit and quote.price <= limit))
    entry = (quote.price if quote and (order_type == "Market" or fills_now) else limit) or 0.0
    per_share = entry * (1 + config.AMO_BUFFER_PCT / 100) if (order_type == "Market" and not live) else entry
    max_q = int(acct["available"] // (per_share * 1.002)) if per_share else 0

    c1, c2 = st.columns(2)
    with c1:
        st.session_state.setdefault("qty", 1)
        qty = st.number_input("Quantity", min_value=1, step=1, key="qty")
        if entry:
            st.caption(f"You can afford up to {max_q:,}")
    sl_key, pick_key = f"sl_{symbol}", f"pick_{symbol}"
    with c2:
        st.session_state.setdefault(sl_key, None)
        sl = st.number_input("Stop loss (₹), required", min_value=0.01, step=0.05, format="%.2f",
                             placeholder="Required", key=sl_key)
        if entry:
            st.pills("Quick stop loss", ["−1%", "−2%", "−3%"], key=pick_key,
                     label_visibility="collapsed", on_change=apply_quick_sl,
                     args=(sl_key, pick_key, entry))
        if entry and sl and sl < entry:
            st.caption(f"{(entry - sl) / entry * 100:.2f}% below the buy price")

    with st.container(border=True):
        r1, r2, r3 = st.columns([1.3, 1, 1], vertical_alignment="bottom")
        risk_amt = r1.number_input("Or size by risk (₹)", min_value=0.0, step=100.0, value=None,
                                   placeholder="e.g. 300", key="risk_amt",
                                   help="Works out the quantity so an SL hit costs about this much, "
                                        "charges included.")
        if risk_amt and sl and entry and sl < entry:
            n = qty_for_risk(risk_amt, entry, sl, max_q)
            r2.markdown(f"= **{n:,} shares**")
            r3.button(f"Use {n:,}", on_click=set_state, args=("qty", max(n, 1)),
                      disabled=n < 1, width="stretch")
        elif risk_amt:
            r2.caption("Enter a stop loss first.")

    setup = st.pills("Setup (optional)", config.SETUPS, key="setup")
    note = st.text_input("Why this trade? (optional)", max_chars=120, key="note",
                         placeholder="Breakout above 1,400 with volume")

with right, st.container(border=True):
    head = st.container(horizontal=True, vertical_alignment="center")
    head.subheader("Order preview")
    head.badge("Paper trade, no real money", color="blue")
    ready = bool(quote and sl and entry)
    if not ready:
        st.caption("Fill in the stock, quantity and stop loss to see the cost and your risk.")
    else:
        bc = delivery_charges("BUY", qty, entry)
        cost = qty * entry + bc["total"]
        other = bc["brokerage"] + bc["exchange"] + bc["sebi"] + bc["gst"]
        if fills_now:
            fills = f"Now, {rs(entry)}"
        elif order_type == "Market":
            fills = f"Next market open (last {rs(entry)})"
        else:
            fills = f"When price reaches {rs(limit)} or lower"
        st.html(rows_html([
            ("Stock", f"<b>{symbol}</b> NSE"),
            ("Action", '<span style="padding:2px 10px;border-radius:6px;background:#E3F2EA;'
                       'color:#0E5A36;font-weight:700">BUY</span>'),
            ("Fills at", fills),
            ("Trade value", f"{qty:,} × {rs(entry)} = {rs(qty * entry)}"),
            ("Charges", f"{rs(bc['total'])}<br><span style='font-size:12px;color:#4E5B6B'>"
                        f"STT {rs(bc['stt'])}, stamp {rs(bc['stamp'])}, "
                        f"exchange + SEBI + GST {rs(other)}</span>"),
            ("<b style='color:#131C27'>Total cost</b>", f"<b>{rs(cost)}</b>"),
        ]))
        if sl >= entry:
            st.error("Stop loss must be below the buy price.")
        else:
            risk = total_risk(qty, entry, sl)
            risk_pct = risk / acct["total"] * 100 if acct["total"] else 0
            st.html(f"""
<div style="display:flex;flex-direction:column;gap:8px;padding:14px 16px;border-radius:12px;
background:#FDF1EF;border:1px solid #F4CFC9">
  <div style="display:flex;justify-content:space-between;align-items:baseline">
    <span style="font-size:14px;font-weight:600;color:#6E1D13">If stop loss hits</span>
    <span style="font-size:20px;font-weight:700;color:#A32F22">−{rs(risk)}</span></div>
  <div style="font-size:13px;color:#6E1D13">{risk_pct:.2f}% of your account, including charges.
  SL {rs(sl)} vs buy {rs(entry)}.</div>
</div>""")
            if risk_pct > 2:
                st.caption("Many swing traders keep the loss per trade to 1–2% of the account.")
        if order_type == "Market" and not live:
            st.info(f"The market is closed, so this buys at the next open price. "
                    f"{config.AMO_BUFFER_PCT:.0f}% extra is blocked in case it opens higher.")
        elif order_type == "Limit" and not fills_now:
            st.info(f"This waits until {symbol} trades at {rs(limit)} or lower.")
        st.markdown(f"Cash left after: **{rs(acct['available'] - cost)}**")
    if st.button("Place buy order", type="primary", disabled=not ready, width="stretch"):
        ui.act(broker.place_buy, symbol, qty, order_type.upper(), sl, limit, setup, note)

# ------------------------------------------------------------------ active trades
open_rows, quotes = ctx["open"], ctx["quotes"]
with st.container(border=True):
    head = st.columns([2, 1], vertical_alignment="center")
    head[0].subheader(f"Active trades ({len(open_rows)})")
    if open_rows:
        inv, op = acct["invested"], acct["open_pnl"]
        head[1].markdown(f"Open P&L {ui.pnl_md(op, op / inv * 100 if inv else 0)}")
        W = [1.4, 0.6, 0.9, 0.9, 1.3, 1.2, 1.1, 0.7, 1.5]
        for c, t in zip(st.columns(W), ["Stock", "Qty", "Bought @", "Now", "Stop loss",
                                        "Room to SL", "P&L", "Held", ""]):
            c.caption(t.upper())
        first_sl = {}
        for m in reversed(broker.sl_log()):
            first_sl.setdefault(m["trade_id"], m["old_sl"])
        for r in open_rows:
            q = quotes.get(r["symbol"])
            cur = q.price if q else None
            cols = st.columns(W, vertical_alignment="center")
            cols[0].markdown(f"**{r['symbol']}**  \n:gray[{r['setup'] or 'No setup'}]")
            cols[1].markdown(f"{r['qty_open']:,}")
            cols[2].markdown(rs(r["entry_price"]))
            cols[3].markdown(f"**{rs(cur)}**" if cur else ":gray[no price]")
            sl_text = rs(r["sl_price"])
            if r["id"] in first_sl:
                was = first_sl[r["id"]]
                word = "Trailed up" if r["sl_price"] > was else "Moved"
                sl_text += f"  \n:blue[{word} from {rs(was)}]"
            cols[4].markdown(sl_text)
            if cur:
                room = (cur - r["sl_price"]) / cur * 100
                with cols[5]:
                    st.progress(min(max(room / 8, 0.0), 1.0), text=f"{room:.1f}%")
                    if room < 2:
                        st.badge("Near SL", color="orange")
                pnl = (cur - r["entry_price"]) * r["qty_open"]
                cols[6].markdown(f"{ui.pnl_md(pnl)}  \n{(cur / r['entry_price'] - 1) * 100:+.2f}%")
            held = (ist_date(ctx["now"]) - ist_date(r["entry_time"])).days
            cols[7].markdown("Today" if held == 0 else f"{held} day{'s' if held != 1 else ''}")
            free = r["qty_open"] - r["pending_sell_qty"]
            with cols[8]:
                bx = st.container(horizontal=True)
                with bx.popover("Move SL"):
                    new_sl = st.number_input("New stop loss (₹)", value=float(r["sl_price"]), step=0.05,
                                             format="%.2f", key=f"mv_{r['id']}")
                    if st.button("Update stop loss", key=f"mvb_{r['id']}", type="primary", width="stretch"):
                        ui.act(broker.move_sl, r["id"], new_sl)
                with bx.popover("Sell", disabled=free < 1):
                    sq = st.number_input("Shares to sell", min_value=1, max_value=max(free, 1),
                                         value=max(free, 1), step=1, key=f"sq_{r['id']}")
                    if cur:
                        st.caption(f"About {ui.signed((cur - r['entry_price']) * sq)} before charges")
                    if st.button("Sell shares", key=f"sb_{r['id']}", type="primary", width="stretch"):
                        ui.act(broker.sell, r["id"], sq)
            if r["pending_sell_qty"]:
                st.caption(f"↳ {r['symbol']}: {r['pending_sell_qty']} shares will sell at the next open.")
        st.caption("P&L is before sell charges. Prices are about 15 minutes delayed.")
    else:
        st.info("No active trades. Place a buy order to start.")

# ------------------------------------------------------------------ pending
pending, queued = ctx["pending"], ctx["queued"]
with st.container(border=True):
    st.subheader(f"Pending orders ({len(pending) + len(queued)})")
    if not pending and not queued:
        st.caption("Nothing pending. Limit orders, and orders placed while the market is closed, show up here.")
    for r in pending:
        a, b = st.columns([5, 1], vertical_alignment="center")
        what = (f"Limit buy {r['qty']:,} @ {rs(r['limit_price'])}" if r["order_type"] == "LIMIT"
                else f"Buy {r['qty']:,} at the next open")
        a.markdown(f"**{r['symbol']}** · {what} · SL {rs(r['sl_price'])} · "
                   f"Blocked {rs(r['reserve_amount'])} · :gray[placed {fmt_ist(r['created_at'])}]")
        if b.button("Cancel", key=f"cx_{r['id']}", width="stretch"):
            ui.act(broker.cancel_order, r["id"])
    for r in queued:
        a, b = st.columns([5, 1], vertical_alignment="center")
        a.markdown(f"**{r['symbol']}** · Sell {r['pending_sell_qty']:,} at the next open · "
                   f":gray[asked {fmt_ist(r['pending_sell_at'])}]")
        if b.button("Cancel sell", key=f"cq_{r['id']}", width="stretch"):
            ui.act(broker.cancel_queued_sell, r["id"])
