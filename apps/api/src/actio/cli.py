"""Command-line entry point: ``python -m actio --role api``.

Actio is one application that runs as different processes, chosen with ``--role``:

- ``api`` serves HTTP;
- ``migrate`` brings the database schema up to date, then exits. It runs before the API
  starts, as its own step, so the API never changes the schema it is serving from;
- ``worker`` runs the background jobs, until it is told to stop;
- ``all`` serves HTTP as ``api`` does, with the worker inside the same process: for the
  smallest installations, and for ``make dev``. It doesn't migrate either.

Beside the roles, ``plan`` does a piece of an installer's work, and exits: ``actio plan
EMAIL`` says which plan of the plans file an account is on, ``actio plan EMAIL NAME``
puts it on the plan ``NAME`` of its own, and ``actio plan EMAIL --default`` takes its own
plan away, so that it has the one the file names for accounts again. It reads the plans
file alone, and connects to the database alone.
"""

import argparse
import asyncio
import copy
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import uvicorn
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from uvicorn.config import LOGGING_CONFIG

from actio import worker
from actio.catalog import CatalogError, read_file
from actio.core import jobs, schema
from actio.core.database import create_engine, create_session_factory
from actio.core.settings import Settings
from actio.identity import accounts
from actio.usage.plans import Plan, Plans, PlansError
from actio.usage.plans import load as load_plans
from actio.usage.plans import read_file as read_plans

ROLES = ("api", "migrate", "worker", "all")

PLAN_USAGE = "actio plan EMAIL [NAME | --default]"

WAITS_FOR_REQUESTS_S = 25
"""How long the web server waits, when it is told to stop, for the requests it is
answering, before it cuts them off.

The ones that last are the readers of answers being written. Those answers end by
themselves: they are halted `GRACE` after the process was told to stop, and recorded
within `LAST_WORDS` (`actio.chat.running`), and their readers with them. So this wait
has to be the longer one, or a reader is cut off before being told how the answer
ended. And whatever stops the process has to wait longer still: `stop_grace_period` in
compose.yaml. Tests hold the three together."""

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
"""How the worker writes a record in its log: one line, with the time, the level, the
logger and the message."""


def main(argv: Sequence[str] | None = None) -> None:
    given = sys.argv[1:] if argv is None else argv
    if given and given[0] == "plan":
        _plan(given[1:])
        return
    parser = argparse.ArgumentParser(
        prog="actio",
        description="Run an Actio process.",
        epilog=f"{PLAN_USAGE} says which plan an account is on, or puts it on another.",
    )
    parser.add_argument(
        "--role", choices=ROLES, default="api", help="process to run (default: %(default)s)"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help=(
            "restart the API, and the worker inside it in the all role, when the code"
            " changes (development only)"
        ),
    )
    args = parser.parse_args(argv)
    if args.reload and args.role not in ("api", "all"):
        parser.error(f"--reload restarts the API's server; the {args.role} role has none")

    settings = Settings()
    if args.role == "migrate":
        revision = schema.upgrade(settings.database_url.get_secret_value())
        sys.stdout.write(f"The database schema is at migration {revision}.\n")
        return
    if args.role == "worker":
        # No web server sets the log up here. One line a record, on stderr.
        logging.basicConfig(level=settings.log_level.upper(), format=LOG_FORMAT)
        # Reading which migration the database is at makes Alembic say, at the info
        # level, how it would run migrations. The worker runs none.
        logging.getLogger("alembic").setLevel(logging.WARNING)
        # And the HTTP client writes a line for each request to a model's server: two
        # for every title. The `all` role leaves them out too.
        logging.getLogger("httpx2").setLevel(logging.WARNING)
        try:
            # The models file, as for the API: a mistake in it is one line, before the
            # worker starts. One that only shows when the models are built, such as a
            # key's variable that isn't set, comes out of the run. No plans file: no job
            # costs anything.
            read_file(settings.models_file)
            asyncio.run(worker.run(settings))
        except CatalogError as error:
            parser.exit(2, f"actio: {error}\n")
        except (jobs.DatabaseUnreachableError, worker.NotMigratedError) as error:
            parser.exit(1, f"actio: {error}\n")
        return

    try:
        # The app reads the two files again when it starts. Reading them here first
        # turns a mistake in one into one clear line, instead of a failed startup's trace.
        offered = read_file(settings.models_file).models
        load_plans(settings.plans_file, models=offered)
    except (CatalogError, PlansError) as error:
        parser.exit(2, f"actio: {error}\n")

    app = "actio.app:create_app"
    logs: dict[str, Any] = {}  # uvicorn's own
    if args.role == "all":
        app = "actio.app:create_app_with_worker"
        logs = {"log_config": _log_config(settings)}
    uvicorn.run(
        app,
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        server_header=False,  # don't advertise the server software
        forwarded_allow_ips=settings.forwarded_allow_ips,
        timeout_graceful_shutdown=WAITS_FOR_REQUESTS_S,
        reload=args.reload,
        # Watch only this package, not the whole repository.
        reload_dirs=[str(Path(__file__).parent)] if args.reload else None,
        **logs,
    )


class PlanError(Exception):
    """What keeps `actio plan` from doing what it was asked, in one line: no account has
    the address, or the database can't be reached or isn't at this code's migration."""


