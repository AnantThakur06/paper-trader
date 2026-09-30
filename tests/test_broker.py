"""Every rule of the app, checked with numbers worked out by hand.

Run with:  python -m pytest -q
"""
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from papertrade.charges import delivery_charges
from papertrade.db import make_engine
from papertrade.prices import FakePrices, YahooPrices
from papertrade.service import PaperBroker, TradeError
from papertrade.timeutil import IST


def ist(y, m, d, h, mi):
    """IST wall-clock time -> naive UTC (how the app stores time)."""
    return datetime(y, m, d, h, mi, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)


MON, TUE = (2026, 10, 5), (2026, 10, 6)   # Monday 5 Oct, Tuesday 6 Oct 2026


@pytest.fixture
def env(tmp_path):
    prices = FakePrices()
    clock = {"now": ist(*MON, 10, 0)}
    url = os.environ.get("TEST_DATABASE_URL")       # optional: test on real Postgres
    if url:
        from sqlalchemy import create_engine
        from papertrade.db import metadata, normalize_url
        metadata.drop_all(create_engine(normalize_url(url)))
    engine = make_engine(url or f"sqlite:///{tmp_path}/test.db", 100_000.0)
    broker = PaperBroker(engine, prices, now_fn=lambda: clock["now"])
    return broker, prices, clock


def flat_candles(prices, sym, start, minutes, price):
    """Quiet candles that don't touch anything interesting."""
    for i in range(minutes):
        t = start + timedelta(minutes=i)
        prices.add_candle(sym, t, price, price + 1, price - 1, price)


def cash(b):
    return b.account()["cash"]


# ---------------------------------------------------------------- charges
def test_buy_charges_by_hand():
    ch = delivery_charges("BUY", 20, 1400)          # turnover 28,000
    assert ch["stt"] == 28                           # 0.1%
    assert ch["exchange"] == 0.86                    # 28000 * 0.00307% = 0.8596
    assert ch["sebi"] == 0.03                        # 10 per crore = 0.028
    assert ch["stamp"] == 4.20                       # 0.015%
    assert ch["gst"] == 0.16                         # 18% of (0.8596 + 0.028)
    assert ch["dp"] == 0
    assert ch["total"] == 33.25


def test_sell_charges_by_hand():
    ch = delivery_charges("SELL", 20, 1370, include_dp=True)   # turnover 27,400
    assert ch["stt"] == 27                           # 27.4 rounds to 27
    assert ch["stamp"] == 0                          # no stamp duty on sell
    assert ch["dp"] == 15.34
    assert ch["total"] == 43.37


# ------------------------------------------------------------- market buy
def test_market_buy_fills_at_live_price(env):
    b, p, clock = env
    p.set_quote("RELIANCE", 1400, ist(*MON, 9, 45))          # ~15 min delayed
    msg = b.place_buy("reliance", 20, "MARKET", sl=1370)
    assert "Bought 20 RELIANCE at ₹1,400.00" in msg
    assert cash(b) == pytest.approx(100_000 - 28_000 - 33.25)
    acct = b.account()
    assert acct["invested"] == 28_000 and acct["available"] == pytest.approx(71_966.75)


def test_sl_must_be_below_price(env):
    b, p, _ = env
    p.set_quote("RELIANCE", 1400, ist(*MON, 9, 45))
    with pytest.raises(TradeError, match="must be below"):
        b.place_buy("RELIANCE", 20, "MARKET", sl=1400)


def test_not_enough_cash(env):
    b, p, _ = env
    p.set_quote("MRF", 150_000, ist(*MON, 9, 45))
    with pytest.raises(TradeError, match="Not enough cash"):
        b.place_buy("MRF", 1, "MARKET", sl=140_000)


def test_unknown_symbol(env):
    b, _, _ = env
    with pytest.raises(TradeError, match="Couldn't get a price"):
        b.place_buy("NOTASTOCK", 1, "MARKET", sl=10)


