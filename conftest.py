"""Fixtures shared by every test suite: throwaway Valkey and Postgres servers.

Tests use a server you already run when its URL is given:

- ACTIO_TEST_VALKEY_URL, such as valkey://127.0.0.1:6379/0;
- ACTIO_TEST_POSTGRES_URL, such as postgresql://actio:password@127.0.0.1:5432/actio,
  for a user that may create databases.

Otherwise they start a throwaway container from the image pinned in compose.yaml, so
tests run what production runs. If Docker isn't available they are skipped, unless
ACTIO_REQUIRE_DOCKER=1 (set by `make test`, and so in CI), which turns the skip into a
failure.
"""

import asyncio
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from valkey.asyncio import Valkey
from valkey.exceptions import ConnectionError as ValkeyConnectionError
from valkey.exceptions import TimeoutError as ValkeyTimeoutError

COMPOSE = Path(__file__).resolve().parent / "compose.yaml"


def _pinned_image(repository: str) -> str:
    pattern = rf"image: ({re.escape(repository)}:\S+@sha256:[0-9a-f]{{64}})"
    match = re.search(pattern, COMPOSE.read_text())
    if match is None:
        raise RuntimeError(f"compose.yaml must pin the {repository} image by digest")
    return match.group(1)


def _docker(*args: str) -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("the docker command isn't installed")
    done = subprocess.run([docker, *args], capture_output=True, text=True, check=True)  # noqa: S603
    return done.stdout.strip()


def _docker_running() -> bool:
    try:
        _docker("info", "--format", "{{.ServerVersion}}")
    except subprocess.CalledProcessError:
        return False
    return True


def _unavailable(service: str, reason: str) -> None:
    if os.environ.get("ACTIO_REQUIRE_DOCKER") == "1":
        pytest.fail(f"{service} is required here, but {reason}")
    pytest.skip(f"{service} tests need Docker: {reason}")


@contextmanager
def _container(
    service: str,
    port: int,
    *run_args: str,
    answers: Callable[[str], None],
    not_yet: tuple[type[Exception], ...],
) -> Iterator[str]:
    """Run a throwaway container and yield the local `host:port` its `port` is published on.

    `answers` is called with that address until it stops raising one of `not_yet`. On
    Linux, Docker's port proxy accepts connections before the server in the container
    is listening, then drops them, so "the port is open" isn't enough.
    """
    if shutil.which("docker") is None:
        _unavailable(service, "the docker command isn't installed")
    if not _docker_running():
        _unavailable(service, "the Docker daemon isn't running")
    container = _docker("run", "--detach", "--rm", "--publish", f"127.0.0.1::{port}", *run_args)
    try:
        address = _docker("port", container, f"{port}/tcp").splitlines()[0]
        deadline = time.monotonic() + 30
        while True:
            try:
                answers(address)
            except not_yet as error:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"{service} didn't answer on {address} within 30 s. "
                        f"Container log:\n{_container_log(container)}"
                    ) from error
                time.sleep(0.1)
            else:
                break
        yield address
    finally:
        _docker("rm", "--force", container)


def _container_log(container: str) -> str:
    try:
        return _docker("logs", container) or "(empty)"
    except subprocess.CalledProcessError:
        return "(none: the container has already exited)"


def _ping_valkey(address: str) -> None:
    async def ping() -> None:
        async with Valkey.from_url(f"valkey://{address}/0") as client:
            await client.ping()

    asyncio.run(ping())


def _ping_postgres(address: str) -> None:
    url = f"postgresql://actio:{POSTGRES_TEST_PASSWORD}@{address}/actio"
    psycopg.connect(url, connect_timeout=2).close()


POSTGRES_TEST_PASSWORD = "throwaway"  # noqa: S105 - for a container that lives one test run


@pytest.fixture(scope="session")
def valkey_url() -> Iterator[str]:
    """A Valkey server for this test session, with persistence off."""
    if url := os.environ.get("ACTIO_TEST_VALKEY_URL"):
        yield url
        return
    with _container(
        "Valkey",
        6379,
        "--read-only",
        "--tmpfs",
        "/data",
        _pinned_image("valkey/valkey"),
        "valkey-server",
        "--save",
        "",
        "--appendonly",
        "no",
        answers=_ping_valkey,
        # valkey-py reports a dropped connection as its own error, which isn't an OSError.
        not_yet=(OSError, ValkeyConnectionError, ValkeyTimeoutError),
    ) as address:
        yield f"valkey://{address}/0"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A Postgres server for this test session, as a user that may create databases.

    Its data lives in memory and it skips the disk-safety work a real database needs,
    which makes it fast and unfit for anything but tests.
    """
    if url := os.environ.get("ACTIO_TEST_POSTGRES_URL"):
        yield url
        return
    with _container(
        "Postgres",
        5432,
        "--tmpfs",
        "/var/lib/postgresql/data",
        "--env",
        "POSTGRES_USER=actio",
        "--env",
        f"POSTGRES_PASSWORD={POSTGRES_TEST_PASSWORD}",
        "--env",
        "POSTGRES_DB=actio",
        _pinned_image("pgvector/pgvector"),
        "postgres",
        "-c",
        "fsync=off",
        "-c",
        "synchronous_commit=off",
        "-c",
        "full_page_writes=off",
        answers=_ping_postgres,
        not_yet=(psycopg.OperationalError,),
    ) as address:
        yield f"postgresql://actio:{POSTGRES_TEST_PASSWORD}@{address}/actio"
