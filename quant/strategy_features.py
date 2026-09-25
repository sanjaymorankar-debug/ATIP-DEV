"""
Quant outputs as W3 strategy features (point in time).

    qf_<factor_id>      the factor's direction-adjusted score (0..100, higher =
                        better) stored for (symbol, as_of) -- e.g. qf_mom_12_1
    qc_<composite>      the composite's score -- e.g. qc_vqm, qc_mom_lowvol_liq
    ev_*                event features (quant/events.py)

QuantHistory(conn, start, end).on(as_of, symbol) -> {feature: value}. Only rows
stored for exactly that date are served (computed from data up to its close).
The latest version of each factor / composite is served. With quant.enabled
false no scores are computed, every qf_ / qc_ feature is None, and conditions
on them are simply not met.
"""

from __future__ import annotations

from datetime import date


def _d(x):
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


class QuantHistory:
    def __init__(self, conn, start=None, end=None):
        self._rows = {}
        q = "SELECT as_of, symbol, factor_key, kind, score FROM quant_factor_score"
        args, where = [], []
        if start:
            where.append("as_of>=?"); args.append(str(start))
        if end:
            where.append("as_of<=?"); args.append(str(end))
        if where:
            q += " WHERE " + " AND ".join(where)
        for a, s, key, kind, score in conn.execute(q, args):
            base = key.split("@", 1)[0]
            name = ("qc_" + base.split(":", 1)[1]) if kind == "composite" else ("qf_" + base)
            self._rows.setdefault((_d(a), s), {})[name] = score
        try:
            from quant.events import EventHistory
            self._events = EventHistory(conn, end)
        except Exception:
            self._events = None

    def on(self, as_of, symbol) -> dict:
        out = dict(self._rows.get((as_of, symbol), {}))
        if self._events is not None:
            out.update(self._events.on(as_of, symbol))
        return out
