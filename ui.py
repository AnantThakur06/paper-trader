"""Pieces shared by every page of the app."""
import hmac
import os
import time
from datetime import datetime

import streamlit as st

from papertrade import TradeError, build_broker
from papertrade.prices import INDICES, FakePrices, PriceError
from papertrade.service import rs
from papertrade.timeutil import fmt_ist, in_market_hours, ist_date, minutes_between, now_utc

PROFIT, LOSS, WARN = "#11683F", "#C23A2B", "#8A4B00"


# ------------------------------------------------------------------ setup
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
    p.indices = {"^NSEI": (25112.4, 96.35, [25010, 25040, 25030, 25080, 25070, 25112.4]),
                 "^NSEBANK": (55480.15, 180.6, [55300, 55350, 55320, 55400, 55450, 55480.15]),
                 "^BSESN": (82040.75, 301.2, [81740, 81800, 81790, 81950, 81990, 82040.75]),
                 "^INDIAVIX": (11.84, -0.28, [12.1, 12.05, 12.0, 11.95, 11.9, 11.84])}
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


# ------------------------------------------------------------------ actions
def flash(kind, msg):
    st.session_state["flash"] = (kind, msg)
    st.rerun()


def act(fn, *args):
    """Run a broker action; show success after rerun, or the error right here."""
    try:
        flash("success", fn(*args))
    except TradeError as e:
        st.error(str(e))


def show_flash():
    if "flash" in st.session_state:
        kind, msg = st.session_state.pop("flash")
        getattr(st, kind)(msg)


def run_sync(broker, force=False):
    last = st.session_state.get("last_sync", 0)
    if not force and time.time() - last < 60:
        return
    with st.spinner("Checking your trades against the market…"):
        msgs, errs = broker.sync()
    st.session_state["last_sync"] = time.time()
    st.session_state["last_sync_at"] = now_utc()
    st.session_state["sync_errors"] = errs
    for m in msgs:
        st.toast(m, duration="long")


def load_context(broker) -> dict:
    """Everything the pages need this run, fetched once."""
    open_rows = broker.trades_with_status("OPEN")
    pending = broker.trades_with_status("PENDING")
    quotes = broker.quotes_for([r["symbol"] for r in open_rows])
    return {"broker": broker, "open": open_rows, "pending": pending,
            "queued": [r for r in open_rows if r["pending_sell_qty"]],
            "quotes": quotes, "acct": broker.account(quotes), "now": now_utc()}


# ------------------------------------------------------------------ formatting
def signed(x) -> str:
    return ("+" if x > 0 else "") + rs(x)


def pnl_md(x, pct=None) -> str:
    """Markdown with green/red colour and a +/− sign (colour is never the only cue)."""
    color = "green" if x > 0 else "red" if x < 0 else "gray"
    text = signed(x) + (f" ({pct:+.2f}%)" if pct is not None else "")
    return f":{color}[**{text}**]"


# ------------------------------------------------------------------ indices
@st.cache_data(ttl=120, show_spinner=False)
def index_data(_prices, cache_key: str):
    out = []
    for name, ticker in INDICES.items():
        try:
            s = _prices.index_snapshot(name, ticker)
            out.append({"name": s.name, "value": s.value, "change": s.change,
                        "pct": s.change_pct, "series": s.series, "ts": s.ts})
        except PriceError:
            pass
    return out


def market_is_open(ctx, indices) -> bool:
    now = ctx["now"]
    if not in_market_hours(now):
        return False
    fresh = [i for i in indices if ist_date(i["ts"]) == ist_date(now)]
    return bool(fresh) if indices else True   # holiday = no fresh index data


# ------------------------------------------------------------------ layout pieces
def sidebar(ctx):
    acct = ctx["acct"]
    with st.sidebar:
        with st.container(border=True):
            st.caption("ACCOUNT")
            st.metric("Available cash", rs(acct["available"]),
                      help="Money you can use for new orders.")
            since = acct["total"] - acct["start"]
            pct = since / acct["start"] * 100 if acct["start"] else 0
            st.html(f"""
<div style="display:flex;flex-direction:column;gap:6px;font-size:14px">
  <div style="display:flex;justify-content:space-between"><span style="opacity:.75">Locked</span><span>{rs(acct['locked'])}</span></div>
  <div style="display:flex;justify-content:space-between"><span style="opacity:.75">Total value</span><span>{rs(acct['total'])}</span></div>
  <hr style="margin:4px 0;border:0;border-top:1px solid rgba(128,128,128,.35)">
  <div style="display:flex;justify-content:space-between"><span style="opacity:.75">Since start</span>
  <span style="font-weight:600;color:{'#5FD49A' if since >= 0 else '#FF8A7A'}">{signed(since)} ({pct:+.2f}%)</span></div>
</div>""")
        broker = ctx["broker"]
        last_run = broker.get_meta("watchman_last_run")
        if last_run:
            ran = datetime.fromisoformat(last_run)
            status = broker.get_meta("watchman_last_status", "")
            st.caption(f"Auto-check ran {fmt_ist(ran, with_date=False)} IST ({status})")
        else:
            st.caption("Auto-check hasn't run yet.")


def top_bar(ctx, indices):
    broker = ctx["broker"]
    left, right = st.columns([3, 1], vertical_alignment="center")
    with left:
        open_now = market_is_open(ctx, indices)
        row = st.container(horizontal=True)
        if open_now:
            row.badge("Market open, closes 15:30 IST", icon=":material/circle:", color="green")
        else:
            row.badge("Market closed, opens 9:15 IST on the next trading day",
                      icon=":material/circle:", color="gray")
        row.badge("Prices about 15 min delayed", color="gray")
        checked = st.session_state.get("last_sync_at")
        if checked:
            row.caption(f"Last checked {fmt_ist(checked, with_date=False)} IST")
    with right:
        if st.button("Check now", icon=":material/refresh:", width="stretch"):
            run_sync(broker, force=True)
            st.rerun()

    last_run = broker.get_meta("watchman_last_run")
    if last_run and in_market_hours(ctx["now"]):
        ran = datetime.fromisoformat(last_run)
        if minutes_between(ran, ctx["now"]) > 30:
            st.warning(f"Auto-check last ran at {fmt_ist(ran)} IST, over 30 minutes ago, so GitHub "
                       "may be running late. This page is still correct: the app checks your "
                       "trades whenever you open it.")
    for err in st.session_state.get("sync_errors", []):
        st.warning(f"Couldn't check some prices right now: {err}. Trying again on the next check.")


def index_strip(indices):
    if not indices:
        st.caption("Index prices are unavailable right now.")
        return
    cols = st.columns(len(indices))
    for col, i in zip(cols, indices):
        col.metric(i["name"], f"{i['value']:,.2f}", delta=f"{i['change']:+,.2f} ({i['pct']:+.2f}%)",
                   chart_data=i["series"], border=True)
