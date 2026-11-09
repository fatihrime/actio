"""Shared fixtures: Hypothesis profiles, and the two engines under test.

Hypothesis profiles: HYPOTHESIS_PROFILE=dev|ci|nightly (default: dev).

Valkey: tests use ACTIO_TEST_VALKEY_URL if it's set. Otherwise they start a throwaway
container from the image pinned in compose.yaml, so tests run what production runs. If
Docker isn't available they're skipped, unless ACTIO_REQUIRE_DOCKER=1 (set in CI), which
turns the skip into a failure.
"""

import asyncio
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings
from valkey.asyncio import Valkey

from actio_quota import (
    Bucket,
    BucketView,
    Estimate,
    Finished,
    LedgerEntry,
    QuotaOracle,
    ReserveResult,
)
from actio_quota.engine import ValkeyQuota

settings.register_profile("dev", max_examples=100)
settings.register_profile("ci", max_examples=200, print_blob=True)
settings.register_profile("nightly", max_examples=5_000, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

COMPOSE = Path(__file__).resolve().parents[3] / "compose.yaml"


def _valkey_image() -> str:
    match = re.search(r"image: (valkey/valkey:\S+@sha256:[0-9a-f]{64})", COMPOSE.read_text())
    assert match, "compose.yaml must pin the Valkey image by digest"
    return match.group(1)


def _docker(*args: str) -> str:
    docker = shutil.which("docker")
    assert docker is not None
    done = subprocess.run([docker, *args], capture_output=True, text=True, check=True)  # noqa: S603
    return done.stdout.strip()


def _unavailable(reason: str) -> None:
    if os.environ.get("ACTIO_REQUIRE_DOCKER") == "1":
        pytest.fail(f"Valkey is required here, but {reason}")
    pytest.skip(f"Valkey tests need Docker: {reason}")


@pytest.fixture(scope="session")
def valkey_url() -> Iterator[str]:
    """A Valkey server for this test session, with persistence off."""
    if url := os.environ.get("ACTIO_TEST_VALKEY_URL"):
        yield url
        return
    if shutil.which("docker") is None:
        _unavailable("the docker command isn't installed")
    try:
        _docker("info", "--format", "{{.ServerVersion}}")
    except subprocess.CalledProcessError:
        _unavailable("the Docker daemon isn't running")
    container = _docker(
        "run", "--detach", "--rm", "--read-only", "--tmpfs", "/data",
        "--publish", "127.0.0.1::6379", _valkey_image(),
        "valkey-server", "--save", "", "--appendonly", "no",
    )  # fmt: skip
    try:
        host_port = _docker("port", container, "6379/tcp").splitlines()[0]
        url = f"valkey://{host_port}/0"
        _wait_until_ready(url)
        yield url
    finally:
        _docker("rm", "--force", container)


def _wait_until_ready(url: str) -> None:
    async def ping() -> bool:
        async with Valkey.from_url(url) as client:
            return bool(await client.ping())

    deadline = time.monotonic() + 30
    while True:
        try:
            if asyncio.run(ping()):
                return
        except OSError:
            if time.monotonic() > deadline:
                raise
        time.sleep(0.1)


class SyncValkeyQuota:
    """`ValkeyQuota` behind the oracle's synchronous interface, for side-by-side tests."""

    def __init__(self, runner: asyncio.Runner, client: Valkey) -> None:
        self._run = runner.run
        self._client = client
        self.engine = ValkeyQuota(client, prefix="test")

    def reserve(
        self,
        user_id: str,
        rid: str,
        buckets: Sequence[Bucket],
        estimate: Estimate,
        *,
        ttl_ms: int,
        now_ms: int,
    ) -> ReserveResult:
        return self._run(
            self.engine.reserve(user_id, rid, buckets, estimate, ttl_ms=ttl_ms, now_ms=now_ms)
        )

    def settle(self, user_id: str, rid: str, credits: int, *, now_ms: int) -> Finished:
        return self._run(self.engine.settle(user_id, rid, credits, now_ms=now_ms))

    def release(self, user_id: str, rid: str, *, now_ms: int) -> Finished:
        return self._run(self.engine.release(user_id, rid, now_ms=now_ms))

    def extend(self, user_id: str, rid: str, *, ttl_ms: int, now_ms: int) -> bool:
        return self._run(self.engine.extend(user_id, rid, ttl_ms=ttl_ms, now_ms=now_ms))

    def collect_expired(self, user_id: str, *, now_ms: int) -> int:
        return self._run(self.engine.collect_expired(user_id, now_ms=now_ms))

    def snapshot(
        self, user_id: str, buckets: Sequence[Bucket], *, now_ms: int
    ) -> tuple[BucketView, ...] | None:
        return self._run(self.engine.snapshot(user_id, buckets, now_ms=now_ms))

    def rebuild(
        self,
        user_id: str,
        buckets: Sequence[Bucket],
        ledger: Sequence[LedgerEntry],
        *,
        now_ms: int,
        if_epoch: int | None,
    ) -> bool:
        return self._run(
            self.engine.rebuild(user_id, buckets, ledger, now_ms=now_ms, if_epoch=if_epoch)
        )

    def epoch(self, user_id: str) -> int | None:
        return self._run(self.engine.epoch(user_id))

    def dump(self, user_id: str, *, now_ms: int) -> dict[str, Any] | None:
        return self._run(self.engine.dump(user_id, now_ms=now_ms))

    def flush(self) -> None:
        """Lose every key, as if Valkey restarted without persistence."""
        self._run(_flush(self._client))


@pytest.fixture(scope="session")
def valkey_runner(valkey_url: str) -> Iterator[tuple[asyncio.Runner, Valkey]]:
    """One event loop and client for the whole session."""
    with asyncio.Runner() as runner:
        client = runner.run(_connect(valkey_url))
        yield runner, client
        runner.run(client.aclose())


async def _connect(url: str) -> Valkey:
    client: Valkey = Valkey.from_url(url)
    return client


async def _flush(client: Valkey) -> None:
    await client.flushdb()


@pytest.fixture
def valkey_quota(valkey_runner: tuple[asyncio.Runner, Valkey]) -> SyncValkeyQuota:
    """The Valkey engine, on an empty database."""
    engine = SyncValkeyQuota(*valkey_runner)
    engine.flush()
    return engine


type Engine = QuotaOracle | SyncValkeyQuota


@pytest.fixture(params=["oracle", "valkey"])
def fresh(request: pytest.FixtureRequest) -> Engine:
    """Each engine in turn, with no users yet: the rule tests run against both."""
    if request.param == "oracle":
        return QuotaOracle()
    quota: SyncValkeyQuota = request.getfixturevalue("valkey_quota")
    return quota
