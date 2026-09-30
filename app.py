"""Paper Trader: the screen. Run locally with:  streamlit run app.py"""
import hmac
import os
import time
from datetime import datetime

import pandas as pd
import streamlit as st

from papertrade import TradeError, build_broker, config
from papertrade.charges import delivery_charges
from papertrade.prices import FakePrices, normalize_symbol
from papertrade.service import REASON_TEXT, rs
from papertrade.timeutil import fmt_ist, in_market_hours, minutes_between, now_utc

st.set_page_config(page_title="Paper Trader", page_icon="📒", layout="wide")

PROFIT, LOSS = "#1E7B45", "#B3261E"


def secret(name):
    try:
        return st.secrets[name]
    except Exception:
        return os.environ.get(name)


def demo_prices():
    """Offline demo prices (set PAPER_DEMO=1). Not used normally."""
    p = FakePrices()
    now = now_utc()
    for sym, price in {"RELIANCE": 1412.5, "SBIN": 812.3, "INFY": 1520.0, "TMPV": 402.1}.items():
        p.set_quote(sym, price, now)
    return p


@st.cache_resource
def get_broker():
    prices = demo_prices() if secret("PAPER_DEMO") == "1" else None
    return build_broker(secret("DATABASE_URL"), prices=prices)


def require_password():
    """If APP_PASSWORD is set in secrets, ask for it once per browser session."""
    expected = secret("APP_PASSWORD")
    if not expected or st.session_state.get("authed"):
        return
    st.title("Paper Trader")
    pw = st.text_input("Password", type="password")
    if pw:
        if hmac.compare_digest(pw, str(expected)):
            st.session_state["authed"] = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()


require_password()
broker = get_broker()


# ------------------------------------------------------------------ helpers
def flash(kind, msg):
    st.session_state["flash"] = (kind, msg)
    st.rerun()


def act(fn, *args):
    try:
        flash("success", fn(*args))
    except TradeError as e:
        st.error(str(e))


def run_sync(force=False):
    last = st.session_state.get("last_sync", 0)
    if not force and time.time() - last < 60:
        return
    with st.spinner("Checking your trades against the market…"):
        msgs, errs = broker.sync()
    st.session_state["last_sync"] = time.time()
    st.session_state["sync_errors"] = errs
    for m in msgs:
        st.toast(m, duration="long")


def color_pnl(v):
    if isinstance(v, (int, float)):
        if v > 0:
            return f"color: {PROFIT}; font-weight: 600"
        if v < 0:
            return f"color: {LOSS}; font-weight: 600"
    return ""


def money_table(df, pnl_cols, money_cols):
    fmt = {c: "₹{:,.2f}" for c in money_cols + pnl_cols}
    return df.style.format(fmt, na_rep="").map(color_pnl, subset=pnl_cols)


def what_happened(e):
    if e["reason"] == "MANUAL":
        return "Profit booked" if e["net_pnl"] > 0 else "Sold manually (loss)"
    text = REASON_TEXT[e["reason"]]
    if e["reason"] in ("SL", "GAP") and e["sl_at_exit"]:
        text += f" (SL {rs(e['sl_at_exit'])})"
    return text


# ------------------------------------------------------------------ header
head, button = st.columns([4, 1], vertical_alignment="bottom")
with head:
    st.title("Paper Trader")
    st.caption("NSE swing trades with virtual money. Prices come from Yahoo Finance "
               "and are about 15 minutes behind the live market.")
with button:
    if st.button("Check trades now", width="stretch"):
        run_sync(force=True)

run_sync()

if "flash" in st.session_state:
    kind, msg = st.session_state.pop("flash")
    getattr(st, kind)(msg)

for err in st.session_state.get("sync_errors", []):
    st.warning(f"Couldn't check some prices right now: {err}. Your trades will be checked on the next try.")

open_rows = broker.trades_with_status("OPEN")
pending_rows = broker.trades_with_status("PENDING")
queued = [r for r in open_rows if r["pending_sell_qty"]]
quotes = broker.quotes_for([r["symbol"] for r in open_rows])
acct = broker.account(quotes)
now = now_utc()

