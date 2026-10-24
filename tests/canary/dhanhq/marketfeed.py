"""The canary's dhanhq.marketfeed: opening a feed is recorded and refused."""
from _canary_log import record

NSE, IDX, BSE, NSE_FNO = 1, 0, 4, 2
Ticker, Quote, Depth, Full = 15, 17, 19, 21


class MarketFeed:
    def __init__(self, *a, **k):
        record("dhanhq.marketfeed", "MarketFeed()")
        raise RuntimeError("CANARY: MarketFeed")


DhanFeed = MarketFeed
