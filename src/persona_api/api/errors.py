"""Translating exceptions into RFC 9457 problem responses.

The only place in the service that maps a failure to a status code, which is what keeps
the handlers thin: they raise domain errors and let this decide what that means over
HTTP.

Three decisions matter here, and the first is the one this whole service is shaped
around.

**Cross-account access is 404, never 403.** A 403 confirms the resource exists, which
would tell one person that another person has a persona for a profile. 404 is the same
answer they would get for a profile nobody has ever used. There is no 403 in the table
below at all, because there is no administrative surface for one to protect -- see
``docs/adr/0003-no-administrative-surface.md``.

**keyring being unreachable is 503, not 401.** Nothing is wrong with the caller's token,
and a 401 would send them to re-authenticate against a service that is not answering.

**An unexpected exception's text never reaches the caller.** It can carry a path, a
hostname, or a fragment of the very credential this service just refused to store. The
caller gets a request id to quote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from persona_api.api.schemas.common import PROBLEM_CONTENT_TYPE, FieldError, Problem
from persona_api.core.context import get_request_id
from persona_api.core.logging import get_logger
from persona_api.domain.errors import (
    AuthenticationError,
    CredentialRefusedError,
    FieldNotFoundError,
    InvalidCursorError,
    InvalidFieldKeyError,
    InvalidFieldValueError,
    InvalidNoteError,
    InvalidProfileError,
    InvalidSearchError,
    KeyringUnreachableError,
    LimitExceededError,
    NoteNotFoundError,
    PersonaExistsError,
    PersonaNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

logger = get_logger(__name__)

PROBLEM_BASE_URI = "https://persona.invalid/problems"

_STATUS_TITLES = {
    status.HTTP_400_BAD_REQUEST: "Bad request",
    status.HTTP_401_UNAUTHORIZED: "Unauthorized",
    status.HTTP_404_NOT_FOUND: "Not found",
    status.HTTP_409_CONFLICT: "Conflict",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "Validation failed",
    status.HTTP_429_TOO_MANY_REQUESTS: "Too many requests",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "Internal server error",
    status.HTTP_503_SERVICE_UNAVAILABLE: "Service unavailable",
}

# Domain errors that map cleanly onto a status code. Anything absent is a bug and
# becomes a 500 with its detail withheld.
_DOMAIN_STATUS: dict[type[Exception], int] = {
    AuthenticationError: status.HTTP_401_UNAUTHORIZED,
    # Not 401. Nothing is wrong with the caller's token; keyring is not answering, and
    # sending them to re-authenticate over it would be advice they cannot act on.
    KeyringUnreachableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    InvalidProfileError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidFieldKeyError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidFieldValueError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidNoteError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidSearchError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidCursorError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    # 422 with a message naming keyring, rather than 400: the request was well-formed
    # and the value is the problem, and the caller needs to be told where it goes.
    CredentialRefusedError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PersonaExistsError: status.HTTP_409_CONFLICT,
    # Not 403. A 403 would confirm the row exists and belongs to somebody else, which
    # is precisely the fact that must not leak across accounts.
    PersonaNotFoundError: status.HTTP_404_NOT_FOUND,
    FieldNotFoundError: status.HTTP_404_NOT_FOUND,
    NoteNotFoundError: status.HTTP_404_NOT_FOUND,
    LimitExceededError: status.HTTP_429_TOO_MANY_REQUESTS,
}


# PLR0913: five keyword-only fields, because RFC 9457 has them. Grouping them into an
# object would add a type whose only job is to be unpacked one line later.
def problem_response(
    *,
    status_code: int,
    detail: str,
    problem_type: str | None = None,
    title: str | None = None,
    errors: list[FieldError] | None = None,
) -> JSONResponse:
    """Build a problem+json response carrying the current request id."""
    slug = problem_type or _slug_for(status_code)
    problem = Problem(
        type=f"{PROBLEM_BASE_URI}/{slug}",
        title=title or _STATUS_TITLES.get(status_code, "Error"),
        status=status_code,
        detail=detail,
        request_id=get_request_id(),
        errors=errors,
    )
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_CONTENT_TYPE,
    )


def _slug_for(status_code: int) -> str:
    return _STATUS_TITLES.get(status_code, "error").lower().replace(" ", "-")


def register_exception_handlers(app: FastAPI) -> None:
    """Install every handler the app needs. Called once, by the app factory."""

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Reshape FastAPI's validation errors into the one error format this API uses.

        Only the location and the message are copied. FastAPI's raw errors include the
        offending *input*, and on this service that input is routinely the thing
        somebody asked to have remembered -- or, on the path that matters most, the
        credential the domain layer just refused.
        """
        return problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the request failed validation",
            problem_type="validation-failed",
            errors=[
                FieldError(
                    location=".".join(str(part) for part in error["loc"]),
                    message=error["msg"],
                )
                for error in exc.errors()
            ],
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(status_code=exc.status_code, detail=str(exc.detail))

    for error_type, status_code in _DOMAIN_STATUS.items():
        app.add_exception_handler(error_type, _domain_handler(status_code))


def unhandled_problem_response(exc: BaseException) -> JSONResponse:
    """Render an unexpected exception as a 500.

    The exception's own message is withheld: it can carry filesystem paths, internal
    hostnames, or a fragment of a credential this service refused to store. The request
    id ties the response to the log record that does have the detail -- which is why
    this is invoked from inside
    :class:`~persona_api.api.middleware.RequestContextMiddleware`, while the id is still
    bound, rather than from Starlette's outermost error middleware, where the binding
    has already unwound and the response would carry no id at all.
    """
    logger.exception("unhandled_exception", error_type=type(exc).__name__)
    return problem_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="an unexpected error occurred; quote the request id when reporting it",
    )


def _domain_handler(
    status_code: int,
) -> Callable[[Request, Exception], Coroutine[Any, Any, JSONResponse]]:
    """Build a handler that renders a domain error at ``status_code``."""

    async def handler(_request: Request, exc: Exception) -> JSONResponse:
        return problem_response(status_code=status_code, detail=str(exc))

    return handler
