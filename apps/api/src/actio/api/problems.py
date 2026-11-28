"""Errors in one format for each surface: RFC 9457 problem details, as
``application/problem+json``, and under `/v1/` OpenAI's error (`actio.api.openai_format`).

Every error of the API's own routes has the same shape, whoever raised it:

    {"type": "about:blank", "title": "Unauthorized", "status": 401,
     "detail": "This needs a session. Start one with POST /api/session/guest."}

- `title` names the kind of problem, and is the same every time it happens;
- `detail` says what happened this time, in words a person can act on;
- extra members carry what a program needs, such as `errors` for an invalid request.

But under `/v1/`, the developer gateway, where programs that speak OpenAI's API ask:
there the same error is OpenAI's, with the same status and headers, its `detail` as its
`message`, and the `code` its raiser gave (`Problem`). Its extra members have no place
there, and are left out. The form is chosen here, where the response is made, from the
request's path (`on_the_gateway`), never where the error is raised: so code raises a
`Problem` whatever the route, and a route nobody thought of can't answer a program in
the wrong form (ADR 0034).

The API's description of itself (OpenAPI) says the same: :func:`install` gives every
error response the `Problem` schema, or under `/v1/` OpenAI's error, where the framework
would describe a body this API never sends.
"""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ConfigDict
from pydantic.json_schema import models_json_schema
from starlette.exceptions import HTTPException

from actio.api import openai_format
from actio.api.described import Described

MEDIA_TYPE = "application/problem+json"

GATEWAY = "/v1/"
"""Where the developer gateway is: every path that starts with this, and no other, such
as `/v1/models` and not `/v10/models`. Its errors are OpenAI's."""


def on_the_gateway(path: str) -> bool:
    """Whether a request's path is on the developer gateway, whose errors are
    OpenAI's, or else on the API's own surface, whose errors are problem documents."""
    return path.startswith(GATEWAY)


class ProblemDocument(Described):
    """An error, as RFC 9457 problem details. Every error this API answers looks like
    this, but under `/v1/`, where it is OpenAI's (`OpenAIErrorResponse`). Some carry
    more members: `errors` (422: what is wrong with the request, and where),
    `retry_after` (429: seconds to wait), `window`, `resets_at` and `alternatives`
    (429: the usage limit that is reached, when it starts over, and the
    models that cost nothing), `at_once` (429: how many answers the plan allows to be
    written at once), `model` and `model_status` (503: the model that can't answer),
    `model` alone (429: the model asked, when no model that could answer can take the
    message right now), `turn_id` and `turn_status` (409: the turn in the way)."""

    model_config = ConfigDict(title="Problem", extra="allow")

    type: str
    """Always `about:blank`: the status says what kind of problem it is."""
    title: str
    """The status in words, such as `Not Found`."""
    status: int
    detail: str
    """What happened this time, in words a person can act on."""


class Problem(Exception):  # noqa: N818 - "problem" is RFC 9457's own word for it
    """An error to send to the client as it is, in the form of the request's surface.

    `headers` go out with it on either surface. `code` is a word a program can tell the
    error by, such as `model_not_found`, which only OpenAI's error says: a problem
    document never shows it. `extra` members are a problem document's alone: OpenAI's
    error has no place for them, and under `/v1/` they are left out."""

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        headers: dict[str, str] | None = None,
        code: str | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = headers
        self.code = code
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


def error_response(
    request: Request,
    status: int,
    detail: str,
    *,
    headers: dict[str, str] | None = None,
    code: str | None = None,
    **extra: Any,
) -> JSONResponse:
    """The error, in the form of the request's surface: OpenAI's under `/v1/`, without
    the extra members, and a problem document, without the code, everywhere else."""
    if on_the_gateway(request.url.path):
        return openai_format.error_response(status, detail, code=code, headers=headers)
    return problem_response(status, detail, headers=headers, **extra)


INVALID = "The request doesn't have the expected form. `errors` lists what is wrong, and where."


def describe(document: dict[str, Any]) -> None:
    """Make an OpenAPI `document` say what errors really look like, in place.

    - Every 4xx and 5xx response that names no body of its own gets the `Problem` schema,
      or on the gateway's paths OpenAI's error.
    - The framework's description of a 422, a body this API never sends, is replaced. On
      the gateway's paths it is taken out: no route there takes a body or a query, and a
      path's text is never refused, so none answers 422.
    """
    schemas = document.setdefault("components", {}).setdefault("schemas", {})
    schemas["Problem"] = ProblemDocument.model_json_schema()
    content = {MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}}
    refs, defined = models_json_schema(
        [(openai_format.OpenAIErrorResponse, "serialization")],
        ref_template="#/components/schemas/{model}",
    )
    [gateway_schema] = refs.values()
    schemas.update(defined["$defs"])
    gateway_content = {"application/json": {"schema": gateway_schema}}
    for path, operations in document.get("paths", {}).items():
        on_gateway = on_the_gateway(path)
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses", {})
            for status, response in list(responses.items()):
                if status == "422" and "HTTPValidationError" in str(response.get("content")):
                    if on_gateway:
                        del responses[status]
                    else:
                        response.update(description=INVALID, content=content)
                elif status[0] in "45" and "content" not in response:
                    response["content"] = gateway_content if on_gateway else content
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)


def install(app: FastAPI) -> None:
    """Make every error `app` returns a problem document, or OpenAI's error on the
    gateway's paths, and its description say so."""
    describe_routes = app.openapi

    def openapi() -> dict[str, Any]:
        described = app.openapi_schema is not None
        document = describe_routes()  # builds the description once, then keeps it
        if not described:
            describe(document)
        return document

    app.openapi = openapi  # type: ignore[method-assign]

    @app.exception_handler(Problem)
    async def raised_here(request: Request, problem: Problem) -> JSONResponse:
        return error_response(
            request,
            problem.status,
            problem.detail,
            headers=problem.headers,
            code=problem.code,
            **problem.extra,
        )

    @app.exception_handler(HTTPException)
    async def raised_by_the_framework(request: Request, error: HTTPException) -> JSONResponse:
        # Such as a 404 for a path no route matches, or a 405 for its method.
        return error_response(
            request, error.status_code, str(error.detail), headers=dict(error.headers or {})
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, error: RequestValidationError) -> JSONResponse:
        # Where it is wrong and how, never what was sent: deep inside a body that can
        # still hold a character no response can carry (`actio.api.body`). A problem
        # document on every path: no route of the gateway's can be refused so, since
        # none takes a body or a query, and a path's text is never refused (ADR 0034).
        errors = [
            {"where": ".".join(str(part) for part in issue["loc"]), "problem": issue["msg"]}
            for issue in error.errors()
        ]
        return problem_response(422, "The request doesn't have the expected form.", errors=errors)
