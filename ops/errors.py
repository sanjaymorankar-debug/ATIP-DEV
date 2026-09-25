"""
Standard application errors.

AtipError(message, code=..., status=..., retryable=..., user_message=...)
    ValidationFailed      VALIDATION_FAILED       400  not retryable
    Unauthenticated       UNAUTHENTICATED         401
    PermissionDenied      PERMISSION_DENIED       403
    NotFound              NOT_FOUND               404
    Conflict              CONFLICT                409  (duplicate / idempotency clash)
    PayloadTooLarge       PAYLOAD_TOO_LARGE       413
    RateLimited           RATE_LIMITED            429  retryable (after Retry-After)
    DependencyUnavailable DEPENDENCY_UNAVAILABLE  503  retryable (broker / data source / db)
    DependencyTimeout     DEPENDENCY_TIMEOUT      504  retryable
    DataQualityError      DATA_QUALITY            422  not retryable
    TradingSafetyError    TRADING_SAFETY          409  NEVER retried
    InternalError         INTERNAL                500

API error envelope (errors raised as AtipError, and any unhandled exception):
    {"error": {"code", "message", "request_id", "retryable"}}
The message is the user-safe one; internal detail and stack traces go to the log
only (with the request id), never to the client. Existing W1-W7 routes keep their
{"error": "..."} bodies (unchanged); this envelope covers the new failures.
"""

from __future__ import annotations


class AtipError(Exception):
    code, status, retryable = "INTERNAL", 500, False
    user_message = "internal error"

    def __init__(self, message: str = "", *, user_message: str | None = None, details: dict | None = None):
        super().__init__(message or self.user_message)
        self.user_message = user_message or (message if self.status < 500 else self.user_message)
        self.details = details or {}


def _mk(name, code, status, retryable, msg):
    return type(name, (AtipError,), {"code": code, "status": status, "retryable": retryable, "user_message": msg})


ValidationFailed = _mk("ValidationFailed", "VALIDATION_FAILED", 400, False, "invalid request")
Unauthenticated = _mk("Unauthenticated", "UNAUTHENTICATED", 401, False, "authentication required")
PermissionDenied = _mk("PermissionDenied", "PERMISSION_DENIED", 403, False, "not permitted")
NotFound = _mk("NotFound", "NOT_FOUND", 404, False, "not found")
Conflict = _mk("Conflict", "CONFLICT", 409, False, "conflict")
PayloadTooLarge = _mk("PayloadTooLarge", "PAYLOAD_TOO_LARGE", 413, False, "request body too large")
RateLimited = _mk("RateLimited", "RATE_LIMITED", 429, True, "too many requests")
DependencyUnavailable = _mk("DependencyUnavailable", "DEPENDENCY_UNAVAILABLE", 503, True, "a dependency is unavailable")
DependencyTimeout = _mk("DependencyTimeout", "DEPENDENCY_TIMEOUT", 504, True, "a dependency timed out")
DataQualityError = _mk("DataQualityError", "DATA_QUALITY", 422, False, "data failed quality checks")
TradingSafetyError = _mk("TradingSafetyError", "TRADING_SAFETY", 409, False, "blocked by a trading safety control")
InternalError = _mk("InternalError", "INTERNAL", 500, False, "internal error")


def envelope(code, message, request_id=None, retryable=False) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id, "retryable": retryable}}


def install_handlers(app):
    """Register the envelope for AtipError and for unhandled exceptions."""
    import logging

    from fastapi.responses import JSONResponse

    from ops import context
    log = logging.getLogger("atip.api")

    @app.exception_handler(AtipError)
    async def _atip(request, exc: AtipError):
        rid = context.request_id.get()
        if exc.status >= 500:
            log.error(f"{exc.code} {request.method} {request.url.path}: {exc}", extra={"error_code": exc.code})
        return JSONResponse(envelope(exc.code, exc.user_message, rid, exc.retryable), status_code=exc.status)

    @app.exception_handler(Exception)
    async def _unhandled(request, exc: Exception):
        rid = context.request_id.get()
        log.exception(f"INTERNAL {request.method} {request.url.path} (request {rid})", extra={"error_code": "INTERNAL"})
        return JSONResponse(envelope("INTERNAL", "internal error", rid), status_code=500)
