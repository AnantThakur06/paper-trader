"""Paper trading for NSE swing trades."""
import os

from . import config
from .db import make_engine
from .prices import FakePrices, YahooPrices
from .service import PaperBroker, TradeError


def build_broker(database_url: str | None = None, prices=None) -> PaperBroker:
    url = database_url or os.environ.get("DATABASE_URL") or config.LOCAL_DATABASE_URL
    engine = make_engine(url, config.STARTING_BALANCE)
    return PaperBroker(engine, prices or YahooPrices())


__all__ = ["build_broker", "PaperBroker", "TradeError", "YahooPrices", "FakePrices", "config"]