# ------------------------------------------------------------------ balance
m1, m2, m3, m4 = st.columns(4)
m1.metric("Available cash", rs(acct["available"]), border=True,
          help="Money you can use for new orders.")
m2.metric("Locked in trades", rs(acct["locked"]), border=True,
          help="Money in open trades (at buy price) plus money blocked for pending orders.")
pct = (acct["open_pnl"] / acct["invested"] * 100) if acct["invested"] else 0.0
m3.metric("Open P&L", rs(acct["open_pnl"]), delta=f"{pct:+.2f}%" if acct["invested"] else None,
          border=True, help="Profit or loss on shares you still hold, before sell charges.")
m4.metric("Total value", rs(acct["total"]), delta=rs(acct["total"] - acct["start"]), border=True,
          help="Cash + current value of your shares. The small number is change since you started.")

last_run = broker.get_meta("watchman_last_run")
if last_run:
    ran = datetime.fromisoformat(last_run)
    status = broker.get_meta("watchman_last_status", "")
    line = f"Auto-check last ran at {fmt_ist(ran)} IST ({status})."
    if in_market_hours(now) and minutes_between(ran, now) > 30:
        st.warning(line + " That's over 30 minutes ago, so GitHub may be running late. "
                          "This page is still correct: the app checks your trades whenever you open it.")
    else:
        st.caption(line)
else:
    st.caption("Auto-check hasn't run yet (see README, step 4). "
               "Until it does, your trades are checked whenever you open this app.")

tab_order, tab_open, tab_pending, tab_history = st.tabs(
    ["Place order", f"Open trades ({len(open_rows)})",
     f"Pending ({len(pending_rows) + len(queued)})", "History"])

# ------------------------------------------------------------------ place order
with tab_order:
    left, right = st.columns([3, 2], gap="large")
    with left:
        symbol = normalize_symbol(st.text_input("Stock symbol (NSE)", placeholder="RELIANCE"))
        quote = broker.quote(symbol) if symbol else None
        live = broker.is_live(quote) if quote else False
        if symbol and not quote:
            st.error(f"Couldn't find {symbol} on NSE. Check the spelling "
                     "(examples: RELIANCE, TMPV, SBIN, M&M).")
        elif quote:
            state = "Market open" if live else "Market closed"
            st.markdown(f"**{symbol}** last price **{rs(quote.price)}** "
                        f"at {fmt_ist(quote.ts)} IST. {state}.")

        order_type = st.radio(
            "Order type", ["Market", "Limit"], horizontal=True,
            help="Market buys now at the latest price (or at the next open if the market is "
                 "closed). Limit waits until the price comes down to your level.")
        c1, c2 = st.columns(2)
        qty = c1.number_input("Quantity", min_value=1, step=1, value=1)
        limit = None
        if order_type == "Limit":
            limit = c2.number_input("Limit price (₹)", min_value=0.01, step=0.05, format="%.2f",
                                    value=float(quote.price) if quote else None)
        sl = st.number_input("Stop loss (₹)", min_value=0.01, step=0.05, format="%.2f",
                             value=None, placeholder="Required")
        setup = st.selectbox("Setup (optional)", ["Not set"] + config.SETUPS)
        note = st.text_input("Why this trade? (optional)", max_chars=120,
                             placeholder="Breakout above 1450 resistance")

    with right:
        st.subheader("Order preview")
        ready = bool(quote and sl and (order_type == "Market" or limit))
        if not ready:
            st.caption("Fill in the stock, quantity and stop loss to see the cost and your risk.")
        else:
            fills_now = live and (order_type == "Market" or quote.price <= limit)
            entry = quote.price if order_type == "Market" or fills_now else limit
            buy_ch = delivery_charges("BUY", qty, entry)["total"]
            cost = qty * entry + buy_ch
            sell_ch = delivery_charges("SELL", qty, sl, include_dp=True)["total"]
            risk = (entry - sl) * qty + buy_ch + sell_ch
            risk_pct = risk / acct["total"] * 100 if acct["total"] else 0
            preview = pd.DataFrame({
                "": ["Buy price", "Trade value", "Buy charges", "Total cost",
                     "Loss if stop loss hits", "Cash left after"],
                "Amount": [rs(entry), rs(qty * entry), rs(buy_ch), rs(cost),
                           f"{rs(risk)} ({risk_pct:.1f}% of account)",
                           rs(acct["available"] - cost)],
            })
            st.dataframe(preview, hide_index=True, width="stretch")
            if order_type == "Market" and not live:
                st.info("The market is closed, so this buys at the next open price. "
                        f"{config.AMO_BUFFER_PCT:.0f}% extra is blocked in case it opens higher.")
            elif order_type == "Limit" and not fills_now:
                st.info(f"This waits until {symbol} trades at {rs(limit)} or lower.")
            if sl >= entry:
                st.error("Stop loss must be below the buy price.")
            elif risk_pct > 2:
                st.caption("Many swing traders keep the loss per trade to 1–2% of the account.")

        if st.button("Place buy order", type="primary", disabled=not ready, width="stretch"):
            act(broker.place_buy, symbol, qty, order_type.upper(), sl, limit,
                None if setup == "Not set" else setup, note)

