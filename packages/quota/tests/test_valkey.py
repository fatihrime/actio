"""What only the Valkey engine can show: atomicity under concurrency, and its storage."""

import asyncio
import random
from collections.abc import AsyncIterator

import pytest
from valkey.asyncio import Valkey

from actio_quota import Bucket, Denied, Estimate, Fixed, Granted, QuotaOracle, Rates, Rolling
from actio_quota.cost import MAX_CREDITS
from actio_quota.engine import IDLE_TTL_MS, ValkeyQuota

HOUR = 3_600_000
T0 = 1_800_000_000_000
TTL = 600_000


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def clients(valkey_url: str) -> AsyncIterator[list[Valkey]]:
    """Eight separate connections, standing in for eight API or worker processes."""
    pool = [Valkey.from_url(valkey_url) for _ in range(8)]
    await pool[0].flushdb()
    yield pool
    for client in pool:
        await client.aclose()


def buckets(limit: int) -> list[Bucket]:
    return [
        Bucket("session", limit, Rolling(5 * HOUR)),
        Bucket("week", limit * 4, Fixed(T0 - HOUR, T0 + 100 * HOUR)),
    ]


@pytest.mark.anyio
async def test_concurrent_requests_admit_exactly_what_fits(clients: list[Valkey]) -> None:
    """256 requests at once, on 8 connections: exactly floor(1000 / 7) are admitted.

    A check-then-charge counter would admit far more here: every request would read the
    same balance before any of them wrote. Each RESERVE runs as one atomic script.
    """
    engines = [ValkeyQuota(client) for client in clients]
    limits = buckets(1_000)
    assert await engines[0].rebuild("u1", limits, [], now_ms=T0, if_epoch=None)

    async def request(i: int) -> object:
        engine = engines[i % len(engines)]
        return await engine.reserve("u1", f"r{i}", limits, Estimate.flat(7), ttl_ms=TTL, now_ms=T0)

    results = await asyncio.gather(*(request(i) for i in range(256)))
    granted = [r for r in results if isinstance(r, Granted)]
    assert len(granted) == 1_000 // 7

    # 30% of the admitted requests crash and never settle; the rest settle concurrently.
    crashed = set(random.Random(0).sample([g.rid for g in granted], k=len(granted) * 3 // 10))
    finished = [g for g in granted if g.rid not in crashed]
    await asyncio.gather(
        *(
            engines[i % len(engines)].settle("u1", g.rid, 7, now_ms=T0 + 1)
            for i, g in enumerate(finished)
        )
    )
    session = (await engines[0].snapshot("u1", limits, now_ms=T0 + 1) or ())[0]
    assert (session.used, session.reserved) == (7 * len(finished), 7 * len(crashed))

    # Once the crashed reservations expire, collection gives every held credit back.
    later = T0 + TTL
    while await engines[0].collect_expired("u1", now_ms=later):
        pass
    session = (await engines[0].snapshot("u1", limits, now_ms=later) or ())[0]
    assert (session.used, session.reserved) == (7 * len(finished), 0)


@pytest.mark.anyio
@pytest.mark.parametrize("headroom", [MAX_CREDITS, MAX_CREDITS - 1, 1, 0])
async def test_large_numbers_stay_exact(clients: list[Valkey], headroom: int) -> None:
    """Near 2^53, a float slip would grant one token too many: both engines must agree."""
    engine, oracle = ValkeyQuota(clients[0]), QuotaOracle()
    limits = [Bucket("big", MAX_CREDITS, Rolling(HOUR))]
    # Odd prices, so the division that sizes the answer isn't exact.
    estimate = Estimate.tokens(Rates(input=999_983, output=7_919), input_tokens=3, max_output=2**40)
    used = Estimate.flat(MAX_CREDITS - headroom)
    assert oracle.rebuild("u1", limits, [], now_ms=T0, if_epoch=None)
    assert await engine.rebuild("u1", limits, [], now_ms=T0, if_epoch=None)
    oracle.reserve("u1", "fill", limits, used, ttl_ms=TTL, now_ms=T0)
    await engine.reserve("u1", "fill", limits, used, ttl_ms=TTL, now_ms=T0)

    ours = oracle.reserve("u1", "r1", limits, estimate, ttl_ms=TTL, now_ms=T0)
    theirs = await engine.reserve("u1", "r1", limits, estimate, ttl_ms=TTL, now_ms=T0)
    assert theirs == ours
    assert await engine.dump("u1", now_ms=T0) == oracle.dump("u1", now_ms=T0)


@pytest.mark.anyio
async def test_scripts_are_resent_after_valkey_forgets_them(clients: list[Valkey]) -> None:
    engine = ValkeyQuota(clients[0])
    assert await engine.rebuild("u1", buckets(10), [], now_ms=T0, if_epoch=None)
    await clients[0].script_flush()  # as after a restart
    grant = await engine.reserve("u1", "r1", buckets(10), Estimate.flat(3), ttl_ms=TTL, now_ms=T0)
    assert isinstance(grant, Granted)


@pytest.mark.anyio
async def test_a_users_keys_expire_together_when_idle(clients: list[Valkey]) -> None:
    engine = ValkeyQuota(clients[0], prefix="q")
    assert await engine.rebuild("u1", buckets(10), [], now_ms=T0, if_epoch=None)
    await engine.reserve("u1", "r1", buckets(10), Estimate.flat(3), ttl_ms=TTL, now_ms=T0)
    await engine.settle("u1", "r1", 3, now_ms=T0)
    keys = sorted(k.decode() for k in await clients[0].keys("q:{u:u1}:*"))
    # rsv and rsv_exp are gone: they emptied when r1 settled.
    assert keys == ["q:{u:u1}:b", "q:{u:u1}:done", "q:{u:u1}:done_exp", "q:{u:u1}:meta"]
    for key in keys:
        assert 0 < await clients[0].pttl(key) <= IDLE_TTL_MS


@pytest.mark.anyio
async def test_a_denial_that_releases_reservations_keeps_the_keys_expiring(
    clients: list[Valkey],
) -> None:
    """A denied reserve still releases expired reservations, which writes done markers.

    If the user never comes back, those markers must expire with their other keys.
    """
    engine = ValkeyQuota(clients[0], prefix="q")
    assert await engine.rebuild("u1", buckets(10), [], now_ms=T0, if_epoch=None)
    await engine.reserve("u1", "r1", buckets(10), Estimate.flat(3), ttl_ms=TTL, now_ms=T0)
    denied = await engine.reserve(
        "u1", "r2", buckets(10), Estimate.flat(11), ttl_ms=TTL, now_ms=T0 + TTL
    )
    assert isinstance(denied, Denied)  # 11 never fits in 10, but r1 had expired
    keys = sorted(k.decode() for k in await clients[0].keys("q:{u:u1}:*"))
    assert keys == ["q:{u:u1}:b", "q:{u:u1}:done", "q:{u:u1}:done_exp", "q:{u:u1}:meta"]
    for key in keys:
        assert 0 < await clients[0].pttl(key) <= IDLE_TTL_MS, key


@pytest.mark.anyio
async def test_users_are_isolated(clients: list[Valkey]) -> None:
    engine = ValkeyQuota(clients[0])
    for user in ("u1", "u2"):
        assert await engine.rebuild(user, buckets(10), [], now_ms=T0, if_epoch=None)
    assert isinstance(
        await engine.reserve("u1", "r1", buckets(10), Estimate.flat(10), ttl_ms=TTL, now_ms=T0),
        Granted,
    )
    assert isinstance(
        await engine.reserve("u2", "r1", buckets(10), Estimate.flat(10), ttl_ms=TTL, now_ms=T0),
        Granted,
    )


def test_rejects_ids_that_could_escape_the_key_layout() -> None:
    engine = ValkeyQuota(Valkey())  # never connects: validation comes first
    with pytest.raises(ValueError, match="user id"):
        asyncio.run(engine.epoch("u1}:x"))
    with pytest.raises(ValueError, match="key prefix"):
        ValkeyQuota(Valkey(), prefix="{q}")
