"""Builds the FastAPI application. ``uvicorn --factory`` calls :func:`create_app`."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from actio import __version__
from actio.api import catalog as catalog_routes
from actio.api import conversations, health, identity, problems, turns, usage
from actio.api.same_origin import SameOriginOnly
from actio.catalog import Catalog, open_catalog
from actio.chat.events import TurnEvents
from actio.chat.readers import TurnReaders
from actio.chat.running import TurnRunner
from actio.core.resources import open_resources
from actio.core.settings import Settings
from actio.core.signals import on_stop_signal
from actio.identity.passwords import Passwords
from actio.usage.limits import UsageLimits
from actio.usage.plans import Plans
from actio.usage.plans import load as load_plans
from actio_quota.engine import ValkeyQuota

DESCRIPTION = """\
Actio's HTTP API, which its web app is built on.

- **Sessions.** Everything but the health checks needs a session, a guest's included.
  It lives in a cookie: start one with `POST /api/session/guest`. Without one the
  answer is 401.
- **Errors.** Every error is an RFC 9457 problem document (`application/problem+json`):
  see the `Problem` schema. A request that changes something, sent by a page on another
  site, is answered 403.
- **Answers from a model** are streamed: see `POST /api/conversations/{id}/turns`. An
  answer is written whether or not anyone reads along. It can be joined again, and
  stopped, by the turn's id.
- **Usage is limited** per session and per week. `GET /api/usage` says how much is
  left, and a question that no longer fits is answered 429, with when it will. A plan
  may also limit how many answers are written for one account at once, and an account
  reads along with only so many at once: past either, the answer is 429 too.
"""


def create_app(
    settings: Settings | None = None,
    *,
    passwords: Passwords | None = None,
    catalog: Catalog | None = None,
    plans: Plans | None = None,
) -> FastAPI:
    """The application. Without `settings`, they are read from the environment.

    Tests replace three parts: `passwords`, with a cheap hasher, `catalog`, with models
    served by a fake, and `plans`, with limits a test can reach. Without them, the
    models file and the plans file are read when the app starts, and a mistake in
    either stops the app from starting.
    """
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            resources = await stack.enter_async_context(open_resources(settings))
            offered = catalog
            if offered is None:
                offered = await stack.enter_async_context(open_catalog(settings))
            # Every model on offer must have a price, wherever the plans come from.
            names = [model.name for model in offered]
            if plans is None:
                priced = load_plans(settings.plans_file, models=names)
            else:
                priced = plans.require_prices_for(names)
            app.state.resources = resources
            app.state.catalog = offered
            app.state.usage = UsageLimits(
                # Beside the rate limits' keys, under the same prefix.
                ValkeyQuota(resources.valkey, prefix=f"{settings.valkey_prefix}:q"),
                priced,
            )
            app.state.turn_events = TurnEvents(
                resources.valkey, prefix=f"{settings.valkey_prefix}:turn"
            )
            app.state.turn_runner = runner = TurnRunner()
            app.state.turn_readers = TurnReaders()
            # The web server tells the application to shut down only after the
            # requests it was answering have ended. The answers being written have to
            # hear it sooner, so that they end while their readers are still there.
            loop = asyncio.get_running_loop()

            def told_to_stop() -> None:
                # Inside a signal handler: only a note for the loop is left here.
                loop.call_soon_threadsafe(runner.stopping)

            stack.callback(on_stop_signal(told_to_stop))
            # Registered last, so it runs first on the way out: whatever is still being
            # written is ended and recorded while the database is still open.
            stack.push_async_callback(runner.drain)
            yield

    app = FastAPI(
        title="Actio",
        version=__version__,
        description=DESCRIPTION,
        # An operation is named after its route's function, such as `start_turn`:
        # the names are part of the recorded description (apps/api/openapi.json).
        generate_unique_id_function=lambda route: route.name,
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
    app.include_router(usage.router)
    return app