# --------------------------------------------------------------- stop loss
def test_sl_hit_during_the_day_exits_at_sl(env):
    b, p, clock = env
    p.set_quote("RELIANCE", 1400, ist(*MON, 9, 45))
    b.place_buy("RELIANCE", 20, "MARKET", sl=1370)
    flat_candles(p, "RELIANCE", ist(*MON, 9, 46), 19, 1400)            # 9:46-10:04
    p.add_candle("RELIANCE", ist(*MON, 10, 5), 1380, 1382, 1368, 1375)  # low touches SL
    flat_candles(p, "RELIANCE", ist(*MON, 10, 6), 10, 1375)
    clock["now"] = ist(*MON, 10, 40)
    msgs, errors = b.sync()
    assert errors == []
    assert any("SL hit" in m for m in msgs)
    (e,) = b.exits_with_trades()
    assert e["price"] == 1370 and e["reason"] == "SL" and e["qty"] == 20
    assert e["gross_pnl"] == -600
    assert e["net_pnl"] == pytest.approx(-600 - 43.37 - 33.25)       # -676.62
    assert cash(b) == pytest.approx(100_000 - 676.62)
    assert b.trades_with_status("CLOSED")[0]["qty_open"] == 0


def test_gap_down_exits_at_open_not_sl(env):
    b, p, clock = env
    p.set_quote("TMPV", 680, ist(*MON, 9, 45))
    b.place_buy("TMPV", 50, "MARKET", sl=670)
    flat_candles(p, "TMPV", ist(*MON, 9, 46), 30, 680)
    p.add_candle("TMPV", ist(*TUE, 9, 15), 664, 666, 660, 662)   # opens below SL
    clock["now"] = ist(*TUE, 10, 0)
    b.sync()
    (e,) = b.exits_with_trades()
    assert e["reason"] == "GAP" and e["price"] == 664 and e["sl_at_exit"] == 670
    assert e["gross_pnl"] == (664 - 680) * 50                          # bigger loss than SL


def test_candle_before_entry_is_ignored(env):
    b, p, clock = env
    p.add_candle("INFY", ist(*MON, 9, 40), 1500, 1501, 1450, 1500)     # before the buy
    p.set_quote("INFY", 1520, ist(*MON, 9, 45))
    b.place_buy("INFY", 10, "MARKET", sl=1490)
    clock["now"] = ist(*MON, 10, 30)
    b.sync()
    assert b.trades_with_status("OPEN")                                 # still open


def test_newest_candle_waits_until_final(env):
    b, p, clock = env
    p.set_quote("INFY", 1520, ist(*MON, 9, 45))
    b.place_buy("INFY", 10, "MARKET", sl=1490)
    p.add_candle("INFY", ist(*MON, 10, 25), 1500, 1501, 1480, 1495)    # newest, touches SL
    clock["now"] = ist(*MON, 10, 30)                                    # too fresh to trust
    b.sync()
    assert b.trades_with_status("OPEN")
    clock["now"] = ist(*MON, 10, 50)                                    # now it's final
    b.sync()
    assert b.trades_with_status("CLOSED")


def test_sync_twice_does_not_double_count(env):
    b, p, clock = env
    p.set_quote("INFY", 1520, ist(*MON, 9, 45))
    b.place_buy("INFY", 10, "MARKET", sl=1490)
    p.add_candle("INFY", ist(*MON, 10, 0), 1500, 1501, 1480, 1495)
    clock["now"] = ist(*MON, 11, 0)
    b.sync()
    before = cash(b)
    b.sync()
    b.sync()
    assert len(b.exits_with_trades()) == 1 and cash(b) == before


