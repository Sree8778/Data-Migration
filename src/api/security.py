"""Who may call what, how often, and the record of what they did.

Controls implemented here (the mapping to standards is in docs/compliance.md):

* Role-based authorisation from ONE table (`POLICY`), deny by default: a state-changing route that no entry covers needs the admin role. A test fails when a
  new route is added without an entry, so a forgotten check cannot ship (OWASP API5 broken function level authorisation; SOX segregation of duties).
* Audit trail of every state-changing call, every refusal (401/403/429) and every download of a report, in a tamper-evident chain (see services/audit_log).
  High-risk actions (approve, sign-off, load, rollback, keys, connections, erasure) are written BEFORE they run: if the trail cannot be written, they do not run.
* Throttling per caller, a stricter one for heavy calls, and a limit on failed log-ins per address (OWASP API4 unrestricted resource consumption, API2).
* A cap on JSON request bodies, and response headers that keep personal data out of caches.

Nothing here reads a request body: the trail holds identifiers and outcomes, never record values.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from src.api.context import AppContext
from src.api.deps import Principal, get_ctx, principal
from src.services.audit_log import AuditUnavailable

_ANY: frozenset = frozenset()   # authenticated is enough


def _roles(*names: str) -> frozenset:
    return frozenset(names)


# (methods, path pattern, roles allowed). First match wins. "admin" is allowed everywhere. An empty set = any authenticated caller.
POLICY: list[tuple[frozenset, re.Pattern, frozenset]] = [
    (_roles("GET"), re.compile(r"^/api/compliance(/|$)"), _roles("auditor")),
    (_roles("POST", "PUT", "DELETE"), re.compile(r"^/api/compliance(/|$)"), _roles("admin")),
    (_roles("GET", "POST", "PUT", "DELETE"), re.compile(r"^/api/keys(/|$)"), _roles("admin")),
    (_roles("POST", "PUT", "DELETE"), re.compile(r"^/api/connections(/|$)"), _roles("admin")),
    (_roles("GET"), re.compile(r"^/api/connections/[^/]+/(schemas|tables|columns|preview|files|containers)$"), _roles("mapper", "loader")),
    (_roles("POST"), re.compile(r"^/api/admin/"), _roles("admin")),
    (_roles("POST"), re.compile(r"^/api/targets/(test|diagnose-sap)$"), _roles("loader")),
    (_roles("POST"), re.compile(r"^/api/governance/(approve|reject)-mapping/"), _roles("approver")),
    (_roles("POST"), re.compile(r"^/api/governance/sign-off-load/"), _roles("approver")),
    (_roles("POST"), re.compile(r"^/api/governance/submit-mapping/"), _roles("mapper")),
    (_roles("POST"), re.compile(r"^/api/pipeline/(load|load-db|verify-db|rollback-db|recover-db)(/|$)"), _roles("loader")),
    (_roles("POST"), re.compile(r"^/api/pipeline/(run|jobs)(/|$)"), _roles("mapper", "loader")),
    (_roles("POST", "PUT", "DELETE"), re.compile(r"^/api/(mapping|mapping-sets|sources|profile|combine|quality|packs)(/|$)"), _roles("mapper")),
    (_roles("POST", "PUT", "DELETE"), re.compile(r"^/api/construction(/|$)"), _roles("mapper", "loader")),
    (_roles("POST"), re.compile(r"^/api/defects/[^/]+/sync-jira$"), _roles("mapper", "approver", "loader")),
    (_roles("POST"), re.compile(r"^/api/runs/[^/]+/delta$"), _roles("mapper", "loader")),
    (_roles("POST"), re.compile(r"^/api/duplicates/"), _roles("mapper", "approver")),
    (_roles("POST"), re.compile(r"^/api/tasks(/|$)"), _roles("mapper", "approver", "loader")),
    (_roles("POST"), re.compile(r"^/api/assistant/"), _ANY),
    (_roles("POST"), re.compile(r"^/api/guide/"), _ANY),
]
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_MUTATION_ROLES = _roles("admin")

HIGH_RISK = re.compile(r"^/api/(governance/(approve|reject)-mapping/|governance/sign-off-load/|pipeline/(load|load-db|rollback-db|recover-db)(/|$)|keys|connections|compliance/)")
HEAVY = re.compile(r"^/api/(pipeline/(run|load|load-db|verify-db)|sources/(register|import-db|import-file)|combine|mapping/generate|quality/assess|profile/)")
SENSITIVE_GET = re.compile(r"^/api/(runs/[^/]+/cockpit|reconciliation/[^/]+/download|compliance/)")


def required_roles(method: str, path: str) -> Optional[frozenset]:
    """The roles that may make this call: None = no rule and nothing to enforce (a read), a set = one of them (empty = any authenticated caller)."""
    for methods, pattern, roles in POLICY:
        if method in methods and pattern.match(path):
            return roles
    return DEFAULT_MUTATION_ROLES if method in MUTATING else None


def authorize(request: Request, who: Principal = Depends(principal), ctx: AppContext = Depends(get_ctx)) -> Principal:
    """Router-level dependency of /api: identifies the caller, checks the role, and puts high-risk actions on record before they run."""
    need = required_roles(request.method, request.url.path)
    if need and "admin" not in who.roles and not (who.roles & need):
        raise HTTPException(403, f"'{who.user}' may not do this: it needs the role {' or '.join(sorted(need))} (yours: {', '.join(sorted(who.roles))})")
    if request.method in MUTATING and HIGH_RISK.match(request.url.path):
        try:
            ctx.audit.append({"phase": "intent", "user": who.user, "roles": sorted(who.roles), "method": request.method, "path": request.url.path,
                              "request_id": getattr(request.state, "request_id", None)})
        except AuditUnavailable as exc:
            raise HTTPException(503, f"refused: this action must be on the audit trail first, and {exc}") from exc
    return who


# ------------------------------------------------------------------ throttling
class SlidingWindow:
    """In-memory counters per key over the last `window` seconds. One process, like the rest of the application's state."""

    def __init__(self, window: float = 60.0) -> None:
        self._window = window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int) -> float:
        """Count one request; returns 0 when it is allowed, else the seconds until it would be."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self._window:
                q.popleft()
            if len(q) >= limit:
                return max(1.0, self._window - (now - q[0]))
            q.append(now)
            if len(self._hits) > 20000:   # forget callers that went quiet
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self._window]:
                    self._hits.pop(k, None)
        return 0.0

    def count(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            return sum(1 for t in q if now - t <= self._window) if q else 0


FAILED_LOGINS_PER_MINUTE = 20


def install(app, ctx: AppContext) -> None:
    """Add the security middleware: request id, size cap, throttling, response headers, audit trail."""
    limiter, failures = SlidingWindow(), SlidingWindow()
    settings = ctx.settings
    max_json = settings.max_json_mb * 1024 * 1024

    def caller_key(request: Request) -> str:
        ip = request.client.host if request.client else "-"
        token = request.headers.get("x-api-key") or request.headers.get("x-user") or ""
        return f"{ip}|{hash(token) & 0xFFFFFFFF:08x}"

    def refuse(request: Request, status: int, detail: str, code: str, retry: float = 0.0) -> JSONResponse:
        response = JSONResponse({"detail": detail, "code": code}, status_code=status)
        if retry:
            response.headers["Retry-After"] = str(int(retry + 0.999))
        return response

    @app.middleware("http")
    async def security(request: Request, call_next):
        path = request.url.path
        incoming = request.headers.get("x-request-id", "")
        request.state.request_id = incoming if re.fullmatch(r"[A-Za-z0-9._-]{8,64}", incoming) else uuid.uuid4().hex[:16]
        api = path.startswith("/api") and not path.startswith("/api/health")
        started = time.perf_counter()
        response = None
        if api:
            ip = request.client.host if request.client else "-"
            if request.method in ("POST", "PUT", "PATCH") and "multipart" not in request.headers.get("content-type", "").lower():
                declared = request.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_json:
                    response = refuse(request, 413, f"the request body is larger than {settings.max_json_mb} MB", "BODY_TOO_LARGE")
            if response is None and failures.count(ip) >= FAILED_LOGINS_PER_MINUTE:
                response = refuse(request, 429, "too many failed log-ins from this address; try again in a minute", "TOO_MANY_FAILED_LOGINS", 60)
            if response is None and settings.rate_limit_per_minute:
                limit = settings.rate_limit_per_minute
                if request.method in MUTATING and HEAVY.match(path):
                    limit = max(5, limit // 10)
                wait = limiter.hit(f"{caller_key(request)}|{'heavy' if limit != settings.rate_limit_per_minute else 'all'}", limit)
                if wait:
                    response = refuse(request, 429, "too many requests; slow down", "RATE_LIMITED", wait)
        if response is None:
            response = await call_next(request)
        if api and response.status_code == 401:
            failures.hit(request.client.host if request.client else "-", 10 ** 9)

        response.headers["X-Request-ID"] = request.state.request_id
        if api:
            response.headers["Cache-Control"] = "no-store"   # responses carry personal data: no browser or proxy cache keeps them
            response.headers.setdefault("Pragma", "no-cache")
        else:
            response.headers.setdefault("Cache-Control", "no-cache")   # pages and scripts: the browser asks each time (a cheap 304), so an update is seen at once and not after a hard refresh
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

        if api:
            status = response.status_code
            if request.method in MUTATING or status in (401, 403, 429) or SENSITIVE_GET.match(path):
                who: Optional[Principal] = getattr(request.state, "principal", None)
                route = request.scope.get("route")
                entry = {
                    "phase": "done", "user": who.user if who else None, "roles": sorted(who.roles) if who else None,
                    "method": request.method, "path": path, "route": getattr(route, "path", None),
                    "ids": {k: v for k, v in request.path_params.items()} or None, "status": status,
                    "outcome": "ok" if status < 400 else "denied" if status in (401, 403) else "throttled" if status == 429 else "rejected" if status < 500 else "error",
                    "ip": request.client.host if request.client else None, "ms": round((time.perf_counter() - started) * 1000),
                    "request_id": request.state.request_id,
                }
                if who is None and status == 401:
                    entry["claimed_user"] = (request.headers.get("x-user") or "")[:64] or None   # untrusted: only what the caller said
                try:
                    ctx.audit.append({k: v for k, v in entry.items() if v is not None})
                except AuditUnavailable:
                    import logging
                    logging.getLogger("migration.audit").critical("AUDIT TRAIL NOT WRITTEN for %s %s (status %s)", request.method, path, status)
        return response
