"""FastAPI entrypoint for the Lead-to-Cash PoC service."""
import logging
from datetime import datetime, timezone

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.logging_config import setup_logging
from app.security import require_api_key

settings = get_settings()
setup_logging(settings.log_level)
log = logging.getLogger("app")

app = FastAPI(title="Lead-to-Cash PoC", version="0.1.0")


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
