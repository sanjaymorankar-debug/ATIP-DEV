"""
W40 API: meta-labelling of the technical signals (ml/meta_label.py). Read-only; authz (enterprise/authz.py):
GET /api/research/ -> research:read.

    GET  /api/research/meta-label/report     the settings, the adopted model (if any), the latest training
                                             run's out-of-fold report (precision / recall / F1 / log loss / AUC
                                             vs the base rate, expectancy of kept vs all signals, bet sizes,
                                             verdict ADOPTABLE / NO_EDGE), recent runs and the latest scores

Training and scoring are CLI / scheduled (python -m ml.meta_label train | score); today's scores appear
on /signals through GET /api/signals/technical when meta_label.enabled and a model is adopted.
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import math

from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool


def _finite(obj):
    """NaN / inf -> None and numpy scalars -> Python, so one odd number cannot break the response."""
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        try:
            obj = obj.item()
        except (TypeError, ValueError):
            return obj
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def register(app, guard, Req, get_connection, json_safe):
    def call(fn):
        conn = get_connection()
        try:
            return JSONResponse(_finite(json_safe(fn(conn))))
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()

    @app.get("/api/research/meta-label/report")
    async def api_meta_label_report():
        from ml.meta_label import report
        return await run_in_threadpool(call, report)
