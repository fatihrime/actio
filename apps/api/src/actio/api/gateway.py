"""The developer gateway: routes under `/v1`, in the form OpenAI's API has, for a program
that carries an API key (ADR 0034).

- `GET /v1/models`: every model on offer;
- `GET /v1/models/{model}`: one of them, by all that follows in the path: an id with a
  slash in it is one id, and a model nobody offers.

Each takes a key, any key that works, and no session: a session's cookie is not looked
at (`CurrentKey`). A model is named by its name in the models file, and the list is the
catalog's alone: no model's server is asked, and nothing is waited for. Whether a model
can answer right now is `GET /api/models`'s to say. Every error here is OpenAI's, made
from the `Problem` raised, wherever it was raised (`actio.api.problems`). Nothing is
counted against a rate limit: a route that refuses whoever has no key that works is not
one that anyone can call (ADR 0033).
"""

from typing import Any

from fastapi import APIRouter

from actio.api import openai_format
from actio.api.dependencies import NO_KEY, CurrentKey, Models
from actio.api.openai_format import OpenAIModel, OpenAIModelList
from actio.api.problems import Problem

router = APIRouter(tags=["gateway"])

NO_SUCH_MODEL = "No model is called that. GET /v1/models lists them."

NOT_OFFERED: dict[int | str, dict[str, Any]] = {
    404: {"description": "No model is called that. `code` says `model_not_found`."}
}


@router.get("/v1/models", responses=NO_KEY)
async def list_gateway_models(_: CurrentKey, catalog: Models) -> OpenAIModelList:
    """Every model on offer, in the form OpenAI's API lists its own, by the names to ask
    for them by, in the order `GET /api/models` lists them in. Whether each can answer
    right now isn't said here: `GET /api/models` says it."""
    return openai_format.model_list(offered.name for offered in catalog)


# The rest of the path, slashes and all: an SDK sends `openai/gpt-oss-20b` as one id, and
# a parameter that ended at a slash left it to the framework's own 404, which names no
# model and has no `code`.
@router.get("/v1/models/{model:path}", responses={**NO_KEY, **NOT_OFFERED})
async def get_gateway_model(model: str, _: CurrentKey, catalog: Models) -> OpenAIModel:
    """One model on offer, by the name to ask for it by, in the form OpenAI's API gives
    one. 404 for a model nobody offers."""
    # By its name as it is: `catalog.get` takes no name at all for the default model's,
    # and nothing comes after `/v1/models/` in a path that ends there.
    if model not in {offered.name for offered in catalog}:
        # Not repeated: a path's text has no bound.
        raise Problem(404, NO_SUCH_MODEL, code="model_not_found")
    return openai_format.model(model)
