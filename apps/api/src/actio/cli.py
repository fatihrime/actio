"""Command-line entry point: ``python -m actio --role api``.

Actio is one application that runs as different processes, chosen with ``--role``.
Phase 0 has only the HTTP API. The background worker role arrives with the job
queue in Phase 1.
"""

import argparse
from collections.abc import Sequence

import uvicorn

from actio.settings import Settings

ROLES = ("api",)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="actio", description="Run an Actio process.")
    parser.add_argument(
        "--role", choices=ROLES, default="api", help="process to run (default: %(default)s)"
    )
    parser.parse_args(argv)

    settings = Settings()
    uvicorn.run(
        "actio.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        server_header=False,  # don't advertise the server software
    )
