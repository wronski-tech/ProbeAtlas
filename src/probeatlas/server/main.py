"""FastAPI application factory for the control-plane API (``probeatlas-api``)."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from probeatlas import __version__
from probeatlas.common.logs import log_fields, setup_logging

from .api import agents, alerts, auth, keys, measurements, monitors, overview
from .config import get_settings
from .db import get_engine, new_session
from .errors import install_error_handlers
from .models import EnrollmentToken
from .security import PREFIX_ENROLLMENT, token_hash

log = logging.getLogger("probeatlas.api")

DESCRIPTION = """
ProbeAtlas measures availability and latency of public internet endpoints from
operator-placed monitoring agents.

**Authentication.** Send `Authorization: Bearer <token>`.
* API keys (`pak_...`) are bound to one workspace.
* Session tokens (`pas_...`, from `/v1/auth/login`) also need an `X-Workspace-Id` header.

**Pagination.** List endpoints return `{"items": [...], "next_cursor": "..."}`.
Pass `cursor=<next_cursor>` to get the next page; `next_cursor` is `null` on the last page.

**Errors.** Every error has the shape `{"error": {"code", "message", "details"}}`.
Validation errors use HTTP 422 and code `validation_error` with per-field `details`.

**Rate limits.** Requests are limited per workspace (default 300/minute) and
authentication endpoints per client IP. Exceeding a limit returns HTTP 429 with
code `rate_limited` and a `Retry-After` header.

**Units.** Durations in milliseconds, percentages 0-100, timestamps UTC ISO 8601.
Fields that do not apply or were not measured are `null`.
"""


def bootstrap_enrollment_token() -> None:
    """Register PROBEATLAS_BOOTSTRAP_ENROLLMENT_TOKEN (local development convenience)."""
    settings = get_settings()
    token = settings.bootstrap_enrollment_token
    if not token:
        return
    if not token.startswith(PREFIX_ENROLLMENT + "_") or len(token) < 24:
        raise SystemExit("PROBEATLAS_BOOTSTRAP_ENROLLMENT_TOKEN must start with 'pae_' and be >= 24 chars")
    if settings.is_production:
        log.warning("bootstrap enrollment token configured in production; prefer probeatlas-admin tokens")
    with new_session() as db:
        db.execute(
            pg_insert(EnrollmentToken)
            .values(token_hash=token_hash(token), description="bootstrap (env)",
                    region=settings.bootstrap_enrollment_region, max_uses=1000)
            .on_conflict_do_nothing(index_elements=["token_hash"])
        )
        db.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup hook: warn about development-only settings and register the bootstrap enrollment token."""
    settings = get_settings()
    if settings.allow_private_targets:
        log.warning("PROBEATLAS_ALLOW_PRIVATE_TARGETS is enabled: private hostnames accepted (development only)")
    bootstrap_enrollment_token()
    yield


def create_app() -> FastAPI:
    """Build the FastAPI app with error handlers, CORS, access logging, routers, and ops endpoints."""
    settings = get_settings()
    app = FastAPI(title="ProbeAtlas API", version=__version__, description=DESCRIPTION, lifespan=lifespan)
    install_error_handlers(app)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Workspace-Id"],
        )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        """Log each request (method, path, status, duration) and add ``X-Content-Type-Options``."""
        start = time.perf_counter()
        response = await call_next(request)
        if request.url.path not in ("/healthz", "/metrics"):
            log.info("request", extra=log_fields(method=request.method, path=request.url.path,
                                                 status=response.status_code,
                                                 duration_ms=round((time.perf_counter() - start) * 1000, 1)))
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    for r in (auth.router, keys.router, monitors.router, measurements.router, alerts.router, agents.router,
              overview.router, agents.protocol):
        app.include_router(r)

    @app.get("/healthz", tags=["ops"])
    def healthz():
        """Liveness probe: the process is up. Does not touch the database."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["ops"])
    def readyz():
        """Readiness probe: the database answers ``SELECT 1``."""
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    def prom_metrics():
        """Expose Prometheus metrics for this API process."""
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()


def run() -> None:
    """Run the API with uvicorn on port 8000 using ProbeAtlas JSON logging."""
    import uvicorn

    setup_logging("api")
    uvicorn.run("probeatlas.server.main:app", host="0.0.0.0", port=8000, log_config=None,  # noqa: S104
                proxy_headers=True)
