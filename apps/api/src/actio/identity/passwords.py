"""Passwords: hashed with Argon2id, which is slow and memory-hungry on purpose.

Someone who steals the table has to spend that time and memory on every guess. The
default parameters are the library's, RFC 9106's second recommended profile: 64 MiB of
memory and three passes, about 50 ms on a laptop.

Hashing blocks, so it runs in a worker thread, and at most a few hashes run at once:
each one takes 64 MiB, and a burst of logins must not exhaust the process's memory.
"""

import asyncio
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 10
MAX_LENGTH = 256
"""A password's length in characters. The upper bound keeps one request from making
the server hash megabytes."""


class Passwords:
    """Hashes and checks passwords. One per process."""

    def __init__(self, hasher: PasswordHasher | None = None, *, at_once: int = 2) -> None:
        self._hasher = hasher or PasswordHasher()
        self._slots = asyncio.Semaphore(at_once)
        # Checked when there is no account to check against, so that "no such email"
        # takes as long as "wrong password", and the two can't be told apart by timing.
        self._decoy = self._hasher.hash(secrets.token_urlsafe(16))

    async def hash(self, password: str) -> str:
        async with self._slots:
            return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, password_hash: str | None, password: str) -> bool:
        """Whether `password` matches. With no hash, does the same work and says no."""
        async with self._slots:
            matches = await asyncio.to_thread(self._matches, password_hash or self._decoy, password)
        return matches and password_hash is not None

    def needs_rehash(self, password_hash: str) -> bool:
        """True when the hash was made with weaker parameters than today's, so it can
        be replaced the next time its owner logs in."""
        return self._hasher.check_needs_rehash(password_hash)

    def _matches(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False
