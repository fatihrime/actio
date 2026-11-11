"""Password hashing: Argon2id, off the event loop, a few at a time."""

import asyncio
from typing import Any, Literal

import argon2.profiles
import pytest
from argon2 import PasswordHasher

from actio.identity.passwords import Passwords

pytestmark = pytest.mark.anyio

GOOD = "correct horse battery staple"


async def test_a_password_matches_its_own_hash_and_no_other(passwords: Passwords) -> None:
    hashed = await passwords.hash(GOOD)

    assert await passwords.verify(hashed, GOOD)
    assert not await passwords.verify(hashed, GOOD + " ")
    assert not await passwords.verify(hashed, "")


async def test_the_hash_is_argon2id_and_salted(passwords: Passwords) -> None:
    first, second = await passwords.hash(GOOD), await passwords.hash(GOOD)

    assert first.startswith("$argon2id$")
    assert first != second  # a new salt each time: equal passwords don't look equal
    assert GOOD not in first


def test_production_uses_the_recommended_parameters() -> None:
    """RFC 9106's second profile: 64 MiB and three passes."""
    hasher = PasswordHasher()

    assert Passwords()._hasher.memory_cost == hasher.memory_cost == 65_536  # KiB
    assert (hasher.time_cost, hasher.parallelism) == (3, 4)


class Recording(PasswordHasher):
    """A real hasher that notes what it is asked to do."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.verified: list[str | bytes] = []
        self.running = 0
        self.most_at_once = 0

    def verify(self, hash: str | bytes, password: str | bytes) -> Literal[True]:  # noqa: A002
        self.verified.append(hash)
        return super().verify(hash, password)

    def hash(self, password: str | bytes, *, salt: bytes | None = None) -> str:
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        try:
            return super().hash(password, salt=salt)
        finally:
            self.running -= 1


def recording() -> Recording:
    cheap = argon2.profiles.CHEAPEST
    return Recording(
        time_cost=cheap.time_cost, memory_cost=cheap.memory_cost, parallelism=cheap.parallelism
    )


async def test_no_account_means_no_and_still_does_the_work() -> None:
    """Otherwise "no such email" would answer faster than "wrong password"."""
    hasher = recording()
    passwords = Passwords(hasher)

    assert not await passwords.verify(None, GOOD)
    assert hasher.verified == [passwords._decoy]  # a real check was made, against a decoy


@pytest.mark.parametrize("stored", ["", "plaintext", "$argon2id$v=19$m=8,t=1,p=1$broken"])
async def test_a_stored_value_that_is_not_a_hash_matches_nothing(
    passwords: Passwords, stored: str
) -> None:
    assert not await passwords.verify(stored, GOOD)


async def test_a_hash_made_with_weaker_parameters_asks_to_be_replaced(passwords: Passwords) -> None:
    old = await passwords.hash(GOOD)
    stronger = Passwords(
        PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)  # still tiny, but more
    )

    assert not passwords.needs_rehash(old)
    assert stronger.needs_rehash(old)
    assert await stronger.verify(old, GOOD)  # the old hash still works until then


async def test_only_a_few_hashes_run_at_once() -> None:
    """Each one takes 64 MiB in production: a burst of logins must not take them all at once."""
    hasher = recording()
    passwords = Passwords(hasher, at_once=2)

    hashes = await asyncio.gather(*(passwords.hash(f"password number {i}") for i in range(12)))

    assert len(set(hashes)) == 12
    assert 1 <= hasher.most_at_once <= 2
