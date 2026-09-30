"""All the numbers you might want to change live in this one file."""

# Money you start with (and get back when you press Reset).
STARTING_BALANCE = 100_000.0

# Equity delivery charges, taken from zerodha.com/charges (checked Sep 2026).
# Change these if your broker is different (e.g. Groww charges delivery brokerage).
CHARGES = {
    "brokerage_pct": 0.0,       # % of trade value per order (Zerodha delivery = 0)
    "brokerage_cap": 0.0,       # max brokerage per order in rupees (0 = no cap)
    "stt_pct": 0.1,             # STT on buy AND sell, rounded to nearest rupee
    "exchange_pct": 0.00307,    # NSE transaction charge
    "sebi_per_crore": 10.0,     # SEBI turnover fee (Rs 10 per crore)
    "stamp_pct_buy": 0.015,     # stamp duty, buy side only
    "gst_pct": 18.0,            # GST on (brokerage + exchange charge + SEBI fee)
    "dp_per_scrip": 15.34,      # DP charge on sell, once per stock per day (GST included)
}

# A market order placed while the market is closed fills at the next open.
# Money is blocked with this extra buffer in case the stock opens higher.
AMO_BUFFER_PCT = 5.0

# Yahoo's NSE data is about 15 minutes delayed, so the newest candle can still
# be "growing". We only trust the newest candle once it is this many minutes old.
LAST_CANDLE_GRACE_MIN = 20

# A price is "live enough" for instant market fills if it is at most this old.
LIVE_PRICE_MAX_AGE_MIN = 45

# Local database used when no DATABASE_URL is given (handy for testing on your PC).
LOCAL_DATABASE_URL = "sqlite:///paper_trades.db"

SETUPS = ["Breakout", "Pullback", "Support bounce", "Trend follow", "Other"]
