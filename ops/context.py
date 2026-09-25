"""Per-request / per-job context carried into logs and audit (contextvars)."""

from __future__ import annotations

import contextvars
import uuid

request_id = contextvars.ContextVar("atip_request_id", default=None)
correlation_id = contextvars.ContextVar("atip_correlation_id", default=None)
tenant_id = contextvars.ContextVar("atip_tenant_id", default=None)
user_id = contextvars.ContextVar("atip_user_id", default=None)
job_name = contextvars.ContextVar("atip_job", default=None)


def new_id(prefix="req") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def snapshot() -> dict:
    return {"request_id": request_id.get(), "correlation_id": correlation_id.get(), "tenant_id": tenant_id.get(),
            "user_id": user_id.get(), "job": job_name.get()}
