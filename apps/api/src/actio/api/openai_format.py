"""OpenAI's form, written in one place: what a program reads under `/v1/`, the developer
gateway, as `ui_stream` is the one place for what a browser reads of a turn.

A model, and the list of them, as OpenAI's API lists its own:

    {"object": "list",
     "data": [{"id": "standard", "object": "model", "created": 0, "owned_by": "actio"}]}

An error, with the four members of OpenAI's published `Error`, every one of them there
each time, as its specification requires:

    {"error": {"message": "No model is called that. GET /v1/models lists them.",
               "type": "invalid_request_error", "param": null, "code": "model_not_found"}}

- **a model is named by its name in the models file,** the name a request asks for it
  by, and never by its name on its provider's server (ADR 0013);
- **an error's `type` goes by its status alone** (`error_type`). OpenAI's guide to its
  error codes names `invalid_request_error`, `rate_limit_error` and
  `service_unavailable_error`; `server_error`, for any other 5xx, is this installation's
  own word, which the guide doesn't name;
- **its `code` is what the error's raiser gave,** and where it gave none, `invalid_api_key`
  for a 401, which under `/v1/` is always the key's; otherwise `null`;
- **its `param` is `null`:** no route under `/v1/` takes a body.

The official SDKs choose the class of the exception they raise by the status alone, and
read `message`, `type`, `param` and `code` from the `error` member: OpenAI's Python SDK in
the API's tests, its JavaScript one end to end. Which responses get this form is
`actio.api.problems`'s to say, from the request's path: whoever raises an error raises a
`Problem`, under `/v1/` as anywhere (ADR 0034).
"""

from collections.abc import Iterable, Mapping
from typing import Literal

from fastapi.responses import JSONResponse

from actio.api.described import Described

TYPES = {429: "rate_limit_error", 503: "service_unavailable_error"}
"""An error's `type` by its status, where it is neither of the two below."""

SERVER_ERROR = "server_error"
"""The `type` of any other 5xx: this installation's own word."""

INVALID_REQUEST = "invalid_request_error"
"""The `type` of every other status."""

CODES = {401: "invalid_api_key"}
"""An error's `code` by its status, where its raiser gave none: under `/v1/` a 401 is
always the key's. Any other status has none then."""

CREATED = 0
"""What a model's `created` says: Actio keeps no moment at which a model was made, and a
made-up one would read as a fact."""

OWNED_BY = "actio"


def error_type(status: int) -> str:
    """The `type` of an error of this status."""
    if status in TYPES:
        return TYPES[status]
    return SERVER_ERROR if status >= 500 else INVALID_REQUEST


class OpenAIError(Described):
    """What went wrong, as OpenAI's API says it."""

    message: str
    """What happened, in words a person can act on."""
    type: str
    """What kind of error it is, by its status alone: `rate_limit_error` for a 429,
    `service_unavailable_error` for a 503, `server_error` for any other status from 500
    up, and `invalid_request_error` for every other status."""
    param: str | None
    """The member of the request that is wrong: always `null`, since no route here takes
    a body."""
    code: str | None
    """A word a program can tell the error by, where there is one: `invalid_api_key` for
    any 401, a key missing, not known, or one of two different keys, and
    `model_not_found` for a model nobody offers. Otherwise `null`."""


class OpenAIErrorResponse(Described):
    """An error under `/v1`, in the form OpenAI's API answers one, which its SDKs read.
    It carries the status and the headers of the error, such as `WWW-Authenticate` on
    a 401 and `Allow` on a 405."""

    error: OpenAIError


class OpenAIModel(Described):
    """A model on offer, in the form OpenAI's API lists one."""

    id: str
    """The name to ask for it by, such as `standard`: its name here, which stays the same
    when what runs behind it changes. Never its name on the server that runs it."""
    object: Literal["model"]
    """Always `model`."""
    created: int
    """Always 0: no moment is kept at which a model was made. Where OpenAI's API says
    one, it is a Unix time, in seconds."""
    owned_by: str
    """Always `actio`."""


class OpenAIModelList(Described):
    """Every model on offer, in the form OpenAI's API lists its own."""

    object: Literal["list"]
    """Always `list`."""
    data: list[OpenAIModel]
    """Every model on offer, in the order `GET /api/models` lists them in, whichever plan
    the key's account is on."""


def model(name: str) -> OpenAIModel:
    """The model called `name` in the models file."""
    return OpenAIModel(id=name, object="model", created=CREATED, owned_by=OWNED_BY)


def model_list(names: Iterable[str]) -> OpenAIModelList:
    """The models called `names` in the models file, in that order."""
    return OpenAIModelList(object="list", data=[model(name) for name in names])


def error_response(
    status: int,
    message: str,
    *,
    code: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """An error of this status, in OpenAI's form, with these headers: its `code`, or for
    a status that has one when none is given, that status's (`CODES`)."""
    body = OpenAIErrorResponse(
        error=OpenAIError(
            message=message,
            type=error_type(status),
            param=None,
            code=code if code is not None else CODES.get(status),
        )
    )
    return JSONResponse(body.model_dump(), status_code=status, headers=headers)
