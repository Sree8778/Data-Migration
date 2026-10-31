"""FastAPI application factory.

Run locally:   uvicorn src.api.main:create_app --factory --port 8000
Interactive API docs: /docs      Web UI: /

The factory takes an optional `AppContext` so tests (and embedding applications) can inject a
database session factory, a scripted LLM, a sandbox SAP client, etc.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.api.context import AppContext
from src.api import security
from src.api.errors import register_error_handlers
from src.api.routes import admin, assistant, audit, combine, compliance, connections, construction, duplicates, fix, governance, guide, insight, keys, mapping, mapping_target, packs, pipeline, quality, sources, tasks
from src.api.settings import Settings
from src.db.session import create_db_engine, create_session_factory
from src.seeds.seed_sap_catalog import seed_sap_catalog

BASE = Path(__file__).parent
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'")  # 'self': the console hosts the pages as tabs

PAGES = {
    "/": ("console.html", "Management Center"),
    "/ui/start": ("start.html", "Start Page"),
    "/ui/sources": ("index.html", "Sources & Runs"),
    "/ui/combine": ("combine.html", "Combine Tables"),
    "/ui/dashboard": ("dashboard.html", "Programme Dashboard"),
    "/ui/compliance": ("compliance.html", "Compliance"),
    "/ui/quality/{table_id}": ("quality.html", "Data Quality"),
    "/ui/duplicates/{run_id}": ("duplicates.html", "Duplicate Review"),
    "/ui/tasks": ("tasks.html", "Stewardship Tasks"),
    "/ui/packs": ("packs.html", "Content Packs"),
    "/ui/profiler/{table_id}": ("profiler.html", "Table Profiler"),
    "/ui/studio/{table_id}": ("studio.html", "Mapping Studio"),
    "/ui/governance/{run_id}": ("governance.html", "Defect & Governance Hub"),
    "/ui/certificate/{run_id}": ("certificate.html", "Cockpit Export & Reconciliation Certificate"),
}


def create_app(ctx: Optional[AppContext] = None, settings: Optional[Settings] = None) -> FastAPI:
    if ctx is None:
        settings = settings or Settings.from_env()
        ctx = AppContext(settings, create_session_factory(create_db_engine()))
    settings = ctx.settings
    if settings.require_auth and not settings.api_keys:
        raise RuntimeError("MIGRATION_REQUIRE_AUTH is set but MIGRATION_API_KEYS is not: refusing to start without authentication")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.auto_seed:
            try:
                seed_sap_catalog(ctx.metadata)  # idempotent; needs `alembic upgrade head` to have run
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger("migration.api").warning("catalog seeding skipped: %s", exc)
        if not settings.api_keys:
            import logging
            logging.getLogger("migration.api").warning(
                "AUTHENTICATION IS OFF (dev-header mode): any caller can name itself in the X-User header, approvers included. "
                "Set MIGRATION_API_KEYS (and MIGRATION_REQUIRE_AUTH=true) on any shared server.")
        try:
            for run_id in ctx.executor.fail_interrupted_runs():
                import logging
                logging.getLogger("migration.api").warning("run %s was still RUNNING when the server started; marked FAILED (interrupted)", run_id)
        except Exception as exc:  # noqa: BLE001 - never stop the server from starting
            import logging
            logging.getLogger("migration.api").warning("could not check for interrupted runs: %s", exc)
        ctx.start_maintenance()
        yield
        ctx.stop_maintenance()
        ctx.jobs.shutdown()

    app = FastAPI(
        title="ERP Migration Accelerator API", version="1.0.0", lifespan=lifespan,
        description="Legacy -> SAP S/4HANA Business Partner migration: profile, map, transform, validate, "
                    "govern, load and reconcile.")
    app.state.ctx = ctx
    register_error_handlers(app)
    security.install(app, ctx)   # request id, throttling, size cap, audit trail, response headers

    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"], allow_headers=["Content-Type", "X-User", "X-API-Key", "X-Request-ID"],
        expose_headers=["Content-Disposition", "X-Request-ID", "Retry-After"], max_age=600)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi")):
            response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    api = APIRouter(prefix="/api", dependencies=[Depends(security.authorize)])  # identifies the caller, checks the role of every route, puts high-risk actions on record first
    for module in (admin, sources, combine, compliance, quality, insight, duplicates, tasks, packs, connections, mapping, mapping_target, fix, guide, governance, pipeline, audit, construction, keys, assistant):
        api.include_router(module.router)
    app.include_router(api)
    app.include_router(admin.public_router, prefix="/api")  # health: no credentials needed for probes

    templates = Jinja2Templates(directory=str(BASE / "templates"))
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    def page(template: str, title: str):
        async def render(request: Request, table_id: int = 0, run_id: int = 0):
            embed = request.query_params.get("embed") == "1"  # the console hosts pages as tabs: no page chrome of their own
            return templates.TemplateResponse(request, template, {"title": title, "table_id": table_id, "run_id": run_id, "embed": embed})
        return render

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(BASE / "static" / "favicon.svg", media_type="image/svg+xml")

    for path, (template, title) in PAGES.items():
        app.add_api_route(path, page(template, title), methods=["GET"], include_in_schema=False)
    return app
