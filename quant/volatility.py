"""
Volatility framework. All estimators use bars ending at the decision date
(callers pass a point-in-time bar list) and are annualised with sqrt(252), in %.

    close_to_close(bars, n)   stdev of log returns (historical / realised volatility)
    parkinson(bars, n)        range estimator: sqrt(mean(ln(H/L)^2) / (4 ln 2))
    garman_klass(bars, n)     0.5 ln(H/L)^2 - (2 ln 2 - 1) ln(C/O)^2
    downside(bars, n)         stdev of negative log returns only (semi-deviation)
    rolling(bars, n, step)    close_to_close over a moving window -> [(date, vol)]
    change(bars, short, long) short-window vol / long-window vol - 1
    regime(bars, n, history)  LOW / NORMAL / HIGH: today's n-day vol vs the 20th /
                              80th percentile of its own rolling history
    iv_rv_spread(iv, rv)      implied minus realised (needs an options IV; W6 has
                              no options data -- see derivatives.py)
"""

from __future__ import annotations

import math

ANN = math.sqrt(252) * 100


def _logrets(bars, n):
    b = bars[-(n + 1):]
    return [math.log(b[i].close / b[i - 1].close) for i in range(1, len(b)) if b[i - 1].close and b[i].close]


def _sd(xs):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def close_to_close(bars, n=20):
    if len(bars) < n + 1:
        return None
    s = _sd(_logrets(bars, n))
    return s * ANN if s is not None else None


def parkinson(bars, n=20):
    b = [x for x in bars[-n:] if x.high and x.low and x.high >= x.low > 0]
    if len(b) < n:
        return None
    return math.sqrt(sum(math.log(x.high / x.low) ** 2 for x in b) / (4 * math.log(2) * n)) * ANN


def garman_klass(bars, n=20):
    b = [x for x in bars[-n:] if x.high and x.low and x.open and x.close and x.high >= x.low > 0]
    if len(b) < n:
        return None
    v = sum(0.5 * math.log(x.high / x.low) ** 2 - (2 * math.log(2) - 1) * math.log(x.close / x.open) ** 2 for x in b) / n
    return math.sqrt(max(v, 0.0)) * ANN


def downside(bars, n=60):
    if len(bars) < n + 1:
        return None
    neg = [r for r in _logrets(bars, n) if r < 0]
    if len(neg) < 2:
        return 0.0
    return math.sqrt(sum(r * r for r in neg) / (len(_logrets(bars, n)) - 1)) * ANN


def rolling(bars, n=20, step=1):
    return [(bars[i].date, close_to_close(bars[:i + 1], n)) for i in range(n, len(bars), step)]


def change(bars, short=20, long=60):
    s, l = close_to_close(bars, short), close_to_close(bars, long)
    return (s / l - 1) if s is not None and l else None


def regime(bars, n=20, history=250):
    series = [v for _, v in rolling(bars[-(history + n + 1):], n) if v is not None]
    if len(series) < 30:
        return None
    today = series[-1]
    srt = sorted(series)
    lo, hi = srt[int(0.2 * (len(srt) - 1))], srt[int(0.8 * (len(srt) - 1))]
    return "LOW" if today <= lo else "HIGH" if today >= hi else "NORMAL"


def iv_rv_spread(iv, rv):
    return None if iv is None or rv is None else iv - rv
