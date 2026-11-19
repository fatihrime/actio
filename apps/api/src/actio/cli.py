"""Command-line entry point: ``python -m actio --role api``.

Actio is one application that runs as different processes, chosen with ``--role``:

- ``api`` serves HTTP;
- ``migrate`` brings the database schema up to date, then exits. It runs before the API
  starts, as its own step, so the API never changes the schema it is serving from;
- ``worker`` runs the background jobs, until it is told to stop.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from actio import worker
from actio.catalog import CatalogError, read_file
from actio.core import jobs, schema
from actio.core.settings import Settings
from actio.usage.plans import PlansError
from actio.usage.plans import load as load_plans

ROLES = ("api", "migrate", "worker")

WAITS_FOR_REQUESTS_S = 25
"""How long the web server waits, when it is told to stop, for the requests it is
answering, before it cuts them off.

The ones that last are the readers of answers being written. Those answers end by
themselves: they are halted `GRACE` after the process was told to stop, and recorded
within `LAST_WORDS` (`actio.chat.running`), and their readers with them. So this wait
has to be the longer one, or a reader is cut off before being told how the answer
ended. And whatever stops the process has to wait longer still: `stop_grace_period` in
compose.yaml. Tests hold the three together."""


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="actio", description="Run an Actio process.")
    parser.add_argument(
        "--role", choices=ROLES, default="api", help="process to run (default: %(default)s)"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="restart the API when the code changes (development only)",
    )
    args = parser.parse_args(argv)
    if args.reload and args.role != "api":
        parser.error(f"--reload restarts the API's server; the {args.role} role has none")

    settings = Settings()
    if args.role == "migrate":
        revision = schema.upgrade(settings.database_url.get_secret_value())
        sys.stdout.write(f"The database schema is at migration {revision}.\n")
        return
    if args.role == "worker":
        # No web server sets the log up here. One line a record, on stderr.
        logging.basicConfig(
            level=settings.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        # Reading which migration the database is at makes Alembic say, at the info
        # level, how it would run migrations. The worker runs none.
        logging.getLogger("alembic").setLevel(logging.WARNING)
        try:
            asyncio.run(worker.run(settings))
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

    uvicorn.run(
        "actio.app:create_app",
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
    )
