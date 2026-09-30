"""Step 1 test: can this computer (or GitHub's server) get NSE prices from Yahoo?

Run:  python check_data.py
"""
import sys
from datetime import timedelta

from papertrade.prices import PriceError, YahooPrices
from papertrade.timeutil import fmt_ist, minutes_between, now_utc

SYMBOLS = ["RELIANCE", "SBIN", "INFY"]


def main() -> int:
    yp = YahooPrices()
    now = now_utc()
    ok = 0
    for sym in SYMBOLS:
        try:
            q = yp.quote(sym)
            candles = yp.candles(sym, since=now - timedelta(days=3), now=now)
        except PriceError as e:
            print(f"FAIL {sym}: {e}")
            continue
        ok += 1
        age = minutes_between(q.ts, now)
        print(f"OK   {sym}: last price ₹{q.price:,.2f} at {fmt_ist(q.ts)} IST "
              f"({age:.0f} min old), {len(candles)} finished 1-min candles in last 3 days")
        for c in candles[-3:]:
            print(f"       {fmt_ist(c.ts)}  O {c.open:.2f}  H {c.high:.2f}  L {c.low:.2f}  C {c.close:.2f}")
    print()
    if ok == len(SYMBOLS):
        print("RESULT: Yahoo data works here. The watchman can run on this machine.")
        return 0
    print("RESULT: Yahoo data did NOT work for every stock. See the FAIL lines above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
