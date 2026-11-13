"""Builds the FastAPI application. ``uvicorn --factory`` calls :func:`create_app`."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from actio import __version__
from actio.api import catalog as catalog_routes
from actio.api import conversations, health, identity, problems, turns
from actio.api.same_origin import SameOriginOnly
from actio.catalog import Catalog, open_catalog
from actio.core.resources import open_resources
from actio.core.settings import Settings
from actio.identity.passwords import Passwords


def create_app(
    settings: Settings | None = None,
    *,
    passwords: Passwords | None = None,
    catalog: Catalog | None = None,
) -> FastAPI:
    """The application. Without `settings`, they are read from the environment.

    Tests replace two parts: `passwords`, with a cheap hasher, and `catalog`, with models
    served by a fake. Without `catalog`, the models file is read when the app starts,
    and a mistake in it stops the app from starting.
    """
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            app.state.resources = await stack.enter_async_context(open_resources(settings))
            app.state.catalog = catalog or await stack.enter_async_context(open_catalog(settings))
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
    app.state.passwords = passwords or Passwords()
    problems.install(app)
    app.add_middleware(SameOriginOnly, origin=settings.public_url)
    app.include_router(health.router)
    app.include_router(identity.router)
    app.include_router(conversations.router)
    app.include_router(turns.router)
    app.include_router(catalog_routes.router)
    return app
