"""Builds the FastAPI application. ``uvicorn --factory`` calls :func:`create_app`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from actio import __version__
from actio.api import health, identity, problems
from actio.api.same_origin import SameOriginOnly
from actio.core.resources import open_resources
from actio.core.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """The application. Without `settings`, they are read from the environment."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_resources(settings) as resources:
            app.state.resources = resources
            yield

    app = FastAPI(
        title="Actio",
        version=__version__,
        # The reverse proxy sends /api/* to this service and everything else to the
        # web app, so the API docs live under /api too.
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    problems.install(app)
    app.add_middleware(SameOriginOnly, origin=settings.public_url)
    app.include_router(health.router)
    app.include_router(identity.router)
    return app
