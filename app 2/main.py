from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from . import db as database
from .config import get_settings
from .models import Base
import json
import logging
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from .context import new_request_id
from .routers import admin, auth, customer, internal, ui


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    s.validate_for_runtime()
    if database.engine is None:
        database.configure(s.effective_database_url)
    if s.env == "development":
        Base.metadata.create_all(database.engine)   # dev convenience; production uses `alembic upgrade head`
    yield


app = FastAPI(title="Citichoice Desk Hub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(admin.router)
app.include_router(internal.router)
app.include_router(ui.router)
app.include_router(auth.router)
app.include_router(customer.router)

log = logging.getLogger("hub")


STAFF_PREFIXES = ("/ui", "/auth")
API_PREFIXES = ("/admin", "/internal", "/customer", "/u/")


def _host(request: Request) -> str:
    return request.headers.get("host", "").split(":")[0].lower()


@app.middleware("http")
async def request_context(request: Request, call_next):
    s = get_settings()
    path = request.url.path
    # Two hostnames, two jobs. The staff UI host never serves the machine API and vice versa, so a Cloudflare
    # Access rule or an IP allow-list on one cannot be bypassed through the other.
    if s.staff_ui_host or s.api_host:
        h = _host(request)
        staff_path, api_path = path.startswith(STAFF_PREFIXES), path.startswith(API_PREFIXES)
        if path != "/healthz" and not ((h == s.staff_ui_host and staff_path) or (h == s.api_host and api_path)):
            return JSONResponse({"detail": "not found"}, status_code=404)
    # State-changing browser requests must come from our own origin (defence in depth next to the CSRF token).
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith(STAFF_PREFIXES) and s.staff_ui_host:
        origin = request.headers.get("origin")
        if origin and origin != f"https://{s.staff_ui_host}":
            return JSONResponse({"detail": "bad origin"}, status_code=403)
    rid = new_request_id()
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if path.startswith(STAFF_PREFIXES):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                                                       "base-uri 'none'; frame-ancestors 'none'")
    # Never log headers, bodies or query strings: they can carry keys, tokens and customer data.
    log.info(json.dumps({"request_id": rid, "method": request.method, "path": request.url.path,
                         "status": response.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1)}))
    return response


@app.get("/healthz")
def healthz():
    with database.engine.connect() as c:
        c.execute(text("select 1"))
    return {"ok": True}
