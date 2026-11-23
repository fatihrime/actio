"""The routes of the models on offer.

`GET /api/models` says what can be chosen, which one is the default, and whether each
is ready to answer right now. Which models there are comes from the models file
(`actio.catalog`); whether one is ready is asked from its provider.

`POST /api/models/{model_id}/warm-up` has a model's server load it into memory before
the first question to it, so that the question doesn't wait for the load (ADR 0026).

This module isn't called `models`: in this application that name is kept for modules
that define database tables.
"""

from typing import Literal

from fastapi import APIRouter, Response

from actio.api.dependencies import NO_SESSION, CurrentPrincipal, Models, Now
from actio.api.described import Described
from actio.api.problems import Problem
from actio.api.rate_limits import Limits
from actio.catalog import UnknownModelError

router = APIRouter(tags=["models"], responses=NO_SESSION)

NOT_READY = {
    "missing": "This model isn't on its server yet.",
    "unreachable": "This model's server can't be reached.",
}


def not_ready(model: str, state: str) -> Problem:
    """The 503 for a model that can't answer right now, by its name and its status:
    `missing` or `unreachable`. The words are for a person, and `model_status` is for
    a page that says what to do about it."""
    return Problem(503, NOT_READY[state], model=model, model_status=state)


class ModelOut(Described):
    id: str
    """The name to ask for it by, such as `standard`."""
    title: str
    provider: Literal["ollama", "openai-compatible"]
    """What kind of server runs it."""
    model: str
    """Its name on that server: for Ollama, what `ollama pull` takes."""
    context_window: int
    max_output_tokens: int
    status: Literal["ready", "missing", "unreachable"]
    """`ready`: its server has it. `missing`: the server answers, but hasn't got this
    model. `unreachable`: the server doesn't answer."""
    thinking: bool
    """Whether it can reason before it answers. False while its server can't be asked."""


class ModelList(Described):
    default: str
    """The `id` of the model that is used when a request names none."""
    models: list[ModelOut]


@router.get("/api/models")
async def list_models(_: CurrentPrincipal, catalog: Models, now: Now) -> ModelList:
    """The models you can choose from, and whether each can answer right now.

    Each provider is asked at most every 15 seconds, so this can be fetched often.
    """
    statuses = await catalog.statuses(now=now)
    return ModelList(
        default=catalog.default,
        models=[
            ModelOut(
                id=offered.name,
                title=offered.title,
                provider=offered.kind,
                model=offered.model,
                context_window=offered.context_window,
                max_output_tokens=offered.max_output_tokens,
                status=statuses[offered.name].state,
                thinking=statuses[offered.name].thinking,
            )
            for offered in catalog
        ],
    )


@router.post(
    "/api/models/{model_id}/warm-up",
    status_code=202,
    response_class=Response,
    responses={
        202: {
            "description": "The model is being loaded: a load was started, or one is "
            "under way already."
        },
        204: {
            "description": "Nothing was started: the model's kind of server can't be "
            "asked to load a model ahead, or its server is writing as many answers as "
            "it takes."
        },
        404: {"description": "No model is called that."},
        429: {
            "description": "Too many requests to load a model in a short time. "
            "`Retry-After` says how long to wait."
        },
        503: {
            "description": "The model can't answer right now: `model_status` says why. "
            "Or requests can't be counted right now."
        },
    },
)
async def warm_up(
    model_id: str, principal: CurrentPrincipal, catalog: Models, limits: Limits, now: Now
) -> Response:
    """Have a model loaded into its server's memory now, so that the first question to
    it doesn't wait for the load. A local server keeps a model loaded for a few minutes
    after it was last asked anything; this moves that time too.

    Answers at once, without waiting for the load, and with no body. It costs no
    credits. What becomes of the load isn't said: a question asked meanwhile waits for
    it at the server, as it would have waited for its own."""
    try:
        offered = catalog.get(model_id)
    except UnknownModelError:
        raise Problem(404, "No model is called that. GET /api/models lists them.") from None
    await limits.warm_up(principal.user_id)
    status = await catalog.status(offered.name, now=now)
    if status.state != "ready":
        raise not_ready(offered.name, status.state)
    return Response(status_code=202 if catalog.load_ahead(offered.name) else 204)
