"""Click through every page of the app, like a person would."""
import os
from datetime import datetime, timezone

import pytest

pytest.importorskip("streamlit")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from papertrade.db import make_engine  # noqa: E402
from papertrade.prices import FakePrices  # noqa: E402
from papertrade.service import PaperBroker  # noqa: E402
from papertrade.timeutil import IST  # noqa: E402

APP = os.path.join(os.path.dirname(__file__), "..", "app.py")


def ist(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)


def seed(url):
    """Two open trades, two finished trades, one pending order."""
    p, clock = FakePrices(), {"now": ist(2026, 9, 28, 10, 0)}
    b = PaperBroker(make_engine(url, 100_000.0), p, now_fn=lambda: clock["now"])
    for sym, px in {"RELIANCE": 1400, "SBIN": 800, "INFY": 1520, "TMPV": 400}.items():
        p.set_quote(sym, px, ist(2026, 9, 28, 9, 45))
    b.place_buy("RELIANCE", 10, "MARKET", sl=1370, setup="Breakout")
    b.place_buy("SBIN", 20, "MARKET", sl=780)
    b.place_buy("INFY", 5, "MARKET", sl=1500, setup="Pullback")
    b.place_buy("TMPV", 30, "MARKET", sl=390)
    infy, tmpv = b.trades_with_status("OPEN")[2:]
    p.add_candle("INFY", ist(2026, 9, 28, 10, 5), 1510, 1511, 1495, 1500)   # SL hit
    clock["now"] = ist(2026, 9, 28, 11, 0)
    p.set_quote("TMPV", 412, ist(2026, 9, 28, 10, 45))
    b.sync()
    b.sell(tmpv["id"], 30)                                                   # profit booked
    p.set_quote("INFY", 1520, ist(2026, 9, 28, 10, 45))
    b.place_buy("INFY", 4, "LIMIT", sl=1450, limit_price=1480)             # pending
    return b


@pytest.fixture
def app(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/ui.db"
    broker = seed(url)
    monkeypatch.setenv("PAPER_DEMO", "1")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    st.cache_resource.clear()
    st.cache_data.clear()
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, at.exception
    return at, broker


def ok(at):
    assert not at.exception, at.exception
    return at


def texts(at):
    return " ".join(m.value for m in at.markdown) + " " + " ".join(c.value for c in at.caption)


def test_trade_page_shows_everything(app):
    at, _ = app
    labels = [m.label for m in at.metric]
    assert {"NIFTY 50", "NIFTY BANK", "SENSEX", "INDIA VIX", "Available cash"} <= set(labels)
    subs = [s.value for s in at.subheader]
    assert "Active trades (2)" in subs and "Pending orders (1)" in subs and "Order preview" in subs
    t = texts(at)
    assert "RELIANCE" in t and "SBIN" in t and "Limit buy 4 @ ₹1,480.00" in t


def test_place_order_after_hours_goes_pending(app):
    at, broker = app
    at.text_input(key="sym").set_value("TMPV").run()
    ok(at)
    at.number_input(key="qty").set_value(10)
    at.number_input(key="sl_TMPV").set_value(395.0).run()
    ok(at)
    assert any("If stop loss hits" in h.proto.body for h in at.get("html"))
    [b for b in at.button if b.label == "Place buy order"][0].click().run()
    ok(at)
    msgs = [s.value for s in at.success]
    assert msgs and ("Order saved" in msgs[0] or "Bought" in msgs[0])


def test_size_by_risk_sets_quantity(app):
    at, _ = app
    at.text_input(key="sym").set_value("TMPV").run()
    at.number_input(key="sl_TMPV").set_value(392.0).run()
    at.number_input(key="risk_amt").set_value(300.0).run()
    ok(at)
    use = [b for b in at.button if b.label.startswith("Use ")][0]
    n = int(use.label.split()[1].replace(",", ""))
    assert n > 0
    use.click().run()
    ok(at)
    assert at.number_input(key="qty").value == n


def test_move_sl_sell_and_cancel(app):
    at, broker = app
    rel, sbin = broker.trades_with_status("OPEN")
    at.number_input(key=f"mv_{rel['id']}").set_value(1385.0)
    at.button(key=f"mvb_{rel['id']}").click().run()
    ok(at)
    assert "stop loss moved" in at.success[0].value
    at.number_input(key=f"sq_{sbin['id']}").set_value(5)
    at.button(key=f"sb_{sbin['id']}").click().run()
    ok(at)
    assert at.success and ("next open" in at.success[0].value or "Sold 5" in at.success[0].value)
    if broker.trades_with_status("OPEN")[1]["pending_sell_qty"]:
        at.button(key=f"cq_{sbin['id']}").click().run()
        ok(at)
        assert "Cancelled the queued sell" in at.success[0].value
    pend = broker.trades_with_status("PENDING")[0]
    at.button(key=f"cx_{pend['id']}").click().run()
    ok(at)
    assert "Cancelled the INFY order" in at.success[0].value


def test_history_page(app):
    at, _ = app
    at.switch_page("views/history.py").run()
    ok(at)
    vals = {m.label: m.value for m in at.metric}
    assert vals["Closed trades"] == "2"
    assert vals["Win rate"] == "50%"
    assert vals["Stop loss hit"] == "1 of 2" and vals["You booked profit"] == "1 of 2"
    assert vals["Trailing SL, in profit"] == "0 of 2" and vals["You sold at a loss"] == "0 of 2"
    assert any("Stop loss closed 1 trade, you closed 1." == c.value for c in at.caption)
    df = at.dataframe[0].value
    assert set(df["Stock"]) == {"INFY", "TMPV"}
    assert dict(zip(df["Stock"], df["Exited by"])) == {"INFY": "Stop loss", "TMPV": "You"}
    assert any("SL hit" in x for x in df["What happened"])
    at.selectbox[1].set_value("Losses only").run()
    ok(at)
    assert list(at.dataframe[0].value["Stock"]) == ["INFY"]


def test_settings_reset(app):
    at, broker = app
    at.switch_page("views/settings.py").run()
    ok(at)
    at.text_input(key="reset_confirm").set_value("RESET").run()
    [b for b in at.button if b.label == "Reset account"][0].click().run()
    ok(at)
    assert "Account reset" in at.success[0].value
    assert broker.account()["cash"] == 100_000


def test_password_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_DEMO", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/pw.db")
    monkeypatch.setenv("APP_PASSWORD", "chai123")
    st.cache_resource.clear()
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.metric
    at.text_input[0].set_value("wrong").run()
    assert at.error[0].value == "Wrong password."
    at.text_input[0].set_value("chai123").run()
    ok(at)
    assert "Available cash" in [m.label for m in at.metric]
