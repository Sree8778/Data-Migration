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
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.api.context import AppContext
from src.api.deps import identify
from src.api.errors import register_error_handlers
from src.api.routes import admin, audit, governance, mapping, pipeline, sources
from src.api.settings import Settings
from src.db.session import create_db_engine, create_session_factory
from src.seeds.seed_sap_catalog import seed_sap_catalog

BASE = Path(__file__).parent
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

PAGES = {
    "/": ("index.html", "Workspace"),
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.auto_seed:
            try:
                seed_sap_catalog(ctx.metadata)  # idempotent; needs `alembic upgrade head` to have run
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger("migration.api").warning("catalog seeding skipped: %s", exc)
        yield

    app = FastAPI(
        title="ERP Migration Accelerator API", version="1.0.0", lifespan=lifespan,
        description="Legacy -> SAP S/4HANA Business Partner migration: profile, map, transform, validate, "
                    "govern, load and reconcile.")
    app.state.ctx = ctx
    register_error_handlers(app)

    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"], allow_headers=["Content-Type", "X-User", "X-API-Key"],
        expose_headers=["Content-Disposition"], max_age=600)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi")):
            response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    api = APIRouter(prefix="/api", dependencies=[Depends(identify)])  # API-key mode protects every route
    for module in (admin, sources, mapping, governance, pipeline, audit):
        api.include_router(module.router)
    app.include_router(api)
    app.include_router(admin.public_router, prefix="/api")  # health: no credentials needed for probes

    templates = Jinja2Templates(directory=str(BASE / "templates"))
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    def page(template: str, title: str):
        async def render(request: Request, table_id: int = 0, run_id: int = 0):
            return templates.TemplateResponse(request, template, {"title": title, "table_id": table_id, "run_id": run_id})
        return render

    for path, (template, title) in PAGES.items():
        app.add_api_route(path, page(template, title), methods=["GET"], include_in_schema=False)
    return app
