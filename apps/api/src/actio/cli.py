"""Command-line entry point: ``python -m actio --role api``.

Actio is one application that runs as different processes, chosen with ``--role``:

- ``api`` serves HTTP;
- ``migrate`` brings the database schema up to date, then exits. It runs before the API
  starts, as its own step, so the API never changes the schema it is serving from.

The background worker role arrives with the job queue.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from actio.core import schema
from actio.core.settings import Settings

ROLES = ("api", "migrate")


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

    settings = Settings()
    if args.role == "migrate":
        revision = schema.upgrade(settings.database_url.get_secret_value())
        sys.stdout.write(f"The database schema is at migration {revision}.\n")
        return

    uvicorn.run(
        "actio.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        server_header=False,  # don't advertise the server software
        forwarded_allow_ips=settings.forwarded_allow_ips,
        reload=args.reload,
        # Watch only this package, not the whole repository.
        reload_dirs=[str(Path(__file__).parent)] if args.reload else None,
    )
