"""FastAPI entrypoint for the Lead-to-Cash PoC service."""
import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.logging_config import setup_logging
from app.request_context import request_id_var
from app.routers import approvals, documents, financial, inquiries, ops
from app.security import require_api_key

settings = get_settings()
setup_logging(settings.log_level)
log = logging.getLogger("app")

app = FastAPI(title="Lead-to-Cash PoC", version="0.6.0")
app.include_router(inquiries.router)
app.include_router(approvals.router)
app.include_router(documents.router)
app.include_router(financial.router)
app.include_router(ops.router)

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a correlation id to every request (taken from n8n's X-Request-ID when valid)."""
    incoming = request.headers.get("x-request-id", "")
    rid = incoming if _SAFE_REQUEST_ID.match(incoming) else str(uuid.uuid4())
    request.state.request_id = rid
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = rid
    return response


def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, headers=getattr(exc, "headers", None),
                        content={"error": "http_error", "detail": exc.detail, "request_id": _rid(request)})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    details = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422,
                        content={"error": "validation_error", "detail": details, "request_id": _rid(request)})


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    # Log full details server-side; never leak internals to the caller.
    log.exception("Unhandled error", extra={"request_id": _rid(request)})
    return JSONResponse(status_code=500,
                        content={"error": "internal_error", "detail": "Unexpected server error",
                                 "request_id": _rid(request)})


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict:
    """Public liveness check (no sensitive data)."""
    return {"status": "ok"}


@app.get("/health/db")
def health_db():
    try:
        from app.db import check_db
        check_db()
        return {"status": "ok", "database": "reachable"}
    except Exception:
        log.exception("Database health check failed")
        return JSONResponse(status_code=503, content={"status": "error", "database": "unreachable"})


@app.get("/v1/ping", dependencies=[Depends(require_api_key)])
def ping() -> dict:
    """Authenticated connectivity check used by the n8n '00 - Connectivity Check' workflow."""
    return {
        "status": "ok",
        "service": "lead-to-cash-api",
        "env": settings.app_env,
        "llm_provider": settings.llm_provider,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }
