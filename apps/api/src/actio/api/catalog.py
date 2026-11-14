"""The route that lists the models on offer.

`GET /api/models` says what can be chosen, which one is the default, and whether each
is ready to answer right now. Which models there are comes from the models file
(`actio.catalog`); whether one is ready is asked from its provider.

This module isn't called `models`: in this application that name is kept for modules
that define database tables.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from actio.api.dependencies import NO_SESSION, CurrentPrincipal, Models, Now

router = APIRouter(tags=["models"], responses=NO_SESSION)


class ModelOut(BaseModel):
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


class ModelList(BaseModel):
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
