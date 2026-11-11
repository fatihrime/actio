"""Errors in one format: RFC 9457 problem details, as ``application/problem+json``.

Every error the API returns has the same shape, whoever raised it:

    {"type": "about:blank", "title": "Unauthorized", "status": 401,
     "detail": "This needs a session. Start one with POST /api/session/guest."}

- `title` names the kind of problem, and is the same every time it happens;
- `detail` says what happened this time, in words a person can act on;
- extra members carry what a program needs, such as `errors` for an invalid request.
"""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

MEDIA_TYPE = "application/problem+json"


class Problem(Exception):  # noqa: N818 - "problem" is RFC 9457's own word for it
    """An error to send to the client as it is."""

    def __init__(
        self, status: int, detail: str, *, headers: dict[str, str] | None = None, **extra: Any
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = headers
        self.extra = extra


def problem_response(
    status: int, detail: str, *, headers: dict[str, str] | None = None, **extra: Any
) -> JSONResponse:
    body = {
        "type": "about:blank",  # RFC 9457: no more specific type than the status itself
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type=MEDIA_TYPE, headers=headers)


def install(app: FastAPI) -> None:
    """Make every error `app` returns a problem document."""

    @app.exception_handler(Problem)
    async def raised_here(_request: Request, problem: Problem) -> JSONResponse:
        return problem_response(
            problem.status, problem.detail, headers=problem.headers, **problem.extra
        )

    @app.exception_handler(HTTPException)
    async def raised_by_the_framework(_request: Request, error: HTTPException) -> JSONResponse:
        # Such as a 404 for a path no route matches, or a 405 for its method.
        return problem_response(
            error.status_code, str(error.detail), headers=dict(error.headers or {})
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, error: RequestValidationError) -> JSONResponse:
        errors = [
            {"where": ".".join(str(part) for part in issue["loc"]), "problem": issue["msg"]}
            for issue in error.errors()
        ]
        return problem_response(422, "The request doesn't have the expected form.", errors=errors)
