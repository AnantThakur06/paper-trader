"""History page: every finished trade, one row per trade, with stats."""
import pandas as pd
import streamlit as st

import ui
from papertrade.service import rs
from papertrade.timeutil import fmt_ist, ist_date

ctx = st.session_state["ctx"]
broker = ctx["broker"]
trades = broker.completed_trades()


def color_pnl(v):
    if isinstance(v, (int, float)):
        if v > 0:
            return f"color: {ui.PROFIT}; font-weight: 600"
        if v < 0:
            return f"color: {ui.LOSS}; font-weight: 600"
    return ""


def money_table(df, pnl_cols, money_cols):
    fmt = {c: "₹{:,.2f}" for c in money_cols + pnl_cols}
    return df.style.format(fmt, na_rep="").map(color_pnl, subset=pnl_cols)


def stat(col, label, value, sub=""):
    with col.container(border=True):
        st.metric(label, value)
        st.caption(sub or " ")


top = st.columns([3, 1], vertical_alignment="bottom")
top[0].title("History")
top[0].caption("Every finished trade, one row per trade. Active trades are on the Trade page.")

if not trades:
    st.info("No finished trades yet. When a trade is sold, by you or by the stop loss, "
            "it appears here with its full result.")
else:
    f1, f2, f3, f4 = st.columns(4)
    period = f1.selectbox("Period", ["All time", "This month", "Last 30 days", "Last 7 days"])
    stocks = f2.multiselect("Stocks", sorted({t["symbol"] for t in trades}), placeholder="All stocks")
    result = f3.selectbox("Result", ["Wins and losses", "Wins only", "Losses only"])
    setup = f4.selectbox("Setup", ["All setups"] + sorted({t["setup"] or "Not set" for t in trades}))
    today = ist_date(ctx["now"])

    def keep(t):
        d = ist_date(t["sold_at"])
        if period == "This month" and (d.year, d.month) != (today.year, today.month):
            return False
        if period == "Last 30 days" and (today - d).days > 30:
            return False
        if period == "Last 7 days" and (today - d).days > 7:
            return False
        if stocks and t["symbol"] not in stocks:
            return False
        if result == "Wins only" and t["net"] <= 0:
            return False
        if result == "Losses only" and t["net"] > 0:
            return False
        return setup == "All setups" or (t["setup"] or "Not set") == setup

    shown = [t for t in trades if keep(t)]
    wins = [t for t in shown if t["net"] > 0]
    losses = [t for t in shown if t["net"] <= 0]
    gross = sum(t["gross"] for t in shown)
    charges = sum(t["charges"] for t in shown)
    net = sum(t["net"] for t in shown)
    avg_w = sum(t["net"] for t in wins) / len(wins) if wins else 0.0
    avg_l = sum(t["net"] for t in losses) / len(losses) if losses else 0.0

    s = st.columns(5)
    stat(s[0], "Closed trades", f"{len(shown)}", f"{len(wins)} wins, {len(losses)} losses")
    stat(s[1], "Win rate", f"{len(wins) / len(shown) * 100:.0f}%" if shown else "–", "After charges")
    stat(s[2], "Net P&L", ui.signed(net), f"Gross {ui.signed(gross)}")
    stat(s[3], "Charges paid", rs(charges),
         f"{charges / gross * 100:.0f}% of your gross profit" if gross > 0 else "Paid on every buy and sell")
    with s[4].container(border=True):
        st.caption("Avg win / avg loss")
        st.markdown(f"{ui.pnl_md(avg_w)} / {ui.pnl_md(avg_l)}")
        st.caption(f"Wins are {avg_w / abs(avg_l):.2f}× your losses" if wins and losses and avg_l else " ")

    if not shown:
        st.info("No trades match these filters.")
    else:
        df = pd.DataFrame([{
            "Stock": t["symbol"], "Setup": t["setup"] or "",
            "Bought @": t["entry_price"], "Bought on": fmt_ist(t["entry_time"]),
            "Sold @": t["sell_avg"], "Sold on": fmt_ist(t["sold_at"]),
            "Held": "Same day" if t["held_days"] == 0 else f"{t['held_days']} days", "Qty": t["qty"],
            "What happened": t["result"] + (f" ({t['detail']})" if t["detail"] else ""),
            "Gross": t["gross"], "Charges": t["charges"], "Net": t["net"], "Note": t["note"] or "",
        } for t in shown])
        top[1].download_button("Download CSV", df.to_csv(index=False).encode(),
                               file_name="paper_trades_history.csv", mime="text/csv",
                               icon=":material/download:", width="stretch")

        left, right = st.columns([2.1, 1], gap="large")
        with left, st.container(border=True):
            st.subheader("Completed trades")
            st.dataframe(money_table(df, ["Gross", "Net"], ["Bought @", "Sold @", "Charges"]),
                         hide_index=True, width="stretch")
            st.caption("'Sold @' is the average price when a trade was sold in parts.")
            multi = [t for t in shown if len(t["exits"]) > 1]
            if multi:
                with st.expander(f"Trades sold in parts ({len(multi)})"):
                    for t in multi:
                        parts = ", ".join(f"{x['qty']} @ {rs(x['price'])} on {fmt_ist(x['time'])}"
                                          for x in t["exits"])
                        st.markdown(f"**{t['symbol']}**: {parts}")

        with right:
            with st.container(border=True):
                points, running = [0.0], 0.0
                for t in reversed(shown):          # oldest first
                    running += t["net"]
                    points.append(round(running, 2))
                st.markdown(f"**Net P&L over time** {ui.pnl_md(running)}")
                st.line_chart(pd.DataFrame({"Net P&L (₹)": points}), height=180)
                st.caption("Each point is one finished trade, oldest on the left.")
            with st.container(border=True):
                st.markdown("**Results by setup**")
                by = {}
                for t in shown:
                    b = by.setdefault(t["setup"] or "Not set", {"Trades": 0, "Wins": 0, "Net P&L": 0.0})
                    b["Trades"] += 1
                    b["Wins"] += t["net"] > 0
                    b["Net P&L"] += t["net"]
                sdf = pd.DataFrame([{"Setup": k, **v} for k, v in by.items()]).sort_values(
                    "Net P&L", ascending=False)
                st.dataframe(money_table(sdf, ["Net P&L"], []), hide_index=True, width="stretch")

with st.expander(f"Stop-loss changes ({len(log := broker.sl_log())})"):
    if log:
        st.dataframe(pd.DataFrame([{"Stock": x["symbol"], "From": rs(x["old_sl"]), "To": rs(x["new_sl"]),
                                    "When": fmt_ist(x["changed_at"])} for x in log]),
                     hide_index=True, width="stretch")
    else:
        st.caption("No stop-loss changes yet.")

dead = broker.trades_with_status("CANCELLED", "REJECTED")
with st.expander(f"Cancelled and rejected orders ({len(dead)})"):
    if dead:
        st.dataframe(pd.DataFrame([{"Stock": d["symbol"], "Qty": d["qty"], "Status": d["status"].title(),
                                    "Placed": fmt_ist(d["created_at"]), "Reason": d["status_note"] or ""}
                                   for d in dead]), hide_index=True, width="stretch")
    else:
        st.caption("None.")