# ------------------------------------------------------------------ open trades
with tab_open:
    if not open_rows:
        st.info("No open trades. Place a buy order to start.")
    else:
        rows = []
        for r in open_rows:
            q = quotes.get(r["symbol"])
            cur = q.price if q else None
            pnl = (cur - r["entry_price"]) * r["qty_open"] if cur else None
            rows.append({
                "Stock": r["symbol"], "Qty": r["qty_open"], "Bought @": r["entry_price"],
                "Current": cur, "Stop loss": r["sl_price"], "P&L": pnl,
                "P&L %": (cur / r["entry_price"] - 1) * 100 if cur else None,
                "Bought on": fmt_ist(r["entry_time"]), "Setup": r["setup"] or "",
                "Note": r["note"] or "",
                "Queued sell": f"{r['pending_sell_qty']} at next open" if r["pending_sell_qty"] else "",
            })
        df = pd.DataFrame(rows)
        styled = money_table(df, ["P&L"], ["Bought @", "Current", "Stop loss"]) \
            .format({"P&L %": "{:+.2f}%"}, na_rep="")
        st.dataframe(styled, hide_index=True, width="stretch")
        st.caption("P&L here is before sell charges. Current price is about 15 minutes delayed.")

        st.subheader("Manage a trade")
        options = {f"{r['symbol']}: {r['qty_open']} shares @ {rs(r['entry_price'])} (#{r['id']})": r
                   for r in open_rows}
        r = options[st.selectbox("Trade", list(options))]
        a, b = st.columns(2, gap="large")
        with a:
            new_sl = st.number_input("New stop loss (₹)", value=float(r["sl_price"]), step=0.05,
                                     format="%.2f", key=f"sl_{r['id']}")
            if st.button("Update stop loss", key=f"slbtn_{r['id']}", width="stretch"):
                act(broker.move_sl, r["id"], new_sl)
        with b:
            free = r["qty_open"] - r["pending_sell_qty"]
            if free > 0:
                sell_qty = st.number_input("Shares to sell", min_value=1, max_value=free, value=free,
                                           step=1, key=f"q_{r['id']}")
                if st.button("Sell shares", key=f"sell_{r['id']}", type="primary", width="stretch"):
                    act(broker.sell, r["id"], sell_qty)
            else:
                st.caption("All shares are already queued to sell at the next open.")

# ------------------------------------------------------------------ pending
with tab_pending:
    if not pending_rows and not queued:
        st.info("Nothing pending. Limit orders, and orders placed while the market is closed, show up here.")
    if pending_rows:
        st.subheader("Buy orders waiting to fill")
        df = pd.DataFrame([{
            "Stock": r["symbol"], "Qty": r["qty"],
            "Type": "Limit" if r["order_type"] == "LIMIT" else "Market (next open)",
            "Limit price": r["limit_price"], "Stop loss": r["sl_price"],
            "Money blocked": r["reserve_amount"], "Placed": fmt_ist(r["created_at"]),
        } for r in pending_rows])
        st.dataframe(money_table(df, [], ["Limit price", "Stop loss", "Money blocked"]),
                     hide_index=True, width="stretch")
        opts = {f"{r['symbol']}: {r['qty']} shares (#{r['id']})": r for r in pending_rows}
        c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
        pick = opts[c1.selectbox("Order", list(opts), key="cancel_pick")]
        if c2.button("Cancel order", width="stretch"):
            act(broker.cancel_order, pick["id"])
    if queued:
        st.subheader("Sells waiting for the next open")
        for r in queued:
            c1, c2 = st.columns([3, 1], vertical_alignment="center")
            c1.write(f"**{r['symbol']}**: sell {r['pending_sell_qty']} shares at the next open "
                     f"(asked {fmt_ist(r['pending_sell_at'])})")
            if c2.button("Cancel sell", key=f"cq_{r['id']}", width="stretch"):
                act(broker.cancel_queued_sell, r["id"])