def _plan(argv: Sequence[str]) -> None:
    """`actio plan`: say which plan an account is on, or put it on another. Success is
    one line on stdout; a failure is one line on stderr, and status 1 for the account or
    the database, 2 for the plans file or a plan it doesn't have."""
    parser = argparse.ArgumentParser(
        prog="actio",
        usage=PLAN_USAGE,
        description=(
            "Say which plan of the plans file an account is on, or put it on another. A"
            " change holds from the account's next request."
        ),
    )
    parser.add_argument("email", metavar="EMAIL", help="the account's email address")
    which = parser.add_mutually_exclusive_group()
    which.add_argument(
        "name", metavar="NAME", nargs="?", help="put the account on the plan of this name"
    )
    which.add_argument(
        "--default",
        action="store_true",
        help="take the account's own plan away: it has the one the file names for accounts",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    try:
        # The plans file alone: no model is asked anything here.
        plans = Plans(read_plans(settings.plans_file))
    except PlansError as error:
        parser.exit(2, f"actio: {error}\n")
    if args.name is not None and plans.named(args.name) is None:
        names = [f'"{name}"' for name in plans.names()]
        listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
        parser.exit(
            2, f'actio: {settings.plans_file} has no plan "{args.name}": its plans are {listed}\n'
        )
    try:
        said = asyncio.run(
            plan(
                settings.database_url.get_secret_value(),
                plans,
                args.email,
                to=args.name,
                change=args.name is not None or args.default,
            )
        )
    except PlanError as error:
        parser.exit(1, f"actio: {error}\n")
    sys.stdout.write(f"{said}\n")


async def plan(database_url: str, plans: Plans, email: str, *, to: str | None, change: bool) -> str:
    """Say which plan the account with the address `email` is on. With `change`, put it on
    the plan `to` of `plans`, of its own, or with `None` take its own plan away, and
    commit that. Returns the line to say. Raises `PlanError`.

    The database's migration is read first: at another than this code's, the account's
    plan may have no column to be read from or written to."""
    address = accounts.normalize_email(email)
    engine = create_engine(database_url)
    try:
        async with create_session_factory(engine)() as db:
            await _at_this_codes_migration(db)
            if not change:
                own = await accounts.own_plan(db, email=email)
                said = f"{address} is on the plan {_on(plans, own)}"
                if own is not None and plans.named(own) is None:
                    said += f": meanwhile it has {_on(plans, None)}"
                return f"{said}."
            before = await accounts.put_on_plan(db, email=email, plan=to)
            if before == to:
                return f"{address} is already on the plan {_on(plans, to)}: nothing was changed."
            await db.commit()
            return (
                f"{address} is now on the plan {_on(plans, to)}."
                f" Before, it was on {_on(plans, before)}."
            )
    except accounts.NoSuchAccountError:
        raise PlanError(f"no account has the address {address}") from None
    finally:
        await engine.dispose()


async def _at_this_codes_migration(db: AsyncSession) -> None:
    """Raise `PlanError` unless the database can be reached, and is at the migration this
    code expects."""
    try:
        revision = await schema.current(await db.connection())
    except (SQLAlchemyError, OSError) as error:
        # The driver's own words, in one line: SQLAlchemy adds a second one about itself.
        said = str(getattr(error, "orig", None) or error).splitlines()[0]
        raise PlanError(f"couldn't connect to the database: {said}") from error
    mismatch = schema.mismatch(revision, schema.head())
    if mismatch is not None:
        raise PlanError(mismatch)


def _on(plans: Plans, own: str | None) -> str:
    """The plan an account that is on `own` of its own has, or on none of its own, in
    words: its name, its title, and whether it is the account's own."""
    if own is None:
        accounts_plan = plans.plan_for(is_guest=False, own_plan=None)
        return f"{_named(accounts_plan)}, the one the plans file names for accounts"
    found = plans.named(own)
    if found is None:
        return f'"{own}", its own, which the plans file doesn\'t have'
    return f"{_named(found)}, its own"


def _named(found: Plan) -> str:
    return f'"{found.name}" ({found.title})'


def _log_config(settings: Settings) -> dict[str, Any]:
    """The log of the `all` role: uvicorn's own, and the worker's beside it.

    uvicorn's sets up only its own loggers, so everyone else's records below a warning
    would be lost, the worker's among them, and the rest written bare. Here Actio's and
    the queue's are written from the settings' level, and everyone else's from warnings
    up, each in one line as the worker role writes it. Below a warning, the HTTP client
    writes a line for each request to a model's server, the reloader one for each change,
    and Alembic two each time the database's migration is read.

    A copy of uvicorn's, which uvicorn writes into, and given to uvicorn rather than set
    up here: with --reload, the server runs in a process of its own, which sets up its
    log from uvicorn's config alone.
    """
    level = logging.getLevelNamesMapping()[settings.log_level.upper()]
    config = copy.deepcopy(LOGGING_CONFIG)
    config["formatters"]["actio"] = {"format": LOG_FORMAT}
    config["handlers"]["actio"] = {
        "class": "logging.StreamHandler",
        "formatter": "actio",
        "stream": "ext://sys.stderr",
    }
    config["loggers"] |= {"actio": {"level": level}, "procrastinate": {"level": level}}
    config["root"] = {"handlers": ["actio"], "level": max(level, logging.WARNING)}
    return config