# -------------------------------------------------------------- limit order
def test_limit_order_waits_then_fills_at_limit(env):
    b, p, clock = env
    p.set_quote("HDFCBANK", 1600, ist(*MON, 9, 45))
    msg = b.place_buy("HDFCBANK", 10, "LIMIT", sl=1560, limit_price=1590)
    assert "Limit order placed" in msg
    reserve = 10 * 1590 + delivery_charges("BUY", 10, 1590)["total"]
    assert b.account()["reserved"] == pytest.approx(reserve)
    assert b.account()["available"] == pytest.approx(100_000 - reserve)

    p.add_candle("HDFCBANK", ist(*MON, 10, 1), 1600, 1601, 1595, 1598)  # not yet
    p.add_candle("HDFCBANK", ist(*MON, 10, 2), 1596, 1597, 1588, 1592)  # touches 1590
    flat_candles(p, "HDFCBANK", ist(*MON, 10, 3), 5, 1592)
    clock["now"] = ist(*MON, 10, 40)
    msgs, _ = b.sync()
    (t,) = b.trades_with_status("OPEN")
    assert t["entry_price"] == 1590 and t["reserve_amount"] == 0
    assert b.account()["reserved"] == 0


def test_limit_fills_at_open_when_stock_opens_below(env):
    b, p, clock = env
    clock["now"] = ist(*MON, 18, 0)
    p.set_quote("HDFCBANK", 1600, ist(*MON, 15, 29))
    b.place_buy("HDFCBANK", 10, "LIMIT", sl=1540, limit_price=1590)
    p.add_candle("HDFCBANK", ist(*TUE, 9, 15), 1575, 1580, 1570, 1578)
    clock["now"] = ist(*TUE, 10, 0)
    b.sync()
    assert b.trades_with_status("OPEN")[0]["entry_price"] == 1575     # better price


def test_marketable_limit_fills_immediately_during_market(env):
    b, p, _ = env
    p.set_quote("ITC", 400, ist(*MON, 9, 45))
    msg = b.place_buy("ITC", 10, "LIMIT", sl=390, limit_price=405)
    assert "Bought 10 ITC at ₹400.00" in msg


def test_cancel_limit_order_returns_money(env):
    b, p, _ = env
    p.set_quote("HDFCBANK", 1600, ist(*MON, 9, 45))
    b.place_buy("HDFCBANK", 10, "LIMIT", sl=1560, limit_price=1590)
    (t,) = b.trades_with_status("PENDING")
    b.cancel_order(t["id"])
    assert b.account()["available"] == pytest.approx(100_000)
    assert b.trades_with_status("CANCELLED")


# ------------------------------------------------ orders when market is closed
def test_market_order_after_hours_fills_next_open(env):
    b, p, clock = env
    clock["now"] = ist(*MON, 18, 0)
    p.set_quote("RELIANCE", 1400, ist(*MON, 15, 29))
    msg = b.place_buy("RELIANCE", 20, "MARKET", sl=1370)
    assert "Market is closed" in msg
    assert b.account()["reserved"] == pytest.approx(20 * 1470 + delivery_charges("BUY", 20, 1470)["total"])
    p.add_candle("RELIANCE", ist(*TUE, 9, 15), 1410, 1415, 1405, 1412)
    clock["now"] = ist(*TUE, 10, 0)
    b.sync()
    (t,) = b.trades_with_status("OPEN")
    assert t["entry_price"] == 1410
    assert cash(b) == pytest.approx(100_000 - 20 * 1410 - delivery_charges("BUY", 20, 1410)["total"])


def test_after_hours_order_rejected_if_gap_up_too_big(env):
    b, p, clock = env
    clock["now"] = ist(*MON, 18, 0)
    p.set_quote("RELIANCE", 1400, ist(*MON, 15, 29))
    b.place_buy("RELIANCE", 67, "MARKET", sl=1370)       # fits with the 5% buffer
    p.add_candle("RELIANCE", ist(*TUE, 9, 15), 1500, 1505, 1495, 1500)   # +7% gap up
    clock["now"] = ist(*TUE, 10, 0)
    msgs, _ = b.sync()
    assert b.trades_with_status("REJECTED")
    assert cash(b) == pytest.approx(100_000)


