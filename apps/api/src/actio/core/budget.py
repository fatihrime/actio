"""What the cloud providers' plans let this installation send to their models, counted in
Valkey (ADR 0030).

A provider's plan takes only so many requests and tokens from an account in a minute
and in a day, for each model. Each such limit is a `Bucket`: it holds up to its size,
and refills steadily, by its size over its period. A bucket is named by its limit, its
provider and its model, so whatever counts in it from anywhere counts in the same one:
two models of the models file that are one model on one provider, and the API's process
and the worker's.

One Lua script does the counting (`budget.lua`), so that what processes do at the same
time adds up. It does two things, and refuses nothing:

- `Budgets.look` says, for each bucket, how long until an amount fits it: no time at
  all when it fits now. It writes nothing;
- `Budgets.add` takes an amount from each bucket, or gives one back, no further than
  empty and no further than full.

Who is asked, and what a request takes, is decided above this, in `actio.catalog`.

The rate limiter beside this (`rate_limit.py`) counts one attempt at a time, and refuses
one that doesn't fit. A bucket here holds many tokens, more than one a millisecond in a
large one, takes whole amounts, which may be put right later, and never refuses: a look
is apart from what is taken.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Literal

from valkey.asyncio import Valkey
from valkey.exceptions import ValkeyError

type Counts = Literal["requests", "tokens"]
type Period = Literal["minute", "day"]

_PERIODS_MS: dict[Period, int] = {"minute": 60_000, "day": 86_400_000}
_PROVIDER = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


class BudgetsUnavailableError(Exception):
    """Valkey didn't answer: nothing could be looked at, or counted."""


@dataclass(frozen=True, slots=True)
class Bucket:
    """One limit of a provider's model, as Actio keeps to it: `size` requests, or
    tokens, a minute or a day, refilled steadily, by `size` over the period.

    Buckets that name the same limit, provider and model are one, wherever they are
    counted from."""

    counts: Counts
    period: Period
    provider: str
    """The provider's name in the models file."""
    model: str
    """The model's name on the provider's server."""
    size: int
    """What a full bucket holds."""

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError("a bucket holds 1 at the least")
        # Its key's parts are split at ':', and only the model, which comes last, may hold
        # one.
        if _PROVIDER.fullmatch(self.provider) is None:
            raise ValueError("a provider's name is lower-case letters, digits, '-' and '_'")
        if not self.model:
            raise ValueError("a bucket is of a model")

    @property
    def limit(self) -> str:
        """What it counts over which period, as its key names it: `tokens-a-minute`."""
        return f"{self.counts}-a-{self.period}"

    @property
    def period_ms(self) -> int:
        """How long an empty bucket takes to be full again."""
        return _PERIODS_MS[self.period]

    def __str__(self) -> str:
        """The limit in words: `tokens a minute`."""
        return f"{self.counts} a {self.period}"


class Budgets:
    """The buckets of every provider's model, in one Valkey database."""

    def __init__(self, client: Valkey, *, prefix: str = "b") -> None:
        self._prefix = prefix
        script = (files("actio.core") / "budget.lua").read_text()
        # Sent once, then called by its hash. If Valkey restarts and forgets it, the
        # client sends it again.
        self._run = client.register_script(script)

    def key(self, bucket: Bucket) -> str:
        """Where a bucket is kept: its limit, its provider, and its model last, since a
        model's name on its server may hold any character. Readable, not hashed: these
        are names from the models file, never what people typed."""
        return f"{self._prefix}:{bucket.limit}:{bucket.provider}:{bucket.model}"

    async def look(self, *amounts: tuple[Bucket, int], now: datetime) -> list[timedelta]:
        """For each bucket, how long until its amount, 0 or more, fits it: no time at
        all, `timedelta(0)`, when it fits now. An amount larger than a bucket's size fits
        it when it is full. Writes nothing.

        Raises `BudgetsUnavailableError` when Valkey doesn't answer."""
        if any(amount < 0 for _, amount in amounts):
            raise ValueError("a look is for an amount of 0 or more")
        waits = await self._call("look", amounts, now)
        return [timedelta(milliseconds=wait) for wait in waits]

    async def add(self, *amounts: tuple[Bucket, int], now: datetime) -> None:
        """Take each amount from its bucket, or give it back when it is negative. Never
        refused: a bucket goes no lower than empty, and no higher than full.

        Raises `BudgetsUnavailableError` when Valkey doesn't answer."""
        await self._call("add", amounts, now)

    async def _call(
        self,
        operation: Literal["look", "add"],
        amounts: tuple[tuple[Bucket, int], ...],
        now: datetime,
    ) -> list[int]:
        if not amounts:
            return []
        # Whole milliseconds, without a float between.
        args: list[str | int] = [operation, (now - _EPOCH) // _MILLISECOND]
        for bucket, amount in amounts:
            args += [bucket.size, bucket.period_ms, amount]
        try:
            answered = await self._run(keys=[self.key(bucket) for bucket, _ in amounts], args=args)
        except (ValkeyError, OSError) as error:
            raise BudgetsUnavailableError from error
        return [int(number) for number in answered]
