"""Stable public HTTP errors that never expose exception internals."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from fastapi.responses import JSONResponse


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PublicErrorSpec:
    """A versionable public error contract independent of exception text or type."""

    status_code: int
    code: str
    message: str


AUTHENTICATION_FAILED = PublicErrorSpec(401, "authentication_failed", "Authentication failed")
FORBIDDEN = PublicErrorSpec(403, "forbidden", "The requested action is not permitted")
INVALID_REQUEST = PublicErrorSpec(400, "invalid_request", "The request is invalid")
UNPROCESSABLE_REQUEST = PublicErrorSpec(422, "invalid_request", "The request could not be processed")
NOT_FOUND = PublicErrorSpec(404, "not_found", "The requested resource was not found")
CONFLICT = PublicErrorSpec(409, "conflict", "The request conflicts with the current resource state")
# Answered by the request size limit before any route or handler runs. It is not
# in the status table below: no handler raises it, and nothing is logged with an
# exception for it.
REQUEST_TOO_LARGE = PublicErrorSpec(413, "request_too_large", "The request body is too large")
UPSTREAM_FAILURE = PublicErrorSpec(502, "upstream_failure", "An upstream service failed")
INTERNAL_ERROR = PublicErrorSpec(500, "internal_error", "An internal error occurred")


_PUBLIC_ERRORS_BY_STATUS: dict[int, PublicErrorSpec] = {
    spec.status_code: spec
    for spec in (
        INVALID_REQUEST,
        AUTHENTICATION_FAILED,
        FORBIDDEN,
        NOT_FOUND,
        CONFLICT,
        UNPROCESSABLE_REQUEST,
        UPSTREAM_FAILURE,
        INTERNAL_ERROR,
    )
}


def public_exception_response(exc: BaseException, *, status_code: int) -> JSONResponse:
    """Log an exception privately and return its registered stable public family.

    Unknown status codes fail closed to ``internal_error``. Exception messages and
    types are intentionally absent from the serialized response.
    """

    spec = _PUBLIC_ERRORS_BY_STATUS.get(status_code, INTERNAL_ERROR)
    logger.exception(
        "HTTP request failed with public error code=%s status=%d",
        spec.code,
        spec.status_code,
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={"public_error_code": spec.code, "public_error_status": spec.status_code},
    )
    return JSONResponse(
        status_code=spec.status_code,
        content={"detail": {"code": spec.code, "message": spec.message}},
    )
