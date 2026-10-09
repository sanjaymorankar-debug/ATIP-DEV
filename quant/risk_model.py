"""
Fundamental factor risk model (W40, gap analysis 4 item 9): a Barra-style equity risk model over
ATIP's stored universe. numpy only (pandas is not needed). Research output: nothing here creates an
intent, a risk decision or an order, and nothing in quant/ imports execution/ or orders/ (the PAPER
and LIVE books are decomposed by portfolio/risk.py factor_risk, which calls decompose_weights).

Notation: t = an exposure date (a NIFTY50 session), t+1 the next session; N stocks, K factors;
"estu" = the estimation universe of a session = stocks with a close, enough history and a
point-in-time market cap. Daily units throughout; annualised figures multiply variance by 252.

1. EXPOSURES X_t, point in time (prices <= t; fundamentals whose knowledge date <= t, the same rule
   as quant.factors.fundamentals_as_of -- quant.factors.knowledge_date). Per field (shares_out,
   book_value_ps, eps_ttm, roe) the latest-period value known by t is used; a value whose period
   ended more than max_fundamental_age_days before t is treated as missing.
     market        1 for every stock (the country factor)
     industry      1 in the stock's model industry: the NSE industry of the cached Nifty 500 list;
                   an industry with fewer than min_industry_size stocks in the model universe is
                   pooled with the other small ones of its NSE macro-economic sector (NSE_MACRO) --
                   into that macro sector when it is itself a kept industry or the pool is big
                   enough, else into "Other" (which also takes stocks with no NSE industry,
                   flagged "industry")
     style descriptors (raw, before standardisation):
       size            ln(mcap), mcap = close_t x shares_out / 1e7 (Rs crore)
       beta            EWMA-weighted OLS slope of daily returns on NIFTY50 returns over beta_window
                       sessions, weights 0.5^(age / beta_halflife); >= beta_min_obs observations
       momentum        sum of daily log returns from momentum_window to momentum_skip sessions ago
                       (12-1), >= momentum_min_coverage of them present
       resid_vol       sqrt(sum w e^2 / sum w), e = the beta regression's residuals; then
                       orthogonalised to size and beta (below)
       book_to_price   book_value_ps / close_t
       earnings_yield  eps_ttm / close_t (negative for loss makers)
       quality         roe (TTM profit / equity)
       liquidity       ln(mean(volume / shares_out)) over liquidity_window sessions; orthogonalised
                       to size
   Each style, cross-sectionally on t:
     winsorise    clip at the winsor_pct / 100 - winsor_pct percentiles of the estu values
     standardise  z = (x - sum_i c_i x_i / sum_i c_i) / std_eq(x)   over estu, c_i = mcap_i:
                  cap-weighted mean 0, equal-weighted (population) standard deviation 1
     missing      a stock without the descriptor gets the median z of its industry's estu stocks
                  that have it (0 = the cap-weighted mean when none do) and the descriptor is
                  listed in its `filled` flags -- never silently
     orthogonalise  resid_vol on (size, beta), liquidity on size: cap-weighted least squares over
                  the estu stocks whose inputs were all present, residuals kept for every stock
     re-standardise every style over estu, so the properties above hold exactly after filling
   A style present for fewer than min_style_coverage of the estu that day is switched off for the
   session (exposure 0 for everyone, flagged; its factor return is 0).

2. FACTOR RETURNS f_{t+1}: cross-sectional weighted least squares of r_{i,t+1} = close_{t+1} /
   close_t - 1 on X_t over the estu stocks with a valid return (|r| <= max_abs_return; larger is a
   data error or an unadjusted corporate action and is excluded, counted in n_excluded):
       min_f  sum_i v_i (r_i - X_i f)^2,   v_i = sqrt(mcap_i)
       s.t.   sum_j C_j f_j = 0 over the industries j, C_j = the cap weight of industry j
   The constraint identifies the market factor (industry dummies sum to the market column): with
   cap-weighted mean-0 styles, f_market = the cap-weighted universe return less its cap-weighted
   specific return. Solved with the restriction matrix R (f = R theta: the largest industry's
   return is -sum_{j != J} (C_j / C_J) f_j): theta = (R'X'VXR)^-1 R'X'Vr. Residuals
   u_i = r_i - X_i f are kept for every stock with a return (estu or not). Stored per day: f, t
   statistics with White (HC1) standard errors -- specific variances differ across stocks, which
   the sqrt(cap) weights do not undo: Cov f = n/(n-p) R A^-1 (sum_i v_i^2 u_i^2 z_i z_i') A^-1 R',
   A = R'X'VXR, z_i = (XR)_i, v scaled to mean 1 -- and the weighted
   R^2 = 1 - sum v u^2 / sum v (r - rbar_v)^2.

3. COVARIANCE as of t (the forecast for (t, t+1]), from the factor returns realised <= t (the last
   cov_window sessions, >= cov_min_obs of them):
       EWMA + Newey-West  S(h) = G_0 + sum_{l=1..L} (1 - l/(L+1)) (G_l + G_l'),
                          G_l = sum_s w_s f_s f_{s-l}' / sum_s w_s,  w_s = 0.5^(age_s / h),  L = newey_west_lags
       F = D C D,  D = diag(sqrt(diag S(vol_halflife))),  C = the correlation of S(corr_halflife)
                    (eigenvalues clipped at 1e-10, unit diagonal restored) -- volatilities react
                    faster than correlations, which need more observations (K(K-1)/2 of them)
   Factor returns are not demeaned (daily means are noise next to daily volatility).
   SPECIFIC RISK: sigma_hat_i^2 = sum_s w_s u_{i,s}^2 / sum_s w_s, w_s = 0.5^(age / specific_halflife),
   over the residuals realised <= t (>= specific_min_obs of them), then Bayesian shrinkage toward
   the stock's size bucket b (shrinkage_buckets: "amfi" = LARGE top 100 / MID 101-250 / SMALL by
   market cap, as research/tech_signals.py CAP_BUCKETS; or an integer number of quantile buckets):
       sigma_SH = v sigma_bar_b + (1 - v) sigma_hat,   v = q |sigma_hat - sigma_bar_b| / (Delta_b + q |sigma_hat - sigma_bar_b|)
       sigma_bar_b = the cap-weighted mean sigma_hat of the bucket, Delta_b = sqrt(mean_b (sigma_hat - sigma_bar_b)^2),
       q = shrinkage_q (0.1, Barra USE4). A stock with too few residuals gets sigma_bar_b
       (spec_from_bucket = 1); one without a market cap is shrunk toward all buckets together.

4. DECOMPOSITION of weights w (decompose):  x = X'w,  V = x'Fx + sum_i w_i^2 d_i  (d = specific var)
       factor contribution  c_k = x_k (F x)_k        sum_k c_k = x'Fx
       marginal contribution to risk  MCR_i = (X F x + D w)_i / sigma,   sum_i w_i MCR_i = sigma
       beta to the benchmark  b = (x'F x_b + sum_i w_i w_b,i d_i) / sigma_b^2
       active risk  (x - x_b)'F(x - x_b) + sum_i (w_i - w_b,i)^2 d_i
   Benchmark: a cap-weighted Nifty 50 PROXY -- the benchmark_names largest estu stocks by filed
   market cap (full market cap; NSE weights by free float, which ATIP does not store, and ATIP stores
   no index constituent weights). With fewer than benchmark_min_names stocks having a market cap it
   is the NIFTY50 INDEX itself, its exposures estimated returns-based (ridge regression of its daily
   returns on the factor returns) and its residual variance added as specific risk. The result says
   which ("benchmark.kind").

5. BIAS STATISTIC (bias_test): for a portfolio, z_t = R_{p,t+1} / sigma_{p,t} over the stored
   forecasts; b = std(z) (ddof 1) ~ 1 when the model is right (> 1: risk under-forecast). The band
   is the 95% interval of the sample standard deviation of T normal draws, 1 +- 1.96 / sqrt(2 (T-1))
   (Barra's rule of thumb sqrt(2/T)); fat tails widen the true band. Test portfolios: the
   cap-weighted estu, the equal-weighted estu, the Nifty 50 proxy, the AMFI size buckets, each
   industry (cap-weighted) and bias_random_portfolios equal-weighted random portfolios; a book is
   tested with today's weights held fixed over the window.

update() is the nightly job (pipeline/w39_jobs.py, after the post-market pipeline): incremental and
idempotent -- it computes exposures for sessions not stored yet, regressions whose next session
arrived, and covariance / specific risk for exposure dates that have none; a second run finds
nothing to do (NO_NEW). A change of the factor set or of a parameter (model_hash) rebuilds the model
from scratch over history_sessions; exposures and covariances older than that are purged (factor
returns and regressions are kept). A trailing session whose stock bars have not arrived yet (fewer
than half the usual count: NIFTY50 came, the Bhavcopy did not) waits for a later run instead of being
stored half-filled, and a universe that shrank by half or an empty NSE industry map (a missing Nifty
500 list) is refused rather than rebuilt into a degenerate model (rebuild=True accepts it). Tables:
quant_risk_exposure, quant_risk_factor_return, quant_risk_regression, quant_risk_covariance,
quant_risk_state.

Not modelled (limitations): the universe is today's tracked list (survivorship), NSE industries are
today's (not point in time), no volatility-regime or eigenfactor adjustment, no Newey-West on
specific returns, prices are not adjusted for corporate actions (large moves are excluded instead).

Every parameter is in DEFAULTS and can be overridden in atip_data/config.json "risk_model".
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import Counter, defaultdict
from datetime import date, datetime

import numpy as np

log = logging.getLogger("atip.quant")

MODEL_VERSION = "1"
MARKET = "market"
OTHER = "Other"
STYLES = ("size", "beta", "momentum", "resid_vol", "book_to_price", "earnings_yield", "quality", "liquidity")
STYLE_INFO = {
    "size": "ln(market cap) = ln(close x shares_out / 1e7), Rs crore",
    "beta": "EWMA-weighted OLS beta of daily returns on NIFTY50 (beta_window, beta_halflife)",
    "momentum": "12-1 momentum: sum of daily log returns from momentum_window to momentum_skip sessions ago",
    "resid_vol": "std of the beta regression's residuals, orthogonalised to size and beta",
    "book_to_price": "book value per share / close",
    "earnings_yield": "TTM EPS / close (negative for loss makers)",
    "quality": "return on equity (TTM profit / equity)",
    "liquidity": "ln(mean daily volume / shares outstanding), liquidity_window sessions, orthogonalised to size",
}
ORTHOGONALISE = {"resid_vol": ("size", "beta"), "liquidity": ("size",)}
FUND_FIELDS = ("shares_out", "book_value_ps", "eps_ttm", "roe")
ANNUAL = 252

# NSE's industry (the Nifty 500 list's "Industry" column) -> its macro-economic sector: the parent a
# small industry is pooled into.
NSE_MACRO = {
    "Chemicals": "Commodities", "Construction Materials": "Commodities", "Metals & Mining": "Commodities",
    "Forest Materials": "Commodities",
    "Automobile and Auto Components": "Consumer Discretionary", "Consumer Durables": "Consumer Discretionary",
    "Consumer Services": "Consumer Discretionary", "Media Entertainment & Publication": "Consumer Discretionary",
    "Realty": "Consumer Discretionary", "Textiles": "Consumer Discretionary",
    "Oil Gas & Consumable Fuels": "Energy", "Fast Moving Consumer Goods": "Fast Moving Consumer Goods",
    "Financial Services": "Financial Services", "Healthcare": "Healthcare",
    "Capital Goods": "Industrials", "Construction": "Industrials",
    "Information Technology": "Information Technology", "Services": "Services",
    "Telecommunication": "Telecommunication", "Power": "Utilities", "Diversified": "Diversified",
}

DEFAULTS = {
    "enabled": True,                 # the nightly job; the routes read whatever is stored either way
    "time": "21:45",                 # nightly, after post-market (16:45), its catch-up (18:30), EOD-late (19:30)
    "universe": "tracked_current",   # quant.engine universe (Nifty 500 + holdings), or a list of symbols
    "benchmark_symbol": "NIFTY50",   # prices_daily series: the model calendar, beta, the index benchmark
    "history_sessions": 500,         # sessions of exposures / forecasts built by the first run (or a
                                     # rebuild) and kept; older ones are purged (factor returns are kept)
    "min_history": 126,              # closes among the last beta_window sessions a stock needs for exposures
    "min_estimation_stocks": 30,     # fewer stocks with a market cap on a session -> no exposures that day
    "max_abs_return": 0.35,          # |daily return| above this: data error / unadjusted corporate action
    "winsor_pct": 1.0,               # descriptors clipped at the 1st / 99th cross-sectional percentile
    "min_style_coverage": 0.3,       # share of the estu a style needs, else it is off for the session
    "min_industry_size": 8,          # NSE industries with fewer model-universe stocks are pooled
    "beta_window": 252,              # sessions of the beta / residual-volatility regression
    "beta_halflife": 63,             # its EWMA half-life (sessions)
    "beta_min_obs": 126,             # observations it needs
    "momentum_window": 252,          # 12-1 momentum: from 252 ...
    "momentum_skip": 21,             # ... to 21 sessions ago
    "momentum_min_coverage": 0.8,    # share of those returns that must be present
    "liquidity_window": 63,          # sessions of the turnover average
    "max_fundamental_age_days": 550, # a fundamental whose period ended longer ago is missing
    "vol_halflife": 90,              # EWMA half-life (sessions) of factor volatilities
    "corr_halflife": 180,            # EWMA half-life (sessions) of factor correlations
    "newey_west_lags": 2,            # Bartlett-weighted autocovariance lags in the factor covariance
    "cov_window": 500,               # factor-return sessions the covariance uses
    "cov_min_obs": 63,               # sessions it needs before a forecast is made
    "specific_halflife": 90,         # EWMA half-life of squared specific returns
    "specific_window": 360,          # specific-return sessions used
    "specific_min_obs": 42,          # fewer -> the size-bucket mean (spec_from_bucket)
    "shrinkage_q": 0.1,              # Bayesian shrinkage intensity (Barra USE4)
    "shrinkage_buckets": "amfi",     # "amfi" (LARGE 100 / MID 150 / SMALL) or an integer of quantile buckets
    "benchmark_names": 50,           # the Nifty 50 proxy: this many largest estu stocks, cap-weighted
    "benchmark_min_names": 30,       # fewer stocks with a market cap -> the NIFTY50 index, returns-based
    "bias_window": 250,              # sessions of the bias test
    "bias_random_portfolios": 10,    # equal-weighted random test portfolios ...
    "bias_random_size": 20,          # ... of this many stocks
}
# parameters that do not change a stored number (everything else is in model_hash)
_NOT_HASHED = {"enabled", "time", "universe", "bias_window", "bias_random_portfolios", "bias_random_size",
               "benchmark_names", "benchmark_min_names"}
_INT_KEYS = {"history_sessions", "min_history", "min_estimation_stocks", "min_industry_size", "beta_window",
             "beta_min_obs", "momentum_window", "momentum_skip", "liquidity_window", "max_fundamental_age_days",
             "newey_west_lags", "cov_window", "cov_min_obs", "specific_window", "specific_min_obs",
             "benchmark_names", "benchmark_min_names", "bias_window", "bias_random_portfolios", "bias_random_size"}
_FLOAT_KEYS = {"max_abs_return", "winsor_pct", "min_style_coverage", "beta_halflife", "momentum_min_coverage",
               "vol_halflife", "corr_halflife", "specific_halflife", "shrinkage_q"}

DDL = (
    """CREATE TABLE IF NOT EXISTS quant_risk_exposure (
        as_of DATE NOT NULL, symbol TEXT NOT NULL, industry TEXT, mcap_cr REAL, in_estu INTEGER,
        x_size REAL, x_beta REAL, x_momentum REAL, x_resid_vol REAL, x_book_to_price REAL, x_earnings_yield REAL,
        x_quality REAL, x_liquidity REAL, filled TEXT, ret_next REAL, resid_next REAL, spec_var REAL,
        spec_var_raw REAL, spec_obs INTEGER, spec_from_bucket INTEGER, PRIMARY KEY (as_of, symbol))""",
    "CREATE INDEX IF NOT EXISTS idx_quant_risk_exposure_symbol ON quant_risk_exposure(symbol, as_of)",
    """CREATE TABLE IF NOT EXISTS quant_risk_factor_return (
        date DATE NOT NULL, factor TEXT NOT NULL, exposure_date DATE, ret REAL, t_stat REAL, model_hash TEXT,
        PRIMARY KEY (date, factor))""",
    """CREATE TABLE IF NOT EXISTS quant_risk_regression (
        date DATE PRIMARY KEY, exposure_date DATE, n_stocks INTEGER, n_excluded INTEGER, r2 REAL, note TEXT,
        model_hash TEXT, computed_at TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS quant_risk_covariance (
        as_of DATE PRIMARY KEY, factors_json TEXT, cov_json TEXT, n_obs INTEGER, model_hash TEXT,
        computed_at TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS quant_risk_state (
        name TEXT PRIMARY KEY, value_json TEXT, updated_at TIMESTAMP)""",
)
TABLES = {"quant_risk_exposure": (DDL[0], DDL[1]), "quant_risk_factor_return": (DDL[2],),
          "quant_risk_regression": (DDL[3],), "quant_risk_covariance": (DDL[4],), "quant_risk_state": (DDL[5],)}
_DATA_TABLES = ("quant_risk_exposure", "quant_risk_factor_return", "quant_risk_regression", "quant_risk_covariance")
_XCOLS = tuple(f"x_{s}" for s in STYLES)


def ensure_tables(conn):
    for ddls in TABLES.values():
        for d in ddls:
            conn.execute(d)


# ── configuration ─────────────────────────────────────────────────────────────

def settings() -> dict:
    """DEFAULTS overridden by atip_data/config.json "risk_model". An override of the wrong type or out
    of range is ignored (listed in "_warnings"), so a typo cannot take the routes down."""
    try:
        from quant.config import CONFIG_PATH
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("risk_model") or {}
    except Exception:
        raw = {}
    return validate({k: v for k, v in raw.items()} if isinstance(raw, dict) else {})


def validate(overrides: dict) -> dict:
    out, warn = dict(DEFAULTS), []
    for k, v in (overrides or {}).items():
        if k not in DEFAULTS:
            warn.append(f"unknown key {k!r}")
            continue
        try:
            if k in _INT_KEYS:
                v = int(v)
                if v < (0 if k == "newey_west_lags" else 1):
                    raise ValueError
            elif k in _FLOAT_KEYS:
                v = float(v)
                if not math.isfinite(v) or v < 0 or (k.endswith("halflife") and v <= 0) or \
                        (k in ("min_style_coverage", "momentum_min_coverage") and v > 1) or \
                        (k == "winsor_pct" and v >= 50):
                    raise ValueError
            elif k == "enabled":
                v = v is not False
            elif k == "shrinkage_buckets":
                if v != "amfi":
                    v = int(v)
                    if v < 1:
                        raise ValueError
            elif k == "universe":
                if not isinstance(v, (str, list)):
                    raise ValueError
            elif k in ("time", "benchmark_symbol"):
                v = str(v)
            out[k] = v
        except (TypeError, ValueError):
            warn.append(f"invalid {k}={v!r}; using {DEFAULTS[k]!r}")
    if out["momentum_skip"] >= out["momentum_window"]:
        warn.append("momentum_skip >= momentum_window; using the defaults")
        out["momentum_window"], out["momentum_skip"] = DEFAULTS["momentum_window"], DEFAULTS["momentum_skip"]
    for k in ("min_history", "beta_min_obs"):
        if out[k] > out["beta_window"]:
            warn.append(f"{k} > beta_window; using beta_window ({out['beta_window']})")
            out[k] = out["beta_window"]
    need = max(out["specific_window"], out["bias_window"]) + 2
    if out["history_sessions"] < need:
        warn.append(f"history_sessions {out['history_sessions']} < specific_window / bias_window + 2; using {need}")
        out["history_sessions"] = need
    if warn:
        out["_warnings"] = warn
    return out


def model_hash(cfg: dict, factors) -> str:
    p = {k: v for k, v in cfg.items() if k in DEFAULTS and k not in _NOT_HASHED}
    blob = json.dumps({"version": MODEL_VERSION, "factors": list(factors), "params": p}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ── small helpers ─────────────────────────────────────────────────────────────

def _d(x) -> date:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def _num(x, nd=6):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def _chunks(seq, n=400):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def kind_of(factor: str) -> str:
    return "market" if factor == MARKET else "style" if factor in STYLES else "industry"


# ── pure numerics ─────────────────────────────────────────────────────────────

def winsorise(x, ref, pct):
    """x clipped to the [pct, 100 - pct] percentiles of its finite values inside `ref`."""
    x = np.asarray(x, float)
    vals = x[ref & np.isfinite(x)]
    if pct <= 0 or len(vals) < 3:
        return x.copy()
    lo, hi = np.percentile(vals, [pct, 100 - pct])
    return np.where(np.isfinite(x), np.clip(x, lo, hi), np.nan)


def standardise(x, cap, ref):
    """(x - cap-weighted mean) / equal-weighted std, both over the finite values inside `ref`."""
    x = np.asarray(x, float)
    m = ref & np.isfinite(x)
    if m.sum() < 2 or cap[m].sum() <= 0:
        return np.full(x.shape, np.nan)
    mu = float((cap[m] * x[m]).sum() / cap[m].sum())
    sd = float(x[m].std())
    if not sd > 1e-15:
        return np.where(np.isfinite(x), 0.0, np.nan)
    return (x - mu) / sd


def build_exposures(raw: dict, mcap, estu, industry, winsor_pct=1.0, min_coverage=0.3, styles=STYLES):
    """Style exposures (n x S) from raw descriptors {style: array(n)} with NaN for missing.
    Returns (Z, filled (n x S bool), off [styles switched off for this cross-section])."""
    mcap = np.asarray(mcap, float)
    estu = np.asarray(estu, bool)
    industry = np.asarray(industry)
    n, S = len(mcap), len(styles)
    cap = np.where(np.isfinite(mcap) & (mcap > 0), mcap, 0.0)
    Z = np.zeros((n, S))
    present = np.zeros((n, S), bool)
    off = []
    for k, s in enumerate(styles):
        x = np.asarray(raw.get(s, np.full(n, np.nan)), float).copy()
        ok = np.isfinite(x)
        if (ok & estu).sum() < max(3, min_coverage * estu.sum()):
            off.append(s)
            continue
        z = standardise(winsorise(x, estu, winsor_pct), cap, estu)
        present[:, k] = ok & np.isfinite(z)
        Z[:, k] = np.where(present[:, k], z, np.nan)
    for k, s in enumerate(styles):                      # missing -> the industry's median (flagged)
        if s in off:
            continue
        miss = ~present[:, k]
        for g in np.unique(industry[miss]):
            src = estu & present[:, k] & (industry == g)
            Z[miss & (industry == g), k] = float(np.median(Z[src, k])) if src.any() else 0.0
    idx = {s: k for k, s in enumerate(styles)}
    for s, regs in ORTHOGONALISE.items():
        if s not in idx or s in off:
            continue
        rk = [idx[r] for r in regs if r in idx and r not in off]
        if not rk:
            continue
        clean = estu & present[:, idx[s]] & present[:, rk].all(1)
        if clean.sum() < len(rk) + 3:
            continue
        A = np.column_stack([np.ones(n), Z[:, rk]])
        sw = np.sqrt(cap[clean])
        coef = np.linalg.lstsq(A[clean] * sw[:, None], Z[clean, idx[s]] * sw, rcond=None)[0]
        Z[:, idx[s]] = Z[:, idx[s]] - A @ coef
    for k, s in enumerate(styles):
        if s in off:
            continue
        z = standardise(Z[:, k], cap, estu)
        if not np.isfinite(z).all() or not (z[estu] != 0).any():
            off.append(s)
            Z[:, k] = 0.0
        else:
            Z[:, k] = z
    filled = ~present
    for s in off:
        Z[:, idx[s]] = 0.0
        filled[:, idx[s]] = True
    return Z, filled, off


def industry_model(symbols, sectors: dict, min_size: int) -> dict:
    """{map: NSE industry -> model industry, industries: [model industries], merged: {model: [NSE]},
    counts: {model: n}} for the model universe `symbols`."""
    counts = Counter(sectors.get(s) for s in symbols if sectors.get(s))
    keep = {s for s, n in counts.items() if n >= min_size}
    mapping = {s: s for s in keep}
    pools = defaultdict(list)
    for s in counts:
        if s not in keep:
            pools[NSE_MACRO.get(s, s)].append(s)
    for parent, members in pools.items():
        n = sum(counts[m] for m in members)
        target = parent if (parent in keep or n >= min_size) else OTHER
        for m in members:
            mapping[m] = target
    model_counts = Counter()
    for s in symbols:
        model_counts[mapping.get(sectors.get(s), OTHER)] += 1
    inds = sorted(i for i in model_counts if i != OTHER) + ([OTHER] if model_counts.get(OTHER) else [])
    merged = defaultdict(list)
    for nse, m in sorted(mapping.items()):
        if nse != m:
            merged[m].append(nse)
    return {"map": mapping, "industries": inds, "merged": dict(merged), "counts": dict(model_counts)}


def factor_regression(X, r, v, ind_cols, ind_cap) -> dict:
    """Constrained WLS of r (n) on X (n x K). ind_cols: the industry columns; ind_cap: their cap
    weights (same order). Columns that are all zero (an industry absent that day, a style switched
    off) are not estimated: f = 0, t = None."""
    X, r, v = np.asarray(X, float), np.asarray(r, float), np.asarray(v, float)
    n, K = X.shape
    active = np.abs(X).sum(0) > 0
    caps = {c: float(w) for c, w in zip(ind_cols, ind_cap) if active[c] and w > 0}
    for c in ind_cols:
        if c not in caps:
            active[c] = False
    free = [c for c in range(K) if active[c]]
    anchor = max(caps, key=lambda c: caps[c]) if caps else None
    if anchor is not None:
        free.remove(anchor)
    R = np.zeros((K, len(free)))
    for j, c in enumerate(free):
        R[c, j] = 1.0
        if anchor is not None and c in caps:
            R[anchor, j] = -caps[c] / caps[anchor]
    vw = v / v.mean()
    XR = X @ R
    A = XR.T @ (XR * vw[:, None])
    Ainv = np.linalg.pinv(A)
    theta = Ainv @ (XR.T @ (vw * r))
    f = R @ theta
    u = r - X @ f
    rbar = float((vw * r).sum() / vw.sum())
    ss_tot = float((vw * (r - rbar) ** 2).sum())
    ss_res = float((vw * u ** 2).sum())
    dof = n - len(free)
    t, se = np.full(K, np.nan), np.full(K, np.nan)
    if dof > 0:                                      # White (HC1): specific variances differ across stocks
        G = XR * (vw * u)[:, None]
        cov = (n / dof) * (R @ Ainv @ (G.T @ G) @ Ainv @ R.T)
        se = np.where(active, np.sqrt(np.clip(np.diag(cov), 0, None)), np.nan)
        good = active & (se > 0)
        t[good] = f[good] / se[good]
    return {"f": f, "resid": u, "r2": 1 - ss_res / ss_tot if ss_tot > 0 else None, "t": t, "se": se,
            "active": active, "n": n, "dof": dof}


def ewma_weights(n: int, halflife: float) -> np.ndarray:
    """Weights of n observations, oldest first, the newest weighted 1: 0.5^(age / halflife)."""
    return 0.5 ** (np.arange(n)[::-1] / float(halflife))


def ewma_nw(Fr, halflife, lags) -> np.ndarray:
    """EWMA covariance (not demeaned) of the rows of Fr (T x K, oldest first) with the Newey-West
    (Bartlett) correction for `lags` lags of serial correlation."""
    Fr = np.asarray(Fr, float)
    T = len(Fr)
    w = ewma_weights(T, halflife)
    S = (Fr * w[:, None]).T @ Fr / w.sum()
    for l in range(1, min(int(lags), T - 1) + 1):
        wl = w[l:]
        G = (Fr[l:] * wl[:, None]).T @ Fr[:-l] / wl.sum()
        S = S + (1 - l / (lags + 1)) * (G + G.T)
    return S


def factor_covariance(Fr, vol_halflife, corr_halflife, lags) -> np.ndarray:
    """F = D C D: volatilities from the vol_halflife EWMA-NW covariance, correlations from the
    corr_halflife one (nearest positive semi-definite correlation by eigenvalue clipping)."""
    Sv = ewma_nw(Fr, vol_halflife, lags)
    Sc = ewma_nw(Fr, corr_halflife, lags)
    sd = np.sqrt(np.clip(np.diag(Sv), 0, None))
    dc = np.sqrt(np.clip(np.diag(Sc), 0, None))
    K = len(sd)
    C = np.eye(K)
    ok = dc > 0
    C[np.ix_(ok, ok)] = Sc[np.ix_(ok, ok)] / np.outer(dc[ok], dc[ok])
    C = (C + C.T) / 2
    vals, vecs = np.linalg.eigh(C)
    if vals.min() < 1e-10:
        C = (vecs * np.clip(vals, 1e-10, None)) @ vecs.T
        d = np.sqrt(np.diag(C))
        C = C / np.outer(d, d)
    return np.outer(sd, sd) * C


def specific_vol(U, halflife, min_obs):
    """(sigma_hat (N), n_obs (N)) from the residual panel U (T x N, oldest first, NaN = none);
    sigma_hat is NaN with fewer than min_obs residuals."""
    U = np.asarray(U, float)
    if U.size == 0:
        return np.full(U.shape[1] if U.ndim == 2 else 0, np.nan), np.zeros(U.shape[1] if U.ndim == 2 else 0, int)
    M = np.isfinite(U)
    W = ewma_weights(len(U), halflife)[:, None] * M
    sw = W.sum(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        var = (W * np.where(M, U, 0.0) ** 2).sum(0) / sw
    nobs = M.sum(0)
    return np.where((nobs >= min_obs) & (sw > 0), np.sqrt(var), np.nan), nobs


def size_buckets(mcap, rule="amfi"):
    """Bucket label per stock by market-cap rank (largest first); None without a market cap."""
    mcap = np.asarray(mcap, float)
    ok = np.isfinite(mcap) & (mcap > 0)
    out = np.array([None] * len(mcap), dtype=object)
    order = np.argsort(-np.where(ok, mcap, -np.inf))[:int(ok.sum())]
    if rule == "amfi":
        try:
            from research.tech_signals import CAP_BUCKETS
        except Exception:                               # pragma: no cover - the same rule, inline
            CAP_BUCKETS = ((100, "LARGE"), (250, "MID"))
        for rank, i in enumerate(order):
            out[i] = next((name for cut, name in CAP_BUCKETS if rank < cut), "SMALL")
    else:
        nb, cnt = int(rule), len(order)
        for rank, i in enumerate(order):
            out[i] = f"Q{min(nb - 1, rank * nb // max(cnt, 1)) + 1}"
    return out


def shrink_specific(sigma, mcap, buckets, q=0.1):
    """Bayesian shrinkage of specific volatilities toward the cap-weighted mean of their size bucket
    (Barra USE4). Returns (sigma_SH, from_bucket): from_bucket marks stocks whose sigma_hat was
    missing and got the bucket mean."""
    sigma = np.asarray(sigma, float)
    mcap = np.asarray(mcap, float)
    cap = np.where(np.isfinite(mcap) & (mcap > 0), mcap, 0.0)
    out = sigma.copy()
    from_bucket = ~np.isfinite(sigma)
    have = np.isfinite(sigma) & (cap > 0)

    def stats(m):
        if not m.any():
            return None, None
        mean = float((cap[m] * sigma[m]).sum() / cap[m].sum())
        return mean, float(np.sqrt(((sigma[m] - mean) ** 2).mean()))
    g_mean, g_disp = stats(have)
    labels = [b for b in dict.fromkeys(buckets) if b is not None]
    groups = [(np.array([b == lb for b in buckets]), lb) for lb in labels]
    groups.append((np.array([b is None for b in buckets]), None))
    for members, lb in groups:
        if not members.any():
            continue
        mean, disp = stats(members & have) if lb is not None else (g_mean, g_disp)
        if mean is None:
            mean, disp = g_mean, g_disp
        if mean is None:
            continue
        s = sigma[members]
        dev = np.abs(s - mean)
        with np.errstate(invalid="ignore", divide="ignore"):
            v = np.where(disp + q * dev > 0, q * dev / (disp + q * dev), 0.0)
        out[members] = np.where(np.isfinite(s), v * mean + (1 - v) * s, mean)
    return out, from_bucket


def bias_statistic(z) -> dict:
    """b = std(z) (ddof 1) of standardised outcomes, with the 95% band of the sample std of T normals."""
    z = np.asarray([x for x in z if x is not None and math.isfinite(x)], float)
    T = len(z)
    if T < 10:
        return {"T": T, "bias": None, "band": None, "in_band": None, "mean_z": None}
    b = float(z.std(ddof=1))
    half = 1.96 / math.sqrt(2 * (T - 1))
    return {"T": T, "bias": b, "band": [1 - half, 1 + half], "in_band": bool(1 - half <= b <= 1 + half),
            "mean_z": float(z.mean())}


def decompose(w, X, F, spec_var, wb=None, bench_x=None, bench_resid_var=0.0) -> dict:
    """Risk of weights w (n) under exposures X (n x K), factor covariance F (K x K) and specific
    variances spec_var (n), daily units. Optional benchmark: weights wb over the same n stocks, or
    exposures bench_x (K) + residual variance (an index). Pure: every array in, every number out."""
    w, X, F, d = np.asarray(w, float), np.asarray(X, float), np.asarray(F, float), np.asarray(spec_var, float)
    x = X.T @ w
    Fx = F @ x
    var_f = float(x @ Fx)
    var_s = float((w ** 2 * d).sum())
    var = var_f + var_s
    sd = math.sqrt(var) if var > 0 else 0.0
    contrib = x * Fx
    g_f, g_s = X @ Fx, d * w
    mcr = (g_f + g_s) / sd if sd > 0 else np.zeros_like(w)
    out = {"x": x, "var": var, "var_factor": var_f, "var_specific": var_s, "sd": sd, "contrib": contrib,
           "mcr": mcr, "ctr": w * mcr, "ctr_factor": w * g_f / sd if sd > 0 else np.zeros_like(w),
           "ctr_specific": w * g_s / sd if sd > 0 else np.zeros_like(w)}
    if wb is not None or bench_x is not None:
        if wb is not None:
            wb = np.asarray(wb, float)
            xb = X.T @ wb
            var_b = float(xb @ F @ xb + (wb ** 2 * d).sum())
            cov_pb = float(x @ F @ xb + (w * wb * d).sum())
            xa = x - xb
            var_a = float(xa @ F @ xa + ((w - wb) ** 2 * d).sum())
        else:
            xb = np.asarray(bench_x, float)
            var_b = float(xb @ F @ xb) + float(bench_resid_var)
            cov_pb = float(x @ F @ xb)
            xa = x - xb
            var_a = float(xa @ F @ xa) + var_s + float(bench_resid_var)
        out.update({"xb": xb, "var_bench": var_b, "beta": cov_pb / var_b if var_b > 0 else None,
                    "x_active": xa, "var_active": var_a, "var_active_factor": float(xa @ F @ xa)})
    return out


# ── point-in-time inputs ──────────────────────────────────────────────────────

def sector_map() -> dict:
    """symbol -> NSE industry (the quant engine's map: the cached Nifty 500 list, no network)."""
    try:
        from quant.engine import sectors
        return sectors()
    except Exception:
        return {}


def sessions(conn, bench: str, upto=None) -> list:
    q = "SELECT date FROM prices_daily WHERE symbol=? AND close>0"
    args = [bench]
    if upto:
        q += " AND date<=?"
        args.append(str(upto)[:10])
    return sorted({_d(r[0]) for r in conn.execute(q + " ORDER BY date", args)})


def _holdings(conn) -> set:
    out = set()
    for q in ("SELECT symbol FROM paper_position WHERE quantity>0",
              "SELECT symbol FROM portfolio_holdings WHERE qty>0 AND date=(SELECT MAX(date) FROM portfolio_holdings)"):
        try:
            out |= {str(r[0]).upper() for r in conn.execute(q)}
        except Exception:
            pass
    return out


def model_universe(conn, universe, bench="NIFTY50") -> list:
    if isinstance(universe, (list, tuple)):
        syms = {str(s).strip().upper() for s in universe if str(s).strip()}
    else:
        from quant.engine import _universe
        syms = set(_universe(conn, universe))
    syms |= _holdings(conn)
    syms -= {bench, "NIFTY50", "INDIAVIX"}
    return sorted(syms)


def _load_panel(conn, symbols, dates):
    """closes and volumes (T x N) on the model calendar (NaN where there is no bar)."""
    T, N = len(dates), len(symbols)
    P, V = np.full((T, N), np.nan), np.full((T, N), np.nan)
    pos = {d: i for i, d in enumerate(dates)}
    col = {s: j for j, s in enumerate(symbols)}
    for part in _chunks(symbols):
        for s, dt, c, v in conn.execute(
                f"SELECT symbol, date, close, volume FROM prices_daily WHERE date>=? AND date<=? AND symbol IN "
                f"({','.join('?' * len(part))})", [str(dates[0]), str(dates[-1])] + part):
            i = pos.get(_d(dt))
            if i is None or c is None or c <= 0:
                continue
            P[i, col[s]] = float(c)
            V[i, col[s]] = float(v) if v is not None else np.nan
    return P, V


def _bench_closes(conn, bench, dates):
    pos = {d: i for i, d in enumerate(dates)}
    m = np.full(len(dates), np.nan)
    for dt, c in conn.execute("SELECT date, close FROM prices_daily WHERE symbol=? AND date>=? AND date<=?",
                              (bench, str(dates[0]), str(dates[-1]))):
        i = pos.get(_d(dt))
        if i is not None and c and c > 0:
            m[i] = float(c)
    return m


def fundamentals_panel(conn, symbols, dates, max_age_days=550) -> dict:
    """{field: (T x N)} -- for each date, the latest-period value of the field among the rows known
    by that date (quant.factors.knowledge_date); NaN when none is known or the newest one known
    describes a period that ended more than max_age_days earlier."""
    from quant.factors import knowledge_date
    T, N = len(dates), len(symbols)
    out = {f: np.full((T, N), np.nan) for f in FUND_FIELDS}
    ords = np.array([d.toordinal() for d in dates])
    per = defaultdict(list)
    for part in _chunks(symbols):
        try:
            rows = conn.execute(f"SELECT * FROM fundamental_data WHERE symbol IN ({','.join('?' * len(part))})",
                                part).fetchall()
        except Exception:
            return out
        for r in rows:
            d = dict(r)
            per[str(d["symbol"]).upper()].append(d)
    for j, s in enumerate(symbols):
        rs = per.get(s)
        if not rs:
            continue
        # the order fundamentals_as_of uses: COALESCE(period_end, report_date), created_at
        rs.sort(key=lambda d: (str(d.get("period_end") or d.get("report_date") or ""), str(d.get("created_at") or "")))
        known = [knowledge_date(d) for d in rs]
        for f in FUND_FIELDS:
            items = []
            for k, d in enumerate(rs):
                v = d.get(f)
                if v is None or known[k] is None:
                    continue
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(v):
                    continue
                p = d.get("period_end") or d.get("report_date")
                try:
                    p = _d(p).toordinal() if p else None
                except ValueError:
                    p = None
                items.append((known[k].toordinal(), k, v, p))
            if not items:
                continue
            items.sort()
            kd = np.array([it[0] for it in items])
            best, vals, pers = -1, [], []
            for it in items:                            # running "latest period known so far"
                if it[1] > best:
                    best, bv, bp = it[1], it[2], it[3]
                vals.append(bv)
                pers.append(bp if bp is not None else -1)
            idx = np.searchsorted(kd, ords, side="right") - 1
            ok = idx >= 0
            v = np.where(ok, np.array(vals)[np.clip(idx, 0, None)], np.nan)
            p = np.where(ok, np.array(pers)[np.clip(idx, 0, None)], -1)
            stale = (p >= 0) & (ords - p > max_age_days)
            out[f][:, j] = np.where(stale, np.nan, v)
    return out


def _returns(P, max_abs):
    """Simple returns between consecutive model sessions (NaN unless both closes exist); |r| > max_abs
    -> NaN. Returns (R, n_extreme per row)."""
    R = np.full(P.shape, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        R[1:] = P[1:] / P[:-1] - 1
    extreme = np.isfinite(R) & (np.abs(R) > max_abs)
    R[extreme] = np.nan
    return R, extreme.sum(1)


def descriptors_at(i, P, V, R, Rm, fund, cfg) -> dict:
    """Raw descriptors of every panel column at row i (point in time: rows <= i only)."""
    close = P[i]
    W = int(cfg["beta_window"])
    lo = max(0, i - W + 1)
    nclose = np.isfinite(P[lo:i + 1]).sum(0)
    elig = np.isfinite(close) & (nclose >= int(cfg["min_history"]))
    shares = fund["shares_out"][i]
    with np.errstate(invalid="ignore", divide="ignore"):
        mcap = close * shares / 1e7
    mcap = np.where(np.isfinite(mcap) & (mcap > 0), mcap, np.nan)
    # beta and residual volatility: EWMA-weighted regression on the benchmark
    lo = max(1, i - W + 1)
    Rw, mw = R[lo:i + 1], Rm[lo:i + 1]
    M = np.isfinite(Rw) & np.isfinite(mw)[:, None]
    wts = 0.5 ** ((i - np.arange(lo, i + 1)) / float(cfg["beta_halflife"]))
    Wm = wts[:, None] * M
    x = np.where(np.isfinite(mw), mw, 0.0)[:, None]
    y = np.where(M, Rw, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        sw = Wm.sum(0)
        mx, my = (Wm * x).sum(0) / sw, (Wm * y).sum(0) / sw
        dx, dy = (x - mx) * M, (y - my) * M
        vxx = (Wm * dx * dx).sum(0)
        beta = (Wm * dx * dy).sum(0) / vxx
        e = dy - beta * dx
        rv = np.sqrt((Wm * e * e).sum(0) / sw)
    okb = (M.sum(0) >= int(cfg["beta_min_obs"])) & (vxx > 0)
    beta, rv = np.where(okb, beta, np.nan), np.where(okb, rv, np.nan)
    # 12-1 momentum
    a, b = i - int(cfg["momentum_window"]) + 1, i - int(cfg["momentum_skip"])
    mom = np.full(P.shape[1], np.nan)
    if a >= 1 and b >= a:
        seg = np.log1p(R[a:b + 1])
        cnt = np.isfinite(seg).sum(0)
        mom = np.where(cnt >= float(cfg["momentum_min_coverage"]) * (b - a + 1), np.nansum(seg, 0), np.nan)
    # liquidity: mean daily turnover
    L = int(cfg["liquidity_window"])
    seg = V[max(0, i - L + 1):i + 1]
    seg = np.where(np.isfinite(P[max(0, i - L + 1):i + 1]), seg, np.nan)
    cnt = np.isfinite(seg).sum(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mv = np.where(cnt > 0, np.nansum(seg, 0) / np.maximum(cnt, 1), np.nan)
        turn = mv / shares
        liq = np.where((cnt >= L / 2) & (turn > 0), np.log(np.where(turn > 0, turn, 1.0)), np.nan)
        raw = {"size": np.log(mcap), "beta": beta, "momentum": mom, "resid_vol": rv,
               "book_to_price": fund["book_value_ps"][i] / close, "earnings_yield": fund["eps_ttm"][i] / close,
               "quality": fund["roe"][i], "liquidity": liq}
    return {"raw": {k: np.where(np.isfinite(v), v, np.nan) for k, v in raw.items()}, "elig": elig, "mcap": mcap}


# ── state ─────────────────────────────────────────────────────────────────────

def get_state(conn, name):
    try:
        r = conn.execute("SELECT value_json FROM quant_risk_state WHERE name=?", (name,)).fetchone()
    except Exception:
        return None
    return json.loads(r[0]) if r and r[0] else None


def _put_state(conn, name, value):
    conn.execute("INSERT OR REPLACE INTO quant_risk_state (name, value_json, updated_at) VALUES (?,?,?)",
                 (name, json.dumps(value, default=str), datetime.now()))


def _dates(conn, q, args=()):
    return sorted({_d(r[0]) for r in conn.execute(q, args) if r[0] is not None})


# ── the job ───────────────────────────────────────────────────────────────────

def update(conn, as_of=None, universe=None, sectors=None, cfg=None, rebuild=False) -> dict:
    """Bring the stored model up to `as_of` (default: the latest benchmark session). Incremental and
    idempotent; see the module docstring."""
    cfg = cfg if cfg is not None else settings()
    ensure_tables(conn)
    bench = cfg["benchmark_symbol"]
    cal = sessions(conn, bench, as_of)
    need = max(int(cfg["min_history"]), 2) + 1
    if len(cal) < need:
        return {"status": "SKIPPED", "rows": 0, "reason": f"{len(cal)} {bench} sessions stored; need >= {need}"}
    symbols = model_universe(conn, universe if universe is not None else cfg["universe"], bench)
    if not symbols:
        return {"status": "SKIPPED", "rows": 0, "reason": "empty universe"}
    cal, incomplete = _complete_sessions(conn, cal, symbols)
    sectors = sectors if sectors is not None else sector_map()
    im = industry_model(symbols, sectors, int(cfg["min_industry_size"]))
    factors = [MARKET] + im["industries"] + list(STYLES)
    mhash = model_hash(cfg, factors)
    prev = get_state(conn, "model")
    if prev and prev.get("model_hash") != mhash and not rebuild:
        # a missing Nifty 500 list / industry map would otherwise rebuild a degenerate model tonight
        if len(symbols) < 0.5 * (prev.get("universe_size") or 0):
            return {"status": "SKIPPED", "rows": 0, "reason": f"the universe shrank from {prev['universe_size']} to "
                    f"{len(symbols)} stocks (Nifty 500 list unavailable?); not rebuilding -- POST "
                    f"/api/quant/risk-model/run with rebuild=true to accept it"}
        if len(im["industries"]) <= 1 < len(prev.get("industries") or []):
            return {"status": "SKIPPED", "rows": 0, "reason": "no NSE industry map (the cached Nifty 500 list); not "
                    "rebuilding the model without industries"}
    rebuilt = bool(rebuild or (prev and prev.get("model_hash") != mhash))
    if rebuilt:
        for t in _DATA_TABLES:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM quant_risk_state WHERE name='bias'")
    state = {"model_hash": mhash, "version": MODEL_VERSION, "factors": factors, "industries": im["industries"],
             "merged": im["merged"], "industry_counts": im["counts"], "styles": list(STYLES),
             "params": {k: v for k, v in cfg.items() if k in DEFAULTS}, "universe_size": len(symbols),
             "built_at": prev.get("built_at") if prev and not rebuilt else datetime.now().isoformat(timespec="seconds")}
    state = json.loads(json.dumps(state, default=str))
    if state != prev:                                   # a run that changes nothing writes nothing
        _put_state(conn, "model", state)
    conn.commit()
    ind_of = {s: im["map"].get(sectors.get(s), OTHER) for s in symbols}
    unclassified = {s for s in symbols if not sectors.get(s)}
    out = {"status": "SUCCESS", "model_hash": mhash, "rebuilt": rebuilt, "as_of": str(cal[-1])}
    if incomplete:
        out["incomplete_sessions"] = incomplete
    out["exposures"] = _exposure_pass(conn, cal, symbols, ind_of, unclassified, cfg, mhash)
    out["regressions"] = _regression_pass(conn, cal, factors, cfg, mhash)
    out["forecasts"] = _risk_pass(conn, factors, cfg, mhash)
    out["purged_before"] = _purge(conn, int(cfg["history_sessions"]))
    rows = out["exposures"]["rows"] + out["regressions"]["dates"] + out["forecasts"]["dates"]
    if rows or get_state(conn, "bias") is None:
        try:
            b = bias_test(conn, cfg)
            if b.get("portfolios"):
                _put_state(conn, "bias", b)
                conn.commit()
            out["bias"] = b.get("summary")
        except Exception as e:                          # the model stands without its report card
            log.warning(f"  risk model bias test: {e}")
            out["bias"] = {"error": str(e)}
    out["rows"] = rows
    skipped = out["exposures"]["skipped"]
    if not rows and skipped:
        last = max(skipped)
        out["status"] = "SKIPPED"
        out["reason"] = f"{len(skipped)} session(s) skipped, the latest {last}: {skipped[last]}"
    elif not rows:
        out["status"] = "NO_NEW"
        out["reason"] = f"up to date ({cal[-1]})" + (f"; waiting for the stock bars of {', '.join(incomplete)}"
                                                    if incomplete else "")
    return out


def _complete_sessions(conn, cal, symbols, tail=5, ref=20, min_share=0.5):
    """The calendar without its trailing sessions whose stock bars have not (all) arrived yet: fewer
    universe stocks with a close than min_share x the median of the `ref` sessions before. Such a
    session is processed on a later run, once complete, instead of being stored half-filled."""
    if len(cal) < tail + ref:
        return cal, []
    window = cal[-(tail + ref):]
    counts = Counter()
    for part in _chunks(symbols):
        for dt, n in conn.execute(f"SELECT date, COUNT(*) FROM prices_daily WHERE date>=? AND date<=? AND close>0 AND "
                                  f"symbol IN ({','.join('?' * len(part))}) GROUP BY date",
                                  [str(window[0]), str(window[-1])] + part):
            counts[_d(dt)] += int(n)
    base = float(np.median([counts.get(d, 0) for d in window[:ref]]))
    cut = len(cal)
    for k in range(len(cal) - 1, len(cal) - 1 - tail, -1):
        if counts.get(cal[k], 0) < min_share * base:
            cut = k
        else:
            break
    return cal[:cut], [str(d) for d in cal[cut:]]


def _purge(conn, keep: int):
    """Exposure and covariance rows older than the newest `keep` sessions go (the factor-return and
    regression history is small and stays: it is the research record). Returns the cut date."""
    xd = _dates(conn, "SELECT DISTINCT as_of FROM quant_risk_exposure")
    if len(xd) <= keep:
        return None
    cut = str(xd[-keep])
    conn.execute("DELETE FROM quant_risk_exposure WHERE as_of<?", (cut,))
    conn.execute("DELETE FROM quant_risk_covariance WHERE as_of<?", (cut,))
    conn.commit()
    return cut


def _exposure_pass(conn, cal, symbols, ind_of, unclassified, cfg, mhash) -> dict:
    last = conn.execute("SELECT MAX(as_of) FROM quant_risk_exposure").fetchone()[0]
    first_ok = max(len(cal) - int(cfg["history_sessions"]), int(cfg["min_history"]) - 1, 0)
    todo = [d for d in cal[first_ok:] if last is None or d > _d(last)]
    res = {"dates": 0, "rows": 0, "skipped": {}}
    if not todo:
        return res
    lookback = max(int(cfg["beta_window"]), int(cfg["momentum_window"]), int(cfg["liquidity_window"])) + 1
    i0 = cal.index(todo[0])
    dates = cal[max(0, i0 - lookback): cal.index(todo[-1]) + 1]
    pos = {d: i for i, d in enumerate(dates)}
    P, V = _load_panel(conn, symbols, dates)
    m = _bench_closes(conn, cfg["benchmark_symbol"], dates)
    Rm = np.full(len(m), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        Rm[1:] = m[1:] / m[:-1] - 1
    R, _ = _returns(P, float(cfg["max_abs_return"]))
    fund = fundamentals_panel(conn, symbols, dates, int(cfg["max_fundamental_age_days"]))
    inds = sorted(set(ind_of.values()))
    ind_idx = np.array([inds.index(ind_of[s]) for s in symbols])
    sym_arr = np.array(symbols, dtype=object)
    batch = []
    for d in todo:
        i = pos[d]
        dsc = descriptors_at(i, P, V, R, Rm, fund, cfg)
        elig = dsc["elig"]
        if not elig.any():
            res["skipped"][str(d)] = "no stock with enough history"
            continue
        mcap = dsc["mcap"][elig]
        estu = np.isfinite(mcap)
        if estu.sum() < int(cfg["min_estimation_stocks"]):
            res["skipped"][str(d)] = f"{int(estu.sum())} stocks with a market cap (need {cfg['min_estimation_stocks']})"
            continue
        raw = {s: v[elig] for s, v in dsc["raw"].items()}
        Z, filled, _off = build_exposures(raw, mcap, estu, ind_idx[elig], float(cfg["winsor_pct"]),
                                          float(cfg["min_style_coverage"]))
        syms = sym_arr[elig]
        for r_, s in enumerate(syms):
            flags = [STYLES[k] for k in range(len(STYLES)) if filled[r_, k]]
            if s in unclassified:
                flags.append("industry")
            batch.append((str(d), s, ind_of[s], _num(mcap[r_], 4), int(estu[r_]),
                          *[float(Z[r_, k]) for k in range(len(STYLES))], ",".join(flags)))
        res["dates"] += 1
        if len(batch) >= 20000:
            res["rows"] += _insert_exposures(conn, batch)
            batch = []
    res["rows"] += _insert_exposures(conn, batch)
    return res


def _insert_exposures(conn, batch) -> int:
    if not batch:
        return 0
    conn.executemany(f"INSERT OR REPLACE INTO quant_risk_exposure (as_of, symbol, industry, mcap_cr, in_estu, "
                     f"{', '.join(_XCOLS)}, filled) VALUES ({','.join('?' * (6 + len(_XCOLS)))})", batch)
    conn.commit()
    return len(batch)


def _design(industries, xs, factors):
    """X (n x K) from per-row industry names and style values (n x S)."""
    fidx = {f: k for k, f in enumerate(factors)}
    n = len(industries)
    X = np.zeros((n, len(factors)))
    X[:, 0] = 1.0
    for i, g in enumerate(industries):
        k = fidx.get(g, fidx.get(OTHER))
        if k is not None and kind_of(factors[k]) == "industry":
            X[i, k] = 1.0
    for j, s in enumerate(STYLES):
        if s in fidx:
            X[:, fidx[s]] = np.nan_to_num(np.asarray(xs[:, j], float))
    return X


def _rows_on(conn, as_of, cols="*", where="", args=()):
    return [dict(r) for r in conn.execute(f"SELECT {cols} FROM quant_risk_exposure WHERE as_of=?{where} "
                                          f"ORDER BY symbol", (str(as_of), *args))]


def _regression_pass(conn, cal, factors, cfg, mhash) -> dict:
    nxt = {cal[k]: cal[k + 1] for k in range(len(cal) - 1)}
    have = set(_dates(conn, "SELECT date FROM quant_risk_regression"))
    todo = [t for t in _dates(conn, "SELECT DISTINCT as_of FROM quant_risk_exposure") if t in nxt and nxt[t] not in have]
    res = {"dates": 0, "skipped": {}}
    if not todo:
        return res
    ind_cols = [k for k, f in enumerate(factors) if kind_of(f) == "industry"]
    max_abs = float(cfg["max_abs_return"])
    now = datetime.now()
    for t in todo:
        t1 = nxt[t]
        rows = _rows_on(conn, t, "symbol, industry, mcap_cr, in_estu, " + ", ".join(_XCOLS))
        syms = [r["symbol"] for r in rows]
        px = {}
        for part in _chunks(syms):
            for s, dt, c in conn.execute(f"SELECT symbol, date, close FROM prices_daily WHERE date IN (?,?) AND symbol IN "
                                         f"({','.join('?' * len(part))})", [str(t), str(t1)] + part):
                if c and c > 0:
                    px[(s, _d(dt))] = float(c)
        r = np.array([px[(s, t1)] / px[(s, t)] - 1 if (s, t) in px and (s, t1) in px else np.nan for s in syms])
        r = np.where(np.abs(r) <= max_abs, r, np.nan)
        X = _design([x["industry"] for x in rows], np.array([[x[c] for c in _XCOLS] for x in rows], float), factors)
        mcap = np.array([x["mcap_cr"] if x["mcap_cr"] is not None else np.nan for x in rows], float)
        estu = np.array([bool(x["in_estu"]) for x in rows]) & np.isfinite(mcap)
        reg = estu & np.isfinite(r)
        n_excl = int((estu & ~np.isfinite(r)).sum())
        if reg.sum() < int(cfg["min_estimation_stocks"]):
            note = f"{int(reg.sum())} estimation stocks with a return (need {cfg['min_estimation_stocks']})"
            conn.execute("INSERT OR REPLACE INTO quant_risk_regression (date, exposure_date, n_stocks, n_excluded, r2, "
                         "note, model_hash, computed_at) VALUES (?,?,?,?,?,?,?,?)",
                         (str(t1), str(t), int(reg.sum()), n_excl, None, note, mhash, now))
            res["skipped"][str(t1)] = note
            conn.commit()
            continue
        Xr = X[reg]
        icap = [float(mcap[reg][Xr[:, c] > 0].sum()) for c in ind_cols]
        out = factor_regression(Xr, r[reg], np.sqrt(mcap[reg]), ind_cols, icap)
        f = out["f"]
        u = np.where(np.isfinite(r), r - X @ f, np.nan)
        conn.executemany("INSERT OR REPLACE INTO quant_risk_factor_return (date, factor, exposure_date, ret, t_stat, "
                         "model_hash) VALUES (?,?,?,?,?,?)",
                         [(str(t1), fac, str(t), float(f[k]), _num(out["t"][k], 4), mhash) for k, fac in enumerate(factors)])
        conn.execute("INSERT OR REPLACE INTO quant_risk_regression (date, exposure_date, n_stocks, n_excluded, r2, note, "
                     "model_hash, computed_at) VALUES (?,?,?,?,?,?,?,?)",
                     (str(t1), str(t), int(reg.sum()), n_excl, _num(out["r2"], 6), None, mhash, now))
        conn.executemany("UPDATE quant_risk_exposure SET ret_next=?, resid_next=? WHERE as_of=? AND symbol=?",
                         [(_num(r[k], 10), _num(u[k], 10), str(t), s) for k, s in enumerate(syms)])
        conn.commit()
        res["dates"] += 1
    return res


def _factor_return_matrix(conn, factors, upto=None):
    q, a = "SELECT date, factor, ret FROM quant_risk_factor_return", []
    if upto:
        q += " WHERE date<=?"
        a.append(str(upto))
    fidx = {f: k for k, f in enumerate(factors)}
    by = defaultdict(lambda: np.zeros(len(factors)))
    for dt, fac, ret in conn.execute(q, a):
        if fac in fidx and ret is not None:
            by[_d(dt)][fidx[fac]] = float(ret)
    ds = sorted(by)
    return ds, (np.array([by[d] for d in ds]) if ds else np.zeros((0, len(factors))))


def _risk_pass(conn, factors, cfg, mhash) -> dict:
    """Covariance (as of each exposure date) and specific risk for exposure dates without one."""
    res = {"dates": 0}
    x_dates = _dates(conn, "SELECT DISTINCT as_of FROM quant_risk_exposure")
    done = set(_dates(conn, "SELECT as_of FROM quant_risk_covariance"))
    f_dates, Fr = _factor_return_matrix(conn, factors)
    if not x_dates or not f_dates:
        return res
    fpos = np.array([d.toordinal() for d in f_dates])
    todo = [d for d in x_dates if d not in done
            and int(np.searchsorted(fpos, d.toordinal(), side="right")) >= int(cfg["cov_min_obs"])]
    if not todo:
        return res
    # residual panel: rows as_of in [the specific window before todo[0], todo[-1]]
    xi = x_dates.index(todo[0])
    start = x_dates[max(0, xi - int(cfg["specific_window"]))]
    p_dates = [d for d in x_dates if start <= d <= todo[-1]]
    prow = {d: i for i, d in enumerate(p_dates)}
    pcol, cells = {}, []
    for a, s, u in conn.execute("SELECT as_of, symbol, resid_next FROM quant_risk_exposure WHERE as_of>=? AND as_of<=? "
                                "AND resid_next IS NOT NULL", (str(start), str(todo[-1]))):
        cells.append((prow[_d(a)], pcol.setdefault(s, len(pcol)), float(u)))
    panel = np.full((len(p_dates), len(pcol) + 1), np.nan)        # the extra column: never filled (no history)
    for i, j, u in cells:
        panel[i, j] = u
    now = datetime.now()
    for d in todo:
        k = int(np.searchsorted(fpos, d.toordinal(), side="right"))
        F = factor_covariance(Fr[max(0, k - int(cfg["cov_window"])):k], float(cfg["vol_halflife"]),
                              float(cfg["corr_halflife"]), int(cfg["newey_west_lags"]))
        rows = _rows_on(conn, d, "symbol, mcap_cr")
        syms = [r["symbol"] for r in rows]
        row = prow[d]                                   # residuals realised <= d: rows as_of < d
        U = panel[max(0, row - int(cfg["specific_window"])):row][:, [pcol.get(s, len(pcol)) for s in syms]]
        sig, nobs = specific_vol(U, float(cfg["specific_halflife"]), int(cfg["specific_min_obs"]))
        mcap = np.array([r["mcap_cr"] if r["mcap_cr"] is not None else np.nan for r in rows], float)
        sh, from_bucket = shrink_specific(sig, mcap, size_buckets(mcap, cfg["shrinkage_buckets"]), float(cfg["shrinkage_q"]))
        conn.execute("INSERT OR REPLACE INTO quant_risk_covariance (as_of, factors_json, cov_json, n_obs, model_hash, "
                     "computed_at) VALUES (?,?,?,?,?,?)",
                     (str(d), json.dumps(factors), json.dumps([[float(f"{v:.8g}") for v in row] for row in F]),
                      min(k, int(cfg["cov_window"])), mhash, now))
        conn.executemany("UPDATE quant_risk_exposure SET spec_var=?, spec_var_raw=?, spec_obs=?, spec_from_bucket=? "
                         "WHERE as_of=? AND symbol=?",
                         [(_num(sh[j] ** 2, 12), _num(sig[j] ** 2, 12), int(nobs[j]), int(from_bucket[j]), str(d), s)
                          for j, s in enumerate(syms)])
        conn.commit()
        res["dates"] += 1
    return res


# ── reading the model ─────────────────────────────────────────────────────────

def _cov_on(conn, as_of=None):
    q = "SELECT as_of, factors_json, cov_json, n_obs FROM quant_risk_covariance"
    a = []
    if as_of:
        q += " WHERE as_of<=?"
        a.append(str(as_of)[:10])
    r = conn.execute(q + " ORDER BY as_of DESC LIMIT 1", a).fetchone()
    if not r:
        return None
    return {"as_of": _d(r[0]), "factors": json.loads(r[1]), "F": np.array(json.loads(r[2]), float), "n_obs": r[3]}


def load_model(conn, as_of=None) -> dict | None:
    """The forecast as of the latest session with a covariance (<= as_of): factors, F, and per stock
    X, specific variance, market cap, estu flag, industry and filled flags."""
    try:
        cv = _cov_on(conn, as_of)
    except Exception:
        return None
    if not cv:
        return None
    rows = _rows_on(conn, cv["as_of"], "*", " AND spec_var IS NOT NULL")
    if not rows:
        return None
    xs = np.array([[r[c] for c in _XCOLS] for r in rows], float)
    return {"as_of": cv["as_of"], "factors": cv["factors"], "F": cv["F"], "n_obs": cv["n_obs"],
            "symbols": [r["symbol"] for r in rows], "X": _design([r["industry"] for r in rows], xs, cv["factors"]),
            "spec_var": np.array([r["spec_var"] for r in rows], float),
            "mcap": np.array([r["mcap_cr"] if r["mcap_cr"] is not None else np.nan for r in rows], float),
            "estu": np.array([bool(r["in_estu"]) for r in rows]), "industry": [r["industry"] for r in rows],
            "filled": [[f for f in (r["filled"] or "").split(",") if f] for r in rows],
            "spec_from_bucket": [bool(r["spec_from_bucket"]) for r in rows]}


def proxy_weights(mcap, estu, n_names):
    """Cap weights of the n_names largest estu stocks (the Nifty 50 proxy)."""
    mcap = np.asarray(mcap, float)
    ok = np.asarray(estu, bool) & np.isfinite(mcap) & (mcap > 0)
    order = [i for i in np.argsort(-np.where(ok, mcap, -np.inf)) if ok[i]][:int(n_names)]
    w = np.zeros(len(mcap))
    if order:
        w[order] = mcap[order] / mcap[order].sum()
    return w


def benchmark(conn, model, cfg) -> dict:
    """The active-risk benchmark: the Nifty 50 proxy, or the NIFTY50 index returns-based."""
    n_cap = int((model["estu"] & np.isfinite(model["mcap"])).sum())
    if n_cap >= int(cfg["benchmark_min_names"]):
        wb = proxy_weights(model["mcap"], model["estu"], cfg["benchmark_names"])
        return {"kind": "NIFTY50_PROXY", "weights": wb, "names": int((wb > 0).sum()),
                "description": f"cap-weighted proxy: the {int((wb > 0).sum())} largest stocks of the estimation "
                               f"universe by filed market cap (full market cap; NSE's free-float weights and index "
                               f"constituent weights are not stored in ATIP)"}
    bench = cfg["benchmark_symbol"]
    f_dates, Fr = _factor_return_matrix(conn, model["factors"], model["as_of"])
    f_dates, Fr = f_dates[-int(cfg["cov_window"]):], Fr[-int(cfg["cov_window"]):]
    closes = {}
    if f_dates:
        cal = sessions(conn, bench, f_dates[-1])
        prev = {cal[k + 1]: cal[k] for k in range(len(cal) - 1)}
        want = set(f_dates) | {prev[d] for d in f_dates if d in prev}
        for dt, c in conn.execute("SELECT date, close FROM prices_daily WHERE symbol=? AND date>=? AND date<=?",
                                  (bench, str(min(want)), str(max(want)))):
            closes[_d(dt)] = float(c) if c else None
        y = np.array([closes[d] / closes[prev[d]] - 1 if d in prev and closes.get(d) and closes.get(prev[d]) else np.nan
                      for d in f_dates])
    else:
        y = np.zeros(0)
    ok = np.isfinite(y)
    K = len(model["factors"])
    if ok.sum() < K + 20:
        return {"kind": "NONE", "description": f"fewer than {cfg['benchmark_min_names']} stocks with a market cap and "
                                               f"only {int(ok.sum())} sessions of factor returns for a returns-based "
                                               f"{bench} estimate: no benchmark"}
    A, yy = Fr[ok], y[ok]
    lam = 1e-4 * float(np.trace(A.T @ A)) / K
    xb = np.linalg.solve(A.T @ A + lam * np.eye(K), A.T @ yy)
    e = yy - A @ xb
    r2 = 1 - float(e.var()) / float(yy.var()) if yy.var() > 0 else None
    return {"kind": "NIFTY50_INDEX", "x": xb, "resid_var": float(e.var()), "sessions": int(ok.sum()), "r2": r2,
            "description": f"the {bench} index: exposures estimated returns-based (ridge regression of its daily "
                           f"returns on the factor returns, {int(ok.sum())} sessions, R2 "
                           f"{(r2 if r2 is not None else float('nan')):.2f}); fewer than {cfg['benchmark_min_names']} "
                           f"stocks have a filed market cap for a constituent proxy"}


def decompose_weights(conn, weights: dict, as_of=None, cfg=None, label="WHAT_IF") -> dict:
    """The factor-model decomposition of {symbol: weight} on the latest stored forecast. Weights of
    symbols the model does not cover are reported in coverage and the rest renormalised (said, not
    silent)."""
    cfg = cfg if cfg is not None else settings()
    m = load_model(conn, as_of)
    if m is None:
        return {"status": "NOT_BUILT", "portfolio": label,
                "note": "no risk-model forecast stored yet (the nightly risk_model job, or POST /api/quant/risk-model/run)"}
    wts = {str(k).upper(): float(v) for k, v in (weights or {}).items() if v is not None and float(v) != 0}
    gross = sum(abs(v) for v in wts.values())
    if not gross:
        return {"status": "EMPTY", "portfolio": label, "as_of": str(m["as_of"]), "note": "no weights"}
    pos = {s: i for i, s in enumerate(m["symbols"])}
    covered = [s for s in wts if s in pos]
    uncovered = [{"symbol": s, "weight": _num(wts[s] / gross),
                  "reason": f"no exposures / specific risk on {m['as_of']} (outside the model universe, under "
                            f"{cfg['min_history']} sessions of history, or no close that day)"}
                 for s in wts if s not in pos]
    if not covered:
        return {"status": "UNCOVERED", "portfolio": label, "as_of": str(m["as_of"]),
                "coverage": {"covered": 0, "of": len(wts), "value_share": 0.0, "uncovered": uncovered}}
    share = sum(abs(wts[s]) for s in covered) / gross
    tot = sum(wts[s] for s in covered)
    if abs(tot) < 1e-12:                                # long / short netting to zero: scale by the gross
        tot = sum(abs(wts[s]) for s in covered)
    w = np.zeros(len(m["symbols"]))
    for s in covered:
        w[pos[s]] = wts[s] / tot
    bm = benchmark(conn, m, cfg)
    if bm["kind"] == "NIFTY50_PROXY":
        dec = decompose(w, m["X"], m["F"], m["spec_var"], wb=bm["weights"])
    elif bm["kind"] == "NIFTY50_INDEX":
        dec = decompose(w, m["X"], m["F"], m["spec_var"], bench_x=bm["x"], bench_resid_var=bm["resid_var"])
    else:
        dec = decompose(w, m["X"], m["F"], m["spec_var"])
    return _format(m, w, dec, bm, label, {"covered": len(covered), "of": len(wts), "value_share": _num(share, 6),
                                          "uncovered": uncovered})


def _pct_ann(var):
    return _num(math.sqrt(max(var, 0.0) * ANNUAL) * 100, 4)


def _format(m, w, dec, bm, label, coverage) -> dict:
    F, factors, var = m["F"], m["factors"], dec["var"]
    sd = dec["sd"]
    fac = [{"factor": f, "kind": kind_of(f), "exposure": _num(dec["x"][k]),
            "factor_vol_pct_annual": _pct_ann(F[k, k]),
            "contribution_var_daily": _num(dec["contrib"][k], 12),
            "share_of_variance": _num(dec["contrib"][k] / var if var > 0 else None)}
           for k, f in enumerate(factors)]
    groups = {"market": 0.0, "industry": 0.0, "style": 0.0}
    for k, f in enumerate(factors):
        groups[kind_of(f)] += float(dec["contrib"][k])
    held = np.nonzero(w)[0]
    stocks = []
    for i in held:
        stocks.append({"symbol": m["symbols"][i], "weight": _num(w[i]), "industry": m["industry"][i],
                       "mcr_pct_annual": _num(dec["mcr"][i] * math.sqrt(ANNUAL) * 100, 4),
                       "risk_share": _num(dec["ctr"][i] / sd if sd > 0 else None),
                       "risk_share_factor": _num(dec["ctr_factor"][i] / sd if sd > 0 else None),
                       "risk_share_specific": _num(dec["ctr_specific"][i] / sd if sd > 0 else None),
                       "specific_vol_pct_annual": _pct_ann(m["spec_var"][i]),
                       "beta_exposure": _num(m["X"][i, factors.index("beta")]) if "beta" in factors else None,
                       "filled": m["filled"][i], "specific_from_bucket": m["spec_from_bucket"][i]})
    stocks.sort(key=lambda s: -(s["risk_share"] or 0))
    out = {"status": "OK", "portfolio": label, "as_of": str(m["as_of"]), "coverage": coverage,
           "risk": {"total_vol_pct_annual": _pct_ann(var), "factor_vol_pct_annual": _pct_ann(dec["var_factor"]),
                    "specific_vol_pct_annual": _pct_ann(dec["var_specific"]),
                    "total_vol_pct_daily": _num(sd * 100, 4), "var95_1d_pct": _num(1.6449 * sd * 100, 4),
                    "total_var_daily": _num(var, 12), "factor_var_daily": _num(dec["var_factor"], 12),
                    "specific_var_daily": _num(dec["var_specific"], 12),
                    "factor_share": _num(dec["var_factor"] / var if var > 0 else None),
                    "specific_share": _num(dec["var_specific"] / var if var > 0 else None)},
           "groups": {k: _num(v / var if var > 0 else None) for k, v in groups.items()} |
           {"specific": _num(dec["var_specific"] / var if var > 0 else None)},
           "factors": sorted(fac, key=lambda r: -abs(r["contribution_var_daily"] or 0)),
           "stocks": stocks,
           "checks": {"factor_plus_specific_minus_total": _num(dec["var_factor"] + dec["var_specific"] - var, 15),
                      "factor_contributions_minus_factor_var": _num(float(dec["contrib"].sum()) - dec["var_factor"], 15),
                      "stock_contributions_minus_vol": _num(float(dec["ctr"].sum()) - sd, 15)},
           "model": {"factors": len(factors), "covariance_sessions": m["n_obs"], "stocks": len(m["symbols"]),
                     "estimation_universe": int(m["estu"].sum())},
           "benchmark": {k: v for k, v in bm.items() if k in ("kind", "names", "description", "sessions", "r2")}}
    if "beta" in dec:
        xa = dec["x_active"]
        out["beta"] = {"value": _num(dec["beta"], 4), "benchmark": bm["kind"],
                       "note": "predicted: Cov(portfolio, benchmark) / Var(benchmark) under the model"}
        out["active"] = {"active_vol_pct_annual": _pct_ann(dec["var_active"]),
                         "active_factor_vol_pct_annual": _pct_ann(dec["var_active_factor"]),
                         "active_specific_vol_pct_annual": _pct_ann(dec["var_active"] - dec["var_active_factor"]),
                         "benchmark_vol_pct_annual": _pct_ann(dec["var_bench"]),
                         "top_active_exposures": sorted(
                             [{"factor": f, "kind": kind_of(f), "active_exposure": _num(xa[k])}
                              for k, f in enumerate(factors)], key=lambda r: -abs(r["active_exposure"] or 0))[:8]}
    else:
        out["beta"] = {"value": None, "benchmark": bm["kind"], "note": bm.get("description")}
        out["active"] = {"note": bm.get("description")}
    return out


# ── validation: the bias statistic ───────────────────────────────────────────

def bias_test(conn, cfg=None, weights: dict | None = None, label="book") -> dict:
    """Bias statistics over the last bias_window stored forecasts: the standard test portfolios, or
    one portfolio of fixed `weights` (renormalised each session over the names with a forecast and
    a next-session return)."""
    cfg = cfg if cfg is not None else settings()
    covs = conn.execute("SELECT as_of, factors_json, cov_json FROM quant_risk_covariance ORDER BY as_of DESC LIMIT ?",
                        (int(cfg["bias_window"]) + 1,)).fetchall()
    covs = {_d(r[0]): (json.loads(r[1]), np.array(json.loads(r[2]), float)) for r in covs}
    if not covs:
        return {"portfolios": [], "note": "no stored forecasts"}
    lo, hi = min(covs), max(covs)
    q = ("SELECT as_of, symbol, industry, mcap_cr, in_estu, " + ", ".join(_XCOLS) + ", ret_next, spec_var "
         "FROM quant_risk_exposure WHERE as_of>=? AND as_of<=? AND spec_var IS NOT NULL AND ret_next IS NOT NULL")
    args = [str(lo), str(hi)]
    if weights is not None:
        wmap = {str(k).upper(): float(v) for k, v in weights.items() if v}
        syms = sorted(wmap)
        if not syms:
            return {"portfolios": [], "note": "no weights"}
        q += f" AND symbol IN ({','.join('?' * len(syms))})"
        args += syms
    by = defaultdict(list)
    for r in conn.execute(q, args):
        by[_d(r[0])].append(tuple(r))
    days = sorted(d for d in by if d in covs)[-int(cfg["bias_window"]):]
    if not days:
        return {"portfolios": [], "note": "no forecast has a realised next-session return yet"}
    z = defaultdict(list)
    kinds = {}
    rng = np.random.default_rng(40)
    random_sets = None
    for d in days:
        rows = by[d]
        factors, F = covs[d]
        syms = [r[1] for r in rows]
        X = _design([r[2] for r in rows], np.array([r[5:5 + len(_XCOLS)] for r in rows], float), factors)
        ret = np.array([r[5 + len(_XCOLS)] for r in rows], float)
        spec = np.array([r[6 + len(_XCOLS)] for r in rows], float)
        ports = {}
        if weights is not None:
            ports[label] = np.array([wmap.get(s, 0.0) for s in syms])
            kinds[label] = "fixed weights"
        else:
            mcap = np.array([r[3] if r[3] is not None else np.nan for r in rows], float)
            estu = np.array([bool(r[4]) for r in rows]) & np.isfinite(mcap)
            cap = np.where(estu, mcap, 0.0)
            ports["market (cap-weighted estu)"] = cap
            ports["equal-weighted estu"] = estu.astype(float)
            ports["Nifty 50 proxy"] = proxy_weights(mcap, estu, cfg["benchmark_names"])
            kinds.update({"market (cap-weighted estu)": "market", "equal-weighted estu": "market",
                          "Nifty 50 proxy": "benchmark"})
            bk = size_buckets(np.where(estu, mcap, np.nan), "amfi")
            for b in ("LARGE", "MID", "SMALL"):
                m_ = np.array([x == b for x in bk])
                if m_.sum() >= 3:
                    ports[f"size {b}"] = np.where(m_, cap, 0.0)
                    kinds[f"size {b}"] = "size"
            for g in sorted(set(r[2] for r in rows)):
                m_ = estu & np.array([r[2] == g for r in rows])
                if m_.sum() >= 3:
                    ports[f"industry {g}"] = np.where(m_, cap, 0.0)
                    kinds[f"industry {g}"] = "industry"
            if random_sets is None:
                pool = sorted(np.array(syms, dtype=object)[estu])
                k = min(int(cfg["bias_random_size"]), len(pool))
                random_sets = [set(rng.choice(pool, k, replace=False)) for _ in range(int(cfg["bias_random_portfolios"]))] \
                    if k >= 3 else []
            for j, members in enumerate(random_sets):
                ports[f"random {j + 1}"] = np.array([1.0 if s in members else 0.0 for s in syms])
                kinds[f"random {j + 1}"] = "random"
        names = list(ports)
        Wp = np.array([ports[n] for n in names], float).T                  # n x P
        tot = Wp.sum(0)
        good = tot > 0
        Wp[:, good] = Wp[:, good] / tot[good]
        XW = X.T @ Wp
        var = np.einsum("kp,kl,lp->p", XW, F, XW) + (Wp ** 2 * spec[:, None]).sum(0)
        realised = Wp.T @ ret
        for p, nme in enumerate(names):
            if good[p] and var[p] > 0:
                z[nme].append(float(realised[p] / math.sqrt(var[p])))
    res = []
    for nme, zs in z.items():
        b = bias_statistic(zs)
        res.append({"portfolio": nme, "kind": kinds.get(nme), "sessions": b["T"], "bias": _num(b["bias"], 4),
                    "band": [_num(x, 4) for x in b["band"]] if b["band"] else None, "in_band": b["in_band"],
                    "mean_z": _num(b["mean_z"], 4)})
    scored = [r for r in res if r["bias"] is not None]
    order = {"market": 0, "benchmark": 1, "size": 2, "random": 3, "industry": 4, "fixed weights": 0}
    res.sort(key=lambda r: (order.get(r["kind"], 9), r["portfolio"]))
    summary = {"from": str(days[0]), "to": str(days[-1]), "sessions": len(days), "portfolios": len(scored),
               "in_band": sum(1 for r in scored if r["in_band"]),
               "median_bias": _num(float(np.median([r["bias"] for r in scored])), 4) if scored else None,
               "note": "bias = std of realised / predicted portfolio returns: ~1 when the forecast is right, > 1 "
                       "under-forecast, < 1 over-forecast; band = 95% for normal returns"}
    return {"portfolios": res, "summary": summary, "computed_at": datetime.now().isoformat(timespec="seconds")}


# ── read-outs for the API ─────────────────────────────────────────────────────

def status(conn) -> dict:
    cfg = settings()
    out = {"enabled": cfg["enabled"], "settings": {k: v for k, v in cfg.items() if k in DEFAULTS},
           "warnings": cfg.get("_warnings", []), "styles": STYLE_INFO}
    try:
        st = get_state(conn, "model")
    except Exception:
        st = None
    if not st:
        out.update({"status": "NOT_BUILT", "note": "the model has not been built yet (nightly job risk_model, "
                                                   "or POST /api/quant/risk-model/run)"})
        return out
    out["model"] = {k: st.get(k) for k in ("model_hash", "version", "factors", "industries", "merged",
                                           "industry_counts", "universe_size", "built_at")}
    x = conn.execute("SELECT MIN(as_of), MAX(as_of), COUNT(DISTINCT as_of), COUNT(*) FROM quant_risk_exposure").fetchone()
    out["exposures"] = {"first": str(x[0])[:10] if x[0] else None, "last": str(x[1])[:10] if x[1] else None,
                        "sessions": x[2], "rows": x[3]}
    if x[1]:
        rows = _rows_on(conn, _d(x[1]), "in_estu, filled")
        n = len(rows)
        fl = Counter(f for r in rows for f in (r["filled"] or "").split(",") if f)
        out["exposures"].update({"latest_universe": n, "latest_estimation_universe": sum(1 for r in rows if r["in_estu"]),
                                 "filled_share_latest": {s: _num(fl.get(s, 0) / n if n else None, 4)
                                                         for s in (*STYLES, "industry")}})
    f = conn.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM quant_risk_regression WHERE r2 IS NOT NULL").fetchone()
    r2 = [r[0] for r in conn.execute("SELECT r2 FROM quant_risk_regression WHERE r2 IS NOT NULL ORDER BY date DESC "
                                     "LIMIT 20")]
    out["factor_returns"] = {"first": str(f[0])[:10] if f[0] else None, "last": str(f[1])[:10] if f[1] else None,
                             "sessions": f[2], "r2_latest": _num(r2[0], 4) if r2 else None,
                             "r2_mean_20": _num(float(np.mean(r2)), 4) if r2 else None}
    c = conn.execute("SELECT MIN(as_of), MAX(as_of), COUNT(*) FROM quant_risk_covariance").fetchone()
    out["covariance"] = {"first": str(c[0])[:10] if c[0] else None, "last": str(c[1])[:10] if c[1] else None,
                         "sessions": c[2]}
    if c[1]:
        r = conn.execute("SELECT SUM(spec_from_bucket), COUNT(*) FROM quant_risk_exposure WHERE as_of=?",
                         (str(c[1])[:10],)).fetchone()
        out["specific"] = {"latest_from_bucket": int(r[0] or 0), "latest_stocks": int(r[1] or 0)}
    out["bias"] = get_state(conn, "bias")
    try:
        j = conn.execute("SELECT status, rows_processed, error_msg, end_time FROM pipeline_log WHERE "
                         "job_name='risk_model' ORDER BY id DESC LIMIT 1").fetchone()
        out["last_job"] = {"status": j[0], "rows": j[1], "error": j[2], "at": str(j[3]) if j[3] else None} if j else None
    except Exception:
        out["last_job"] = None
    if c[1]:
        out["status"] = "OK"
    elif x[1]:
        out["status"] = "PARTIAL"
        out["note"] = f"exposures stored; a forecast needs {cfg['cov_min_obs']} sessions of factor returns"
    else:
        out["status"] = "NO_DATA"
        out["note"] = ("no session could be modelled yet -- see last_job; usually too few stocks with a filed "
                       "share count (fundamental_data.shares_out: python -m data.nse_filings)")
    return out


def factors_summary(conn, as_of=None, days=20) -> dict:
    """Latest factor returns (and over the last `days` sessions), volatilities, correlations, R^2."""
    try:
        cv = _cov_on(conn, as_of)
    except Exception:
        cv = None
    if not cv:
        return {"status": "NOT_BUILT", "factors": [], "note": "no factor covariance stored yet"}
    factors, F = cv["factors"], cv["F"]
    sd = np.sqrt(np.clip(np.diag(F), 0, None))
    with np.errstate(invalid="ignore", divide="ignore"):
        C = np.where(np.outer(sd, sd) > 0, F / np.outer(sd, sd), 0.0)
    np.fill_diagonal(C, 1.0)
    fd = [r[0] for r in conn.execute("SELECT DISTINCT date FROM quant_risk_factor_return WHERE date<=? ORDER BY date "
                                     "DESC LIMIT ?", (str(cv["as_of"]), int(days)))]
    rets = defaultdict(dict)
    if fd:
        for dt, fac, ret, t in conn.execute("SELECT date, factor, ret, t_stat FROM quant_risk_factor_return WHERE date>=? "
                                            "AND date<=?", (str(fd[-1])[:10], str(fd[0])[:10])):
            rets[fac][_d(dt)] = (ret, t)
    last = _d(fd[0]) if fd else None
    rows = []
    for k, f in enumerate(factors):
        series = rets.get(f, {})
        lr = series.get(last, (None, None)) if last else (None, None)
        cum = float(np.prod([1 + (v[0] or 0.0) for v in series.values()]) - 1) if series else None
        rows.append({"factor": f, "kind": kind_of(f), "return_pct": _num(lr[0] * 100 if lr[0] is not None else None, 4),
                     "t_stat": _num(lr[1], 3), f"cum_return_pct_{days}d": _num(cum * 100 if cum is not None else None, 4),
                     "vol_pct_annual": _pct_ann(F[k, k])})
    reg = conn.execute("SELECT date, n_stocks, r2 FROM quant_risk_regression WHERE date<=? AND r2 IS NOT NULL ORDER BY "
                       "date DESC LIMIT ?", (str(cv["as_of"]), int(days))).fetchall()
    return {"status": "OK", "as_of": str(cv["as_of"]), "factor_return_date": str(last) if last else None,
            "covariance_sessions": cv["n_obs"], "factors": rows,
            "correlation": {"factors": factors, "values": [[_num(v, 3) for v in row] for row in C]},
            "regression": {"latest": {"date": str(reg[0][0])[:10], "n_stocks": reg[0][1], "r2": _num(reg[0][2], 4)}
                           if reg else None, f"mean_r2_{days}d": _num(float(np.mean([r[2] for r in reg])), 4) if reg else None},
            "note": "factor returns are daily (exposure date -> next session); volatilities annualised from the "
                    "EWMA / Newey-West covariance"}


def run_scheduled(trade_date=None, rebuild=False) -> dict:
    """Nightly (pipeline/w39_jobs.py, run_job compatible). SKIPPED when risk_model.enabled is false."""
    from db.schema import get_connection
    cfg = settings()
    if not cfg["enabled"]:
        return {"status": "SKIPPED", "rows": 0, "reason": "risk_model.enabled is false"}
    conn = get_connection()
    try:
        return update(conn, trade_date, cfg=cfg, rebuild=rebuild)
    finally:
        conn.close()
