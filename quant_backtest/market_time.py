"""Use the exchange timezone regardless of the cloud host's timezone."""

from datetime import datetime
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("Asia/Shanghai")


def market_now() -> datetime:
    return datetime.now(MARKET_TZ)
