"""
The factor registry and point-in-time factor calculators.

A factor is (factor_id, version) with metadata:
    name  category  description  inputs  formula  lookback  frequency  version
    normalization (default spec, see normalize.apply)  direction (+1 higher is
    better, -1 lower is better)  data_dependency  status
and a calculator fn(FactorContext) -> raw value or None. quant_factor stores the
metadata; bumping `version` is how a formula change is recorded.

FactorContext holds only what was known at the close of `as_of`:
    feat(name)     the W3 feature (strategy_engine.features) -- RSI, returns,
                   volatility ... are NOT re-implemented here
    bars           W2 point-in-time bars ending at as_of
    fund / fund_history   fundamental_data rows whose knowledge date <= as_of:
                   knowledge date = report_date + FUNDAMENTAL_LAG_DAYS (45; results
                   are published up to 45 days after quarter end), else the row's
                   created_at date
    delivery       delivery_pct of the bars (prices_daily)
    bench          NIFTY50 closes up to as_of

Cross-sectional factors (sector_mom_60) are finished by engine.py from the
whole universe on the same date.

Status: ACTIVE when its inputs exist in ATIP, DATA_PENDING when a required
input is not stored yet -- fundamental_data is empty today (value, quality,
growth factors compute None until the fundamentals job fills it), and share
counts (market cap / size / turnover / FCF yield / price-to-sales) and bid-ask
spreads are not collected at all. Nothing is estimated in their place.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from quant import volatility as V

FUNDAMENTAL_LAG_DAYS = 45


@dataclass
class FactorDef:
    factor_id: str
    name: str
    category: str
    description: str
    formula: str
    inputs: tuple
    lookback: int
    fn: object = None
    direction: int = 1
    normalization: dict = field(default_factory=lambda: {"method": "percentile", "winsorize": 1.0})
    version: str = "1"
    frequency: str = "daily"
    data_dependency: str | None = None
    cross_sectional: bool = False

    @property
    def key(self):
        return f"{self.factor_id}@{self.version}"

    def meta(self) -> dict:
        return {"factor_id": self.factor_id, "version": self.version, "name": self.name, "category": self.category,
                "description": self.description, "formula": self.formula, "inputs": list(self.inputs),
                "lookback": self.lookback, "frequency": self.frequency, "normalization": self.normalization,
                "direction": self.direction, "data_dependency": self.data_dependency,
                "cross_sectional": self.cross_sectional, "status": "DATA_PENDING" if self.data_dependency else "ACTIVE"}

    @property
    def content_hash(self):
        m = self.meta(); m.pop("status")
        return hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()


class FactorContext:
    def __init__(self, symbol, as_of, bars, feature_ctx, fund=None, fund_history=None, delivery=None, bench=None):
        self.symbol, self.as_of, self.bars = symbol, as_of, bars
        self._fc = feature_ctx
        self.fund, self.fund_history = fund or {}, fund_history or []
        self.delivery, self.bench = delivery or [], bench or {}

    def feat(self, name):
        return self._fc.get(name) if self._fc is not None else None

    def f(self, key):
        v = self.fund.get(key)
        return float(v) if v is not None else None


# -- calculators ------------------------------------------------------------------

def _ret_between(bars, back_from, back_to):
    """% return from `back_from` sessions ago to `back_to` sessions ago."""
    if len(bars) <= back_from or not bars[-1 - back_from].close:
        return None
    return (bars[-1 - back_to].close / bars[-1 - back_from].close - 1) * 100


def _mom_12_1(c):
    return _ret_between(c.bars, 252, 21)


def _risk_adj(c):
    r, v = c.feat("ret_125"), c.feat("volatility_125")
    return r / v if r is not None and v else None


def _beta(c, n=250):
    b = c.bars[-(n + 1):]
    pairs = [(b[i].close / b[i - 1].close - 1, c.bench[b[i].date] / c.bench[b[i - 1].date] - 1)
             for i in range(1, len(b)) if b[i].date in c.bench and b[i - 1].date in c.bench
             and b[i - 1].close and c.bench[b[i - 1].date]]
    if len(pairs) < n // 2:
        return None
    xs, ys = [p[1] for p in pairs], [p[0] for p in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx if vx else None


def _adv(c, n=20):
    b = c.bars[-n:]
    return sum(x.volume for x in b) / n if len(b) == n else None


def _traded_value(c, n=20):
    b = c.bars[-n:]
    return sum(x.close * x.volume for x in b) / n / 1e7 if len(b) == n else None      # Rs crore


def _amihud(c, n=20):
    b = c.bars[-(n + 1):]
    if len(b) < n + 1:
        return None
    vals = [abs(b[i].close / b[i - 1].close - 1) / (b[i].close * b[i].volume / 1e7)
            for i in range(1, len(b)) if b[i - 1].close and b[i].volume and b[i].close]
    return sum(vals) / len(vals) if len(vals) >= n // 2 else None


def _delivery(c, n=20):
    d = [x for x in c.delivery[-n:] if x is not None]
    return sum(d) / len(d) if len(d) >= n // 2 else None


def _inv(key):
    def fn(c):
        v = c.f(key)
        return 100.0 / v if v and v > 0 else None
    return fn


def _fund(key):
    return lambda c: c.f(key)


def _cash_quality(c):
    fcf, p = c.f("fcf_cr"), c.f("profit_cr")
    return fcf / p if fcf is not None and p and p > 0 else None


def _growth_consistency(c):
    g = [r.get("revenue_growth_yoy") for r in c.fund_history[-4:] if r.get("revenue_growth_yoy") is not None]
    if len(g) < 3:
        return None
    m = sum(g) / len(g)
    sd = math.sqrt(sum((x - m) ** 2 for x in g) / (len(g) - 1))
    return 1 / (1 + sd)


def _fcf_growth(c):
    h = c.fund_history
    if len(h) < 5 or h[-5].get("fcf_cr") in (None, 0) or h[-1].get("fcf_cr") is None:
        return None
    return (h[-1]["fcf_cr"] / abs(h[-5]["fcf_cr"]) - 1) * 100


def _pending(c):
    return None


FUND = "fundamental_data (empty today; filled by the weekly fundamentals job)"
SHARES = "shares outstanding / market capitalisation (not collected by ATIP)"
SPREAD = "bid-ask quotes (live_quotes has LTP only; live_ticks is empty)"

FACTORS = [
    # momentum
    FactorDef("mom_12_1", "12-1 month momentum", "momentum", "return from 252 to 21 sessions ago (skips the last month)",
              "close[-22]/close[-253]-1", ("bars",), 253, _mom_12_1),
    FactorDef("mom_6m", "6-month momentum", "momentum", "125-session return", "W3 ret_125", ("bars",), 126,
              lambda c: c.feat("ret_125")),
    FactorDef("mom_3m", "3-month momentum", "momentum", "60-session return", "W3 ret_60", ("bars",), 61,
              lambda c: c.feat("ret_60")),
    FactorDef("rel_mom_60", "relative momentum", "momentum", "60-session return minus NIFTY50's",
              "W3 rel_strength_60", ("bars", "benchmark"), 61, lambda c: c.feat("rel_strength_60")),
    FactorDef("risk_adj_mom_125", "risk-adjusted momentum", "momentum", "ret_125 / annualised volatility_125",
              "ret_125/volatility_125", ("bars",), 126, _risk_adj),
    FactorDef("sector_mom_60", "sector momentum", "momentum", "mean mom_3m of the stock's NSE industry that day",
              "mean(mom_3m) by industry", ("bars", "cross_section"), 61, _pending, cross_sectional=True),
    # volatility
    FactorDef("vol_20", "realised volatility 20", "volatility", "annualised close-to-close volatility, 20 sessions",
              "W3 volatility_20", ("bars",), 21, lambda c: c.feat("volatility_20"), direction=-1),
    FactorDef("vol_60", "historical volatility 60", "volatility", "annualised close-to-close volatility, 60 sessions",
              "W3 volatility_60", ("bars",), 61, lambda c: c.feat("volatility_60"), direction=-1),
    FactorDef("downside_vol_60", "downside volatility", "volatility", "semi-deviation of negative log returns, 60",
              "volatility.downside", ("bars",), 61, lambda c: V.downside(c.bars, 60), direction=-1),
    FactorDef("park_vol_20", "Parkinson volatility", "volatility", "high-low range estimator, 20 sessions",
              "volatility.parkinson", ("bars",), 20, lambda c: V.parkinson(c.bars, 20), direction=-1),
    FactorDef("vol_change_20_60", "volatility change", "volatility", "vol_20 / vol_60 - 1",
              "volatility.change", ("bars",), 61, lambda c: V.change(c.bars, 20, 60), direction=-1),
    FactorDef("beta_250", "beta to NIFTY50", "volatility", "OLS beta of daily returns vs NIFTY50, 250 sessions",
              "cov(r,rm)/var(rm)", ("bars", "benchmark"), 251, _beta, direction=-1),
    # liquidity
    FactorDef("adv_20", "average volume", "liquidity", "mean shares traded, 20 sessions", "mean(volume)",
              ("bars",), 20, _adv),
    FactorDef("traded_value_20", "traded value", "liquidity", "mean close x volume, Rs crore, 20 sessions",
              "mean(close*volume)/1e7", ("bars",), 20, _traded_value),
    FactorDef("amihud_20", "Amihud illiquidity", "liquidity", "mean |return| / traded value (Rs cr), 20 sessions",
              "mean(|r|/value)", ("bars",), 21, _amihud, direction=-1),
    FactorDef("delivery_pct_20", "delivery share", "liquidity", "mean delivery %, 20 sessions",
              "mean(delivery_pct)", ("bars",), 20, _delivery),
    FactorDef("turnover", "share turnover", "liquidity", "volume / shares outstanding", "volume/shares",
              ("shares",), 20, _pending, data_dependency=SHARES),
    FactorDef("bid_ask_spread", "bid-ask spread", "liquidity", "mean quoted spread", "(ask-bid)/mid",
              ("quotes",), 1, _pending, direction=-1, data_dependency=SPREAD),
    # size
    FactorDef("market_cap", "market capitalisation", "size", "price x shares outstanding", "close*shares",
              ("shares",), 1, _pending, data_dependency=SHARES),
    FactorDef("size_log", "log size", "size", "ln(market cap); rank / buckets from normalization", "ln(mcap)",
              ("shares",), 1, _pending, direction=-1, data_dependency=SHARES),
    # value
    FactorDef("earnings_yield", "earnings yield", "value", "100 / PE", "100/pe_ratio", ("fundamentals",), 1,
              _inv("pe_ratio"), data_dependency=FUND),
    FactorDef("book_to_market", "book-to-market", "value", "100 / PB", "100/pb_ratio", ("fundamentals",), 1,
              _inv("pb_ratio"), data_dependency=FUND),
    FactorDef("ebitda_ev_yield", "EBITDA/EV yield", "value", "100 / (EV/EBITDA)", "100/ev_ebitda",
              ("fundamentals",), 1, _inv("ev_ebitda"), data_dependency=FUND),
    FactorDef("dividend_yield", "dividend yield", "value", "dividend yield %", "dividend_yield", ("fundamentals",),
              1, _fund("dividend_yield"), data_dependency=FUND),
    FactorDef("rel_earnings_yield", "relative valuation", "value", "earnings yield vs the stock's industry",
              "100/pe_ratio, sector-relative z-score", ("fundamentals",), 1, _inv("pe_ratio"),
              normalization={"method": "zscore", "winsorize": 1.0, "relative_to": "sector"}, data_dependency=FUND),
    FactorDef("fcf_yield", "cash-flow yield", "value", "FCF / market cap", "fcf_cr/mcap", ("fundamentals", "shares"),
              1, _pending, data_dependency=SHARES),
    FactorDef("price_to_sales", "price-to-sales", "value", "market cap / revenue", "mcap/revenue_ttm",
              ("fundamentals", "shares"), 1, _pending, direction=-1, data_dependency=SHARES),
    # quality
    FactorDef("roe", "return on equity", "quality", "ROE %", "roe", ("fundamentals",), 1, _fund("roe"),
              data_dependency=FUND),
    FactorDef("roa", "return on assets", "quality", "ROA %", "roa", ("fundamentals",), 1, _fund("roa"),
              data_dependency=FUND),
    FactorDef("roce", "return on capital employed", "quality", "ROCE %", "roce", ("fundamentals",), 1, _fund("roce"),
              data_dependency=FUND),
    FactorDef("net_margin", "net margin", "quality", "net profit margin %", "net_margin", ("fundamentals",), 1,
              _fund("net_margin"), data_dependency=FUND),
    FactorDef("operating_margin", "operating margin", "quality", "operating margin %", "operating_margin",
              ("fundamentals",), 1, _fund("operating_margin"), data_dependency=FUND),
    FactorDef("leverage", "leverage", "quality", "debt / equity", "debt_equity", ("fundamentals",), 1,
              _fund("debt_equity"), direction=-1, data_dependency=FUND),
    FactorDef("interest_coverage", "interest coverage", "quality", "EBIT / interest", "interest_coverage",
              ("fundamentals",), 1, _fund("interest_coverage"), data_dependency=FUND),
    FactorDef("cash_quality", "cash-flow quality", "quality", "FCF / net profit (earnings backed by cash)",
              "fcf_cr/profit_cr", ("fundamentals",), 1, _cash_quality, data_dependency=FUND),
    # growth
    FactorDef("revenue_growth", "revenue growth", "growth", "revenue growth YoY %", "revenue_growth_yoy",
              ("fundamentals",), 1, _fund("revenue_growth_yoy"), data_dependency=FUND),
    FactorDef("eps_growth", "earnings growth", "growth", "EPS growth YoY %", "eps_growth_yoy", ("fundamentals",), 1,
              _fund("eps_growth_yoy"), data_dependency=FUND),
    FactorDef("profit_growth", "profit growth", "growth", "net profit growth YoY %", "profit_growth_yoy",
              ("fundamentals",), 1, _fund("profit_growth_yoy"), data_dependency=FUND),
    FactorDef("fcf_growth", "cash-flow growth", "growth", "FCF vs 4 reports earlier %", "fcf_cr[-1]/|fcf_cr[-5]|-1",
              ("fundamentals",), 5, _fcf_growth, data_dependency=FUND),
    FactorDef("growth_consistency", "growth consistency", "growth", "1 / (1 + stdev of the last 4 revenue growths)",
              "1/(1+sd(revenue_growth_yoy[-4:]))", ("fundamentals",), 4, _growth_consistency, data_dependency=FUND),
]
# W22 (AF-04): the families that lived only inside the ATIP composites (VPI's mean
# reversion, MRI's trend / volume, RRI's recovery) plus the W21 technicals, as
# standalone, individually researchable factors. All read W3 features, so they are
# point in time and need no new data.
FACTORS += [
    FactorDef("mr_rsi_14", "RSI mean reversion", "mean_reversion", "50 - RSI(14): oversold scores high",
              "50 - rsi_14", ("bars",), 15, lambda c: (50 - c.feat("rsi_14")) if c.feat("rsi_14") is not None else None),
    FactorDef("mr_zscore_20", "20-session z-score reversion", "mean_reversion",
              "minus the 20-session z-score of the close", "-zscore_20", ("bars",), 20,
              lambda c: -c.feat("zscore_20") if c.feat("zscore_20") is not None else None),
    FactorDef("trend_adx_14", "trend strength", "trend", "Wilder ADX(14)", "adx_14", ("bars",), 29,
              lambda c: c.feat("adx_14")),
    FactorDef("trend_dist_200", "distance from the 200-session average", "trend", "close / sma_200 - 1",
              "close/sma_200-1", ("bars",), 200,
              lambda c: (c.feat("close") / c.feat("sma_200") - 1) if c.feat("sma_200") else None),
    FactorDef("trend_mtf", "multi-timeframe alignment", "trend", "daily / weekly / monthly trend agreement -3..+3",
              "W21 mtf_alignment", ("bars",), 220, lambda c: c.feat("mtf_alignment")),
    FactorDef("trend_supertrend", "Supertrend direction", "trend", "Supertrend(10,3) direction +1 / -1",
              "W21 supertrend_dir", ("bars",), 30, lambda c: c.feat("supertrend_dir")),
    FactorDef("volume_ratio_20", "relative volume", "volume", "volume / mean of the previous 20 sessions",
              "vol_ratio_20", ("bars",), 21, lambda c: c.feat("vol_ratio_20")),
    FactorDef("volume_mfi_14", "money flow index", "volume", "MFI(14)", "W21 mfi_14", ("bars",), 15,
              lambda c: c.feat("mfi_14")),
    FactorDef("volume_cmf_20", "Chaikin money flow", "volume", "CMF(20): accumulation vs distribution",
              "W21 cmf_20", ("bars",), 20, lambda c: c.feat("cmf_20")),
    FactorDef("recovery_range_252", "recovery from the 52-week low", "recovery",
              "position in the 252-session high-low range (0 = at the low)", "range_pos_252", ("bars",), 252,
              lambda c: c.feat("range_pos_252")),
    FactorDef("risk_bri", "Beta Risk Index", "risk", "W21 BRI 0-100 (higher = riskier)", "W21 bri",
              ("bars", "benchmark"), 250, lambda c: c.feat("bri"), direction=-1),
]
REGISTRY = {f.factor_id: f for f in FACTORS}
CATEGORIES = sorted({f.category for f in FACTORS})
BUILTIN_SET = [f.factor_id for f in FACTORS if not f.data_dependency or f.data_dependency == FUND]


def get(factor_id: str) -> FactorDef:
    if factor_id not in REGISTRY:
        raise ValueError(f"unknown factor {factor_id!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[factor_id]


def sync(conn) -> int:
    """Store every factor's metadata (quant_factor). A changed definition under the
    same version is refused -- bump the version."""
    now = datetime.now()
    for f in FACTORS:
        ex = conn.execute("SELECT content_hash FROM quant_factor WHERE factor_id=? AND version=?",
                          (f.factor_id, f.version)).fetchone()
        if ex and ex[0] != f.content_hash:
            raise ValueError(f"factor {f.key} changed without a version bump")
        if not ex:
            m = f.meta()
            conn.execute("INSERT INTO quant_factor (factor_id,version,name,category,description,inputs_json,formula,"
                         "lookback,frequency,normalization_json,direction,data_dependency,status,content_hash,"
                         "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f.factor_id, f.version, f.name, f.category, f.description, json.dumps(m["inputs"]),
                          f.formula, f.lookback, f.frequency, json.dumps(f.normalization), f.direction,
                          f.data_dependency, m["status"], f.content_hash, now))
    conn.commit()
    return len(FACTORS)


def fundamentals_as_of(conn, symbols, as_of) -> dict:
    """{symbol: [rows known by as_of, oldest first]} -- knowledge date =
    report_date + FUNDAMENTAL_LAG_DAYS, else created_at."""
    out = {s: [] for s in symbols}
    try:
        rows = conn.execute("SELECT * FROM fundamental_data ORDER BY report_date, created_at").fetchall()
    except Exception:
        return out
    for r in rows:
        d = dict(r)
        if d["symbol"] not in out:
            continue
        known = None
        if d.get("report_date"):
            try:
                known = datetime.fromisoformat(str(d["report_date"])[:10]).date() + timedelta(days=FUNDAMENTAL_LAG_DAYS)
            except ValueError:
                known = None
        if known is None and d.get("created_at"):
            known = datetime.fromisoformat(str(d["created_at"])[:10]).date()
        if known is not None and known <= as_of:
            out[d["symbol"]].append(d)
    return out


def save_factor_set(conn, name: str, version: str, factor_ids: list, description: str = "") -> dict:
    for f in factor_ids:
        get(f)
    ids = sorted(set(factor_ids))
    h = hashlib.sha256(json.dumps({f: REGISTRY[f].key for f in ids}, sort_keys=True).encode()).hexdigest()
    ex = conn.execute("SELECT content_hash FROM quant_factor_set WHERE name=? AND version=?", (name, version)).fetchone()
    if ex and ex[0] != h:
        raise ValueError(f"factor set {name}@{version} exists with different factors; use a new version")
    if not ex:
        conn.execute("INSERT INTO quant_factor_set (name,version,factors_json,content_hash,description,created_at) "
                     "VALUES (?,?,?,?,?,?)", (name, version, json.dumps([REGISTRY[f].key for f in ids]), h,
                                              description, datetime.now()))
        conn.commit()
    return {"name": name, "version": version, "factors": [REGISTRY[f].key for f in ids], "content_hash": h}


def ensure_builtin_set(conn) -> dict:
    return save_factor_set(conn, "atip_factors", "1", BUILTIN_SET, "all factors computable from ATIP data")
