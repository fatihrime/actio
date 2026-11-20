"""Command-line entry point: ``python -m actio --role api``.

Actio is one application that runs as different processes, chosen with ``--role``:

- ``api`` serves HTTP;
- ``migrate`` brings the database schema up to date, then exits. It runs before the API
  starts, as its own step, so the API never changes the schema it is serving from;
- ``worker`` runs the background jobs, until it is told to stop;
- ``all`` serves HTTP as ``api`` does, with the worker inside the same process: for the
  smallest installations, and for ``make dev``. It doesn't migrate either.
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
from uvicorn.config import LOGGING_CONFIG

from actio import worker
from actio.catalog import CatalogError, read_file
from actio.core import jobs, schema
from actio.core.settings import Settings
from actio.usage.plans import PlansError
from actio.usage.plans import load as load_plans

ROLES = ("api", "migrate", "worker", "all")

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
    parser = argparse.ArgumentParser(prog="actio", description="Run an Actio process.")
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
