"""The watchman. GitHub Actions runs this every 5 minutes during market hours.

It checks every pending and open trade against new price candles and records
fills, stop-loss hits and gap downs in the database.
"""
import os
import sys

from papertrade import build_broker
from papertrade.timeutil import fmt_ist, now_utc


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set. Add it under GitHub > Settings > Secrets > Actions.")
        return 1

    broker = build_broker(url)
    started = now_utc()
    try:
        messages, errors = broker.sync()
    except Exception as e:  # keep the run green; the app shows the error instead
        broker.set_meta("watchman_last_run", started.isoformat())
        broker.set_meta("watchman_last_status", f"error: {e}")
        print(f"Watchman error: {e}")
        return 0

    status = "ok" if not errors else "data problem: " + "; ".join(errors)[:300]
    broker.set_meta("watchman_last_run", started.isoformat())
    broker.set_meta("watchman_last_status", status)

    print(f"Watchman ran at {fmt_ist(started)} IST — {status}")
    for m in messages:
        print(" •", m)
    if not messages:
        print(" • nothing new")
    return 0


if __name__ == "__main__":
    sys.exit(main())
