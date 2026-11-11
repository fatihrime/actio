"""Rate limits: how often something may happen, counted in Valkey.

A limit is a token bucket. "30/hour" is a bucket of 30 tokens that gets one back every
two minutes: 30 at once are fine, and so are 30 an hour for as long as you like. Each
attempt takes a token; with none left, the attempt is refused, and told how long to
wait.

One Lua script does the counting (`rate_limit.lua`), so it is atomic across every
process that shares the Valkey server: two requests arriving together can't both take
the last token. Several limits can be charged in one call, all or nothing.

This is for attempts, such as logins from one address. What a model's answer costs is
counted by the usage-limits engine in `packages/quota`, which reserves and settles.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Self

from valkey.asyncio import Valkey
from valkey.exceptions import ValkeyError

_UNITS_MS = {"second": 1_000, "minute": 60_000, "hour": 3_600_000, "day": 86_400_000}
_FORM = re.compile(r"([1-9][0-9]{0,8})/(second|minute|hour|day)")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


class LimiterUnavailableError(Exception):
    """Valkey didn't answer, so nothing could be counted."""


@dataclass(frozen=True, slots=True)
class Rate:
    """`count` per period: `count` at once, then one more every `interval_ms`."""

    count: int
    period_ms: int

    def __post_init__(self) -> None:
        if self.count < 1 or self.period_ms < self.count:
            raise ValueError("a rate is at least one per period, and at most one per millisecond")

    @classmethod
    def parse(cls, text: str) -> Self:
        """Read a rate written as `30/hour`: a count, then second, minute, hour or day."""
        match = _FORM.fullmatch(text.strip())
        if match is None:
            raise ValueError("must look like 30/hour: a count, then second, minute, hour or day")
        return cls(int(match[1]), _UNITS_MS[match[2]])

    @property
    def interval_ms(self) -> int:
        """How long one token takes to come back."""
        return self.period_ms // self.count

    def __str__(self) -> str:
        unit = next((name for name, ms in _UNITS_MS.items() if ms == self.period_ms), None)
        return f"{self.count}/{unit}" if unit else f"{self.count} per {self.period_ms} ms"


@dataclass(frozen=True, slots=True)
class Take:
    """One token from the bucket that the limit `name` keeps for `subject`.

    `name` says what is being counted, such as `logins-address`. `subject` says for
    whom: an address, an email address, an account id.
    """

    name: str
    subject: str
    rate: Rate

    def __post_init__(self) -> None:
        if _NAME.fullmatch(self.name) is None:
            raise ValueError("a limit's name is lower-case letters, digits, '-' and '_'")


class RateLimiter:
    """Token buckets for any number of limits and subjects, in one Valkey database."""

    def __init__(self, client: Valkey, *, prefix: str = "rl") -> None:
        self._prefix = prefix
        script = (files("actio.core") / "rate_limit.lua").read_text()
        # Sent once, then called by its hash. If Valkey restarts and forgets it, the
        # client sends it again.
        self._spend = client.register_script(script)

    def key(self, take: Take) -> str:
        """Where a bucket is kept. The subject is hashed: Valkey never holds the email
        addresses people type, or their network addresses."""
        subject = hashlib.sha256(take.subject.encode()).hexdigest()[:32]
        return f"{self._prefix}:{take.name}:{subject}"

    async def spend(self, *takes: Take, now: datetime) -> timedelta:
        """Take one token from each bucket, if every one of them has a token.

        Returns no time at all, `timedelta(0)`, when the tokens were taken. Otherwise it
        returns how long until every bucket has one, and nothing was taken: a refused
        attempt costs nothing.
        """
        args = [(now - _EPOCH) // _MILLISECOND]  # whole milliseconds, without a float between
        for take in takes:
            args += [take.rate.count, take.rate.interval_ms]
        try:
            wait_ms = await self._spend(keys=[self.key(take) for take in takes], args=args)
        except (ValkeyError, OSError) as error:
            raise LimiterUnavailableError from error
        return timedelta(milliseconds=int(wait_ms))