# ------------------------------------------------------------------ history
with tab_history:
    s = broker.stats()
    exits_rows = broker.exits_with_trades()
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Closed trades", s["closed_trades"], border=True)
    h2.metric("Win rate", f"{s['win_rate']:.0f}%", border=True,
              help=f"{s['wins']} wins, {s['losses']} losses (after charges).")
    h3.metric("Net P&L", rs(s["net"]), border=True, help="Realised profit/loss after all charges.")
    h4.metric("Charges paid", rs(s["charges"]), border=True)

    if not exits_rows:
        st.info("No sells yet. Every buy and sell will be recorded here.")
    else:
        hist = pd.DataFrame([{
            "Stock": e["symbol"], "Qty": e["qty"],
            "Bought @": e["entry_price"],
            "Order": "Limit" if e["order_type"] == "LIMIT" else "Market",
            "Buy time": fmt_ist(e["entry_time"]), "Sold @": e["price"],
            "Sell time": fmt_ist(e["time"]), "What happened": what_happened(e),
            "Gross P&L": e["gross_pnl"], "Charges": e["sell_charges"] + e["buy_charges_alloc"],
            "Net P&L": e["net_pnl"], "Setup": e["setup"] or "", "Note": e["note"] or "",
        } for e in reversed(exits_rows)])
        st.dataframe(money_table(hist, ["Gross P&L", "Net P&L"], ["Bought @", "Sold @", "Charges"]),
                     hide_index=True, width="stretch")
        st.caption(f"Average win {rs(s['avg_win'])}, average loss {rs(s['avg_loss'])}. "
                   "A partial sell shows as its own row.")
        st.download_button("Download history (CSV)", hist.to_csv(index=False).encode(),
                           file_name="paper_trades_history.csv", mime="text/csv")

        with st.expander("Results by setup"):
            if s["by_setup"]:
                st.dataframe(money_table(pd.DataFrame([
                    {"Setup": k, "Trades": v["trades"], "Wins": v["wins"],
                     "Win rate": f"{v['wins'] / v['trades'] * 100:.0f}%", "Net P&L": v["net"]}
                    for k, v in s["by_setup"].items()]), ["Net P&L"], []),
                    hide_index=True, width="stretch")
            else:
                st.caption("Appears once you have closed trades.")

    with st.expander("Stop-loss changes"):
        log = broker.sl_log()
        if log:
            st.dataframe(pd.DataFrame([{"Stock": x["symbol"], "From": rs(x["old_sl"]),
                                        "To": rs(x["new_sl"]), "When": fmt_ist(x["changed_at"])}
                                       for x in log]), hide_index=True, width="stretch")
        else:
            st.caption("No stop-loss changes yet.")

    with st.expander("Cancelled and rejected orders"):
        dead = broker.trades_with_status("CANCELLED", "REJECTED")
        if dead:
            st.dataframe(pd.DataFrame([{"Stock": d["symbol"], "Qty": d["qty"],
                                        "Status": d["status"].title(),
                                        "Placed": fmt_ist(d["created_at"]),
                                        "Reason": d["status_note"] or ""} for d in dead]),
                         hide_index=True, width="stretch")
        else:
            st.caption("None.")

    with st.expander("Reset account"):
        st.write(f"Deletes every trade and the full history, and sets your balance back to "
                 f"{rs(config.STARTING_BALANCE)}. This can't be undone.")
        confirm = st.text_input("Type RESET to confirm", key="reset_confirm")
        if st.button("Reset account", disabled=confirm != "RESET"):
            act(broker.reset)
