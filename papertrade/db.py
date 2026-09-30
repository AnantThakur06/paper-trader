"""Database tables. Works with SQLite (local) and PostgreSQL (Neon, cloud)."""
from contextlib import contextmanager

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        MetaData, String, Table, Text, create_engine, func,
                        select, text)

from .timeutil import now_utc

metadata = MetaData()

trades = Table(
    "trades", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(40), nullable=False),
    Column("qty", Integer, nullable=False),
    Column("qty_open", Integer, nullable=False, default=0),
    Column("order_type", String(10), nullable=False),        # MARKET / LIMIT
    Column("limit_price", Float),
    Column("sl_price", Float, nullable=False),
    Column("status", String(12), nullable=False),            # PENDING/OPEN/CLOSED/CANCELLED/REJECTED
    Column("reserve_amount", Float, nullable=False, default=0.0),
    Column("setup", String(40)),
    Column("note", Text),
    Column("created_at", DateTime, nullable=False),
    Column("ref_ts", DateTime),                               # watch candles after this
    Column("last_candle_ts", DateTime),
    Column("entry_price", Float),
    Column("entry_time", DateTime),
    Column("buy_charges", Float, nullable=False, default=0.0),
    Column("pending_sell_qty", Integer, nullable=False, default=0),
    Column("pending_sell_at", DateTime),
    Column("closed_at", DateTime),
    Column("status_note", Text),
)

exits = Table(
    "exits", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trade_id", Integer, ForeignKey("trades.id"), nullable=False),
    Column("qty", Integer, nullable=False),
    Column("price", Float, nullable=False),
    Column("time", DateTime, nullable=False),
    Column("reason", String(16), nullable=False),   # MANUAL / MANUAL_OPEN / SL / GAP
    Column("sl_at_exit", Float),
    Column("gross_pnl", Float, nullable=False),
    Column("sell_charges", Float, nullable=False),
    Column("buy_charges_alloc", Float, nullable=False),
    Column("net_pnl", Float, nullable=False),
    Column("dp_charged", Boolean, nullable=False, default=False),
)

sl_changes = Table(
    "sl_changes", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trade_id", Integer, ForeignKey("trades.id"), nullable=False),
    Column("old_sl", Float, nullable=False),
    Column("new_sl", Float, nullable=False),
    Column("changed_at", DateTime, nullable=False),
)

ledger = Table(
    "ledger", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False),
    Column("kind", String(12), nullable=False),     # DEPOSIT / BUY / SELL
    Column("amount", Float, nullable=False),        # + money in, - money out
    Column("trade_id", Integer),
    Column("note", Text),
)

meta = Table(
    "meta", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text),
)

LOCK_KEY = 7_311_2026  # any fixed number; used for a Postgres advisory lock


def normalize_url(url: str) -> str:
    """Accept the connection string exactly as Neon/Supabase show it."""
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def make_engine(url: str, starting_balance: float):
    engine = create_engine(normalize_url(url), pool_pre_ping=True, pool_recycle=280)
    metadata.create_all(engine)
    with locked(engine) as conn:
        has_money = conn.execute(select(func.count()).select_from(ledger)).scalar()
        if not has_money:
            conn.execute(ledger.insert().values(
                ts=now_utc(), kind="DEPOSIT", amount=starting_balance,
                note="Starting balance"))
    return engine


@contextmanager
def locked(engine):
    """One transaction at a time touches the trades (app + watchman safe)."""
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        yield conn