# ------------------------------------------------------------- manual sells
def test_partial_sells_and_dp_once_per_day(env):
    b, p, clock = env
    p.set_quote("SBIN", 800, ist(*MON, 9, 45))
    b.place_buy("SBIN", 100, "MARKET", sl=780)
    (t,) = b.trades_with_status("OPEN")
    clock["now"] = ist(*MON, 11, 0)
    p.set_quote("SBIN", 820, ist(*MON, 10, 45))
    b.sell(t["id"], 50)
    b.sell(t["id"], 30)
    e1, e2 = b.exits_with_trades()
    assert e1["dp_charged"] and not e2["dp_charged"]          # DP only once that day
    assert b.trades_with_status("OPEN")[0]["qty_open"] == 20
    assert e1["buy_charges_alloc"] == pytest.approx(delivery_charges("BUY", 100, 800)["total"] * 0.5)


def test_sell_after_hours_is_queued_for_next_open(env):
    b, p, clock = env
    p.set_quote("SBIN", 800, ist(*MON, 9, 45))
    b.place_buy("SBIN", 100, "MARKET", sl=780)
    (t,) = b.trades_with_status("OPEN")
    flat_candles(p, "SBIN", ist(*MON, 9, 46), 60, 805)
    clock["now"] = ist(*MON, 19, 0)
    p.set_quote("SBIN", 805, ist(*MON, 15, 29))
    msg = b.sell(t["id"], 40)
    assert "next open" in msg
    p.add_candle("SBIN", ist(*TUE, 9, 15), 812, 815, 810, 813)
    clock["now"] = ist(*TUE, 10, 0)
    b.sync()
    (e,) = b.exits_with_trades()
    assert e["reason"] == "MANUAL_OPEN" and e["price"] == 812 and e["qty"] == 40
    assert b.trades_with_status("OPEN")[0]["qty_open"] == 60


def test_cannot_sell_after_sl_already_hit(env):
    b, p, clock = env
    p.set_quote("SBIN", 800, ist(*MON, 9, 45))
    b.place_buy("SBIN", 100, "MARKET", sl=780)
    (t,) = b.trades_with_status("OPEN")
    p.add_candle("SBIN", ist(*MON, 10, 0), 790, 791, 775, 778)
    clock["now"] = ist(*MON, 11, 0)
    p.set_quote("SBIN", 790, ist(*MON, 10, 45))
    with pytest.raises(TradeError, match="already closed"):
        b.sell(t["id"], 10)


# ------------------------------------------------------------ moving the SL
def test_moved_sl_applies_only_after_the_change(env):
    b, p, clock = env
    p.set_quote("LT", 3500, ist(*MON, 9, 45))
    b.place_buy("LT", 10, "MARKET", sl=3400)
    (t,) = b.trades_with_status("OPEN")
    flat_candles(p, "LT", ist(*MON, 9, 46), 50, 3560)                   # up to 10:35
    p.add_candle("LT", ist(*MON, 10, 40), 3560, 3562, 3515, 3550)      # dips to 3515 BEFORE change
    clock["now"] = ist(*MON, 11, 0)
    p.set_quote("LT", 3555, ist(*MON, 10, 45))
    b.move_sl(t["id"], 3520)                                          # trail SL up at 11:00
    b.sync()
    clock["now"] = ist(*MON, 11, 30)
    b.sync()
    assert b.trades_with_status("OPEN"), "old dip happened before the SL was moved"

    p.add_candle("LT", ist(*MON, 11, 5), 3540, 3541, 3518, 3525)       # after change
    clock["now"] = ist(*MON, 12, 0)
    b.sync()
    (e,) = b.exits_with_trades()
    assert e["price"] == 3520 and e["gross_pnl"] == 200                # SL hit in profit
    log = b.sl_log()
    assert log[0]["old_sl"] == 3400 and log[0]["new_sl"] == 3520


