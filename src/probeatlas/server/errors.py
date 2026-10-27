"""Consistent error envelope: ``{"error": {"code", "message", "details"}}``."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("probeatlas.api")


class APIError(Exception):
    """An error that renders as the standard JSON error envelope.

    ``code`` is a stable machine-readable identifier, ``message`` is human-readable, and ``details`` holds
    optional structured context (for example validation errors).
    """

    def __init__(self, status: int, code: str, message: str, details: Any = None, headers: dict | None = None):
        """Create an error with an HTTP ``status``, a stable ``code``, and a message."""
        super().__init__(message)
        self.status, self.code, self.message, self.details, self.headers = status, code, message, details, headers


def not_found(what: str = "resource") -> APIError:
    """Return a 404 :class:`APIError` for ``what``. Also used for resources in other workspaces."""
    return APIError(404, "not_found", f"{what} not found")


def _body(code: str, message: str, details: Any = None) -> dict:
    """Build the ``{"error": {...}}`` response body."""
    err: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"error": err}


def install_error_handlers(app: FastAPI) -> None:
    """Register exception handlers so every error response uses the standard envelope.

    Unhandled exceptions are logged with the request path and return a generic 500 without internals.
    """

    @app.exception_handler(APIError)
    async def _api_error(_req: Request, exc: APIError):
        """Render an :class:`APIError` with its status, code, details, and headers."""
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError):
        """Render request validation failures as 422 ``validation_error`` with per-field details."""
        details = [
            {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return JSONResponse(_body("validation_error", "request validation failed", details), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req: Request, exc: StarletteHTTPException):
        """Render framework HTTP errors (unknown route, wrong method) in the standard envelope."""
        code = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized"}.get(exc.status_code, "http_error")
        return JSONResponse(_body(code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(req: Request, exc: Exception):
        """Log an unexpected exception and return a generic 500 response."""
        log.exception("unhandled error", extra={"fields": {"path": req.url.path}})
        return JSONResponse(_body("internal_error", "internal server error"), status_code=500)
