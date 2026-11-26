"""Builds the FastAPI application. ``uvicorn --factory`` calls :func:`create_app`, or
:func:`create_app_with_worker` for the API with the worker inside it."""

import asyncio
import multiprocessing
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from actio import __version__, worker
from actio.api import catalog as catalog_routes
from actio.api import conversations, health, identity, problems, turns, usage
from actio.api.same_origin import SameOriginOnly
from actio.catalog import Catalog, open_catalog
from actio.chat.events import TurnEvents
from actio.chat.readers import TurnReaders
from actio.chat.running import TurnRunner
from actio.core import alive
from actio.core.resources import Resources, open_resources
from actio.core.settings import Settings
from actio.core.signals import on_stop_signal
from actio.identity.passwords import Passwords
from actio.usage.limits import UsageLimits
from actio.usage.plans import Plans
from actio.usage.plans import load as load_plans

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
    with_worker: bool = False,
) -> FastAPI:
    """The application. Without `settings`, they are read from the environment.

    Tests replace three parts: `passwords`, with a cheap hasher, `catalog`, with models
    served by a fake, and `plans`, with limits a test can reach. Without them, the
    models file and the plans file are read when the app starts, and a mistake in
    either stops the app from starting.

    `with_worker` runs the worker inside the app, on the app's resources and its
    catalog, for as long as the app runs (`actio.worker.run_inside`), and `/api/readyz`
    says whether it works.
    """
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            resources = await stack.enter_async_context(open_resources(settings))
            offered = catalog
            if offered is None:
                offered = await stack.enter_async_context(open_catalog(settings))
            # No load of a model the app started, and no ask about a model's status, is
            # left pending once it has shut down, whoever opened the catalog: one the app
            # was handed, as tests hand it one, is closed by whoever opened it, and maybe
            # only after the app.
            stack.push_async_callback(offered.stop_tasks)
            # Every model on offer must have a price, wherever the plans come from.
            names = [model.name for model in offered]
            if plans is None:
                priced = load_plans(settings.plans_file, models=names)
            else:
                priced = plans.require_prices_for(names)
            app.state.resources = resources
            app.state.catalog = offered
            app.state.usage = UsageLimits(resources.quota, priced)
            app.state.turn_events = TurnEvents(
                resources.valkey, prefix=f"{settings.valkey_prefix}:turn"
            )
            app.state.turn_runner = runner = TurnRunner()
            app.state.turn_readers = TurnReaders()
            # On the app's own catalog: a title waits at the same gate as the answers.
            inside = _WorkerInside(settings, resources, offered) if with_worker else None
            # What the readiness probe asks whether the worker works: nothing, in an
            # app that runs none.
            app.state.worker_works = inside.works if inside is not None else None
            if inside is not None:
                # It ends before the resources are closed: the job in hand uses them.
                stack.push_async_callback(inside.ended)
            # The web server tells the application to shut down only after the
            # requests it was answering have ended. The answers being written have to
            # hear it sooner, so that they end while their readers are still there.
            # So does the worker: the job in hand gets its grace while the web server
            # waits for its requests, not after.
            loop = asyncio.get_running_loop()

            def told_to_stop() -> None:
                # Inside a signal handler: only notes for the loop are left here.
                loop.call_soon_threadsafe(runner.stopping)
                if inside is not None:
                    loop.call_soon_threadsafe(inside.stop)

            # uvicorn takes a SIGINT that follows another signal for "quit at once", and
            # then never shuts the application down: nothing below would run. A server
            # that uvicorn started from another process, as its reloader does, is told
            # to stop several times over: by that process, by the terminal, and by
            # whatever runs the two, as concurrently does in `make dev`. There the first
            # signal counts, and later ones change nothing.
            started_by_another = multiprocessing.parent_process() is not None
            stack.callback(on_stop_signal(told_to_stop, once=started_by_another))
            # Registered after the rest, so it runs before it on the way out: whatever
            # is still being written is ended and recorded while the database is still
            # open.
            stack.push_async_callback(runner.drain)
            if inside is not None:
                # Before that, the worker is told to stop, if nothing told it yet: its
                # grace and the answers' run together.
                stack.callback(inside.stop)
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


def create_app_with_worker() -> FastAPI:
    """The application with the worker inside it: `actio --role all`. uvicorn calls a
    factory without arguments, by its name, so this one stands apart."""
    return create_app(with_worker=True)


class _WorkerInside:
    """The worker, run in a task of the app's process, and told to stop once.

    It is told by cancelling that task. Procrastinate's worker takes the first
    cancellation as "take no new job, and give the one in hand its grace"
    (`jobs.SHUTDOWN_GRACE_S`). A second would cut that job off, and leave the worker on
    the queue's list of workers. So whoever tells it again changes nothing.

    Each time the worker says that it works, its beat is noted by the event loop's
    clock, which doesn't jump, for `works` to look at. Each time it ends with an error
    or can't work, the beat is forgotten.
    """

    def __init__(self, settings: Settings, resources: Resources, catalog: Catalog) -> None:
        self._clock = asyncio.get_running_loop().time
        self._last_beat = alive.LastBeat()
        self._task = asyncio.create_task(
            worker.run_inside(
                settings,
                resources,
                catalog,
                on_beat=self._beat,
                on_stopped=self._last_beat.forget,
            ),
            name="worker",
        )
        self._told = False

    def _beat(self) -> None:
        self._last_beat.beat(self._clock())

    def works(self) -> bool:
        """Whether the worker works now: it said so within `alive.STALE_AFTER_S`, as the
        worker's file has to be touched for its container to be healthy, and hasn't
        ended with an error or found that it can't work since."""
        return self._last_beat.fresh(now=self._clock())

    def stop(self) -> None:
        """Tell the worker to stop, the first time only."""
        if not self._told:
            self._told = True
            self._task.cancel()

    async def ended(self) -> None:
        """Tell the worker to stop, if nothing told it yet, and wait until it has.

        Without a time limit: told to stop while its database is away, Procrastinate
        waits 30 seconds for a connection, as the worker's own process does, and
        whatever stops the process cuts that short."""
        self.stop()
        await asyncio.wait({self._task})