def test_sl_cannot_be_above_current_price(env):
    b, p, clock = env
    p.set_quote("LT", 3500, ist(*MON, 9, 45))
    b.place_buy("LT", 10, "MARKET", sl=3400)
    (t,) = b.trades_with_status("OPEN")
    with pytest.raises(TradeError, match="below the current price"):
        b.move_sl(t["id"], 3600)


# ----------------------------------------------------------- whole picture
def test_money_always_adds_up(env):
    """Final cash - starting cash must equal the sum of every net P&L."""
    b, p, clock = env
    p.set_quote("A", 100, ist(*MON, 9, 45))
    p.set_quote("B", 250, ist(*MON, 9, 45))
    b.place_buy("A", 300, "MARKET", sl=95)
    b.place_buy("B", 120, "MARKET", sl=240)
    ta, tb = b.trades_with_status("OPEN")
    clock["now"] = ist(*MON, 11, 0)
    p.set_quote("A", 104.35, ist(*MON, 10, 45))
    b.sell(ta["id"], 100)
    b.sell(ta["id"], 77)
    p.add_candle("A", ist(*TUE, 9, 15), 93.1, 94, 92, 93)       # gap: rest of A exits at 93.1
    p.add_candle("B", ist(*TUE, 9, 20), 245, 246, 239.5, 241)   # SL on B
    clock["now"] = ist(*TUE, 10, 30)
    b.sync()
    assert not b.trades_with_status("OPEN")
    total_net = sum(e["net_pnl"] for e in b.exits_with_trades())
    assert cash(b) - 100_000 == pytest.approx(total_net, abs=0.02)
    s = b.stats()
    assert s["closed_trades"] == 2
    assert s["net"] == pytest.approx(total_net)
    assert s["gross"] - s["charges"] == pytest.approx(s["net"], abs=0.02)


def test_reset(env):
    b, p, _ = env
    p.set_quote("A", 100, ist(*MON, 9, 45))
    b.place_buy("A", 10, "MARKET", sl=95)
    b.reset()
    assert cash(b) == 100_000 and not b.trades_with_status("OPEN")


# --------------------------------------------------- Yahoo data conversion
def _yahoo_frame(rows):
    idx = pd.DatetimeIndex([pd.Timestamp(t, tz="Asia/Kolkata") for t, *_ in rows], name="Datetime")
    return pd.DataFrame({"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
                         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
                         "Volume": [1000] * len(rows)}, index=idx)


def test_yahoo_candles_are_converted_and_filtered(monkeypatch):
    frame = _yahoo_frame([
        ("2026-10-05 09:15", 100, 101, 99, 100.5),
        ("2026-10-05 09:16", 100.5, 102, 100, 101),
        ("2026-10-05 09:17", 101, 101.5, 98, 99),   # newest: still forming at 09:30
    ])
    yp = YahooPrices()
    monkeypatch.setattr(yp, "_history", lambda *a, **k: frame)
    out = yp.candles("X", since=ist(*MON, 9, 0), now=ist(*MON, 9, 30))
    assert [c.ts for c in out] == [ist(*MON, 9, 15), ist(*MON, 9, 16)]
    assert out[0].session_open and not out[1].session_open
    later = yp.candles("X", since=ist(*MON, 9, 15), now=ist(*MON, 10, 0))
    assert [c.ts for c in later] == [ist(*MON, 9, 16), ist(*MON, 9, 17)]


def test_yahoo_quote(monkeypatch):
    frame = _yahoo_frame([("2026-10-05 10:44", 1400, 1401, 1399, 1400.456)])
    yp = YahooPrices()
    monkeypatch.setattr(yp, "_history", lambda *a, **k: frame)
    q = yp.quote("X")
    assert q.price == 1400.46 and q.ts == ist(*MON, 10, 44)
