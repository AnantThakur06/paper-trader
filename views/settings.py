"""Settings page: account reset, watchman status, charges, data source."""
from datetime import datetime

import pandas as pd
import streamlit as st

import ui
from papertrade import config
from papertrade.service import rs
from papertrade.timeutil import fmt_ist

ctx = st.session_state["ctx"]
broker, acct = ctx["broker"], ctx["acct"]

st.title("Settings")

with st.container(border=True):
    st.subheader("Account")
    st.markdown(f"Starting balance **{rs(config.STARTING_BALANCE)}**. "
                f"Total value now **{rs(acct['total'])}**.")
    with st.expander("Reset account"):
        st.write(f"Deletes every trade and the full history, and sets your balance back to "
                 f"{rs(config.STARTING_BALANCE)}. This can't be undone.")
        confirm = st.text_input("Type RESET to confirm", key="reset_confirm")
        if st.button("Reset account", type="primary", disabled=confirm != "RESET"):
            ui.act(broker.reset)

with st.container(border=True):
    st.subheader("Auto-check")
    last_run = broker.get_meta("watchman_last_run")
    if last_run:
        st.markdown(f"Last ran **{fmt_ist(datetime.fromisoformat(last_run))} IST**: "
                    f"{broker.get_meta('watchman_last_status', '')}")
    else:
        st.markdown("Hasn't run yet.")
    st.caption("Runs on GitHub every 5 minutes, 9:17 AM to 4:12 PM IST, Monday to Friday. "
               "The app also checks your trades every time you open it.")

with st.container(border=True):
    st.subheader("Charges used in P&L")
    c = config.CHARGES
    st.dataframe(pd.DataFrame([
        {"Charge": "Brokerage", "Rate": f"{c['brokerage_pct']}% per order"},
        {"Charge": "STT", "Rate": f"{c['stt_pct']}% on buy and sell"},
        {"Charge": "NSE transaction", "Rate": f"{c['exchange_pct']}%"},
        {"Charge": "SEBI fee", "Rate": f"₹{c['sebi_per_crore']:.0f} per crore"},
        {"Charge": "Stamp duty", "Rate": f"{c['stamp_pct_buy']}% on buy"},
        {"Charge": "GST", "Rate": f"{c['gst_pct']:.0f}% on brokerage + exchange + SEBI"},
        {"Charge": "DP charge", "Rate": f"₹{c['dp_per_scrip']} per stock per day, on sell"},
    ]), hide_index=True, width="stretch")
    st.caption("From zerodha.com/charges. Using another broker? Change them in papertrade/config.py.")

with st.container(border=True):
    st.subheader("Prices")
    st.write("Stock and index prices come from Yahoo Finance and are about 15 minutes behind the "
             "live market. Stop losses are checked on 1-minute candles, so a quick dip is still caught.")
