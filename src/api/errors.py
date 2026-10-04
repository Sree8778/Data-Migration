"""Maps domain exceptions onto HTTP responses with one consistent body: {"detail": ..., "code": ...}."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.context import NotConfiguredError
from src.services.cockpit_generator import CockpitConsistencyError
from src.services.execution_orchestrator import ExecutionError
from src.services.governance_service import (
    GateBlockedError,
    InvalidTransitionError,
    NotAuthorizedError,
    SegregationOfDutiesError,
)
from src.services.llm_mapper_service import LLMUnavailableError
from src.services.metadata_service import ConflictError, MappingValidationError, NotFoundError
from src.services.sap_client import SapAuthError, SapUnavailableError
from src.services.sap_loader_service import LoadInputError, LoadStateError
from src.services.transformation_engine import TransformationCompileError

log = logging.getLogger("migration.api")

# (exception, status, code) - first match wins, so subclasses go before their bases
_MAP: list[tuple[type[Exception], int, str]] = [
    (NotFoundError, 404, "NOT_FOUND"),
    (ConflictError, 409, "CONFLICT"),
    (MappingValidationError, 422, "INVALID_MAPPING"),
    (SegregationOfDutiesError, 403, "SEGREGATION_OF_DUTIES"),
    (NotAuthorizedError, 403, "NOT_AUTHORIZED"),
    (InvalidTransitionError, 409, "INVALID_TRANSITION"),
    (LoadStateError, 409, "LOAD_STATE"),
    (LoadInputError, 422, "INVALID_LOAD_INPUT"),
    (LLMUnavailableError, 503, "LLM_UNAVAILABLE"),
    (SapAuthError, 502, "SAP_AUTH"),
    (SapUnavailableError, 502, "SAP_UNAVAILABLE"),
    (TransformationCompileError, 422, "COMPILE_ERROR"),
    (CockpitConsistencyError, 422, "COCKPIT_INCONSISTENT"),
    (FileNotFoundError, 404, "FILE_NOT_FOUND"),
    (NotConfiguredError, 400, "NOT_CONFIGURED"),
    (ValueError, 422, "INVALID_REQUEST"),
]


def _json(status: int, code: str, detail) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "code": code})


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GateBlockedError)
    async def _gate(_: Request, exc: GateBlockedError):
        return _json(409, "GATE_BLOCKED", {"message": str(exc), **exc.result.model_dump(mode="json")})

    @app.exception_handler(ExecutionError)
    async def _run_failed(_: Request, exc: ExecutionError):
        return _json(500, "RUN_FAILED", {"message": str(exc), "run_id": exc.run_id})

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        # drop the echoed `input`: request bodies may carry values that must not be reflected back
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return _json(422, "VALIDATION_ERROR", errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code, headers=getattr(exc, "headers", None),
                            content={"detail": exc.detail, "code": f"HTTP_{exc.status_code}"})

    async def _domain(_: Request, exc: Exception):
        for exc_type, status, code in _MAP:
            if isinstance(exc, exc_type):
                return _json(status, code, str(exc))
        log.exception("unhandled error", exc_info=exc)  # pragma: no cover
        return _json(500, "INTERNAL_ERROR", "internal server error")  # pragma: no cover

    # Registered per class (not via a blanket Exception handler) so Starlette's ExceptionMiddleware
    # serves them as normal responses; unexpected errors still surface as 500s.
    for exc_type, _status, _code in _MAP:
        app.add_exception_handler(exc_type, _domain)
