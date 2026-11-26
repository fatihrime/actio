"""Whether a model keeps failing: a circuit breaker, one model's.

A breaker is told of each call to its model, a call being one try, one stream of its
client: when it begins (`Breaker.begins`), and how it ended (`Call.ends`), answered,
failed, or with nothing to say. From the calls that ended it says whether the model is
failing now (`Breaker.failing`). A model that is failing is asked after the models of
its chain that aren't, for a while: its caller decides that. A breaker never keeps a
model from being asked.

The rules:

- **R1, closed.** Each call that ends answered or failed is kept with the moment it
  ended: the last `LAST_CALLS`, none that ended longer ago than `WITHIN`.
- **R2, it opens** when a call ends failed and, with it, the calls kept are `AT_LEAST`
  or more, and half or more of them failed. It is then open for `WAITS[0]`, and the
  calls kept are forgotten. It opens only at a failure: an answer never opens it.
- **R3, open, before its wait is over,** the model is failing. It may still be asked, as
  a last resort or as a chain's only model: a call that ends answered then closes the
  breaker (R5), and one that ends failed changes nothing. The wait neither grows nor
  starts again.
- **R4, open, once its wait is over,** the model is asked in its place again, one
  request at a time: from the moment a call begins at or after the wait's end until
  that call ends, the model is failing for everyone else, with no wait to name. The
  first call that ends failed at or after the wait's end opens it again, for the next
  wait of `WAITS`, and the last one from then on. A call that ends with nothing to say
  changes nothing but that it is no longer under way.
- **R5, an answer closes it,** from any open state. The calls kept start anew with that
  answer, and the next opening waits `WAITS[0]` again.
- **R6, calls end in any order,** whenever each ends. One that began as a request that
  tries the model, and ends after the breaker closed or opened again, leaves the count
  of those under way as it should be: never below nothing, and never held by a call
  that has ended.

Nothing here reads a clock: every method that needs the time is given it, `now`. Nothing
here knows what a model is, what a call ended as, or who is told: a breaker only counts.
"""

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

LAST_CALLS = 20
"""How many calls a breaker keeps, at the most: the last that ended answered or failed.
Enough that one failure among many answers doesn't open it, few enough that a model
which has begun to fail is found out within minutes of traffic."""

WITHIN = timedelta(minutes=30)
"""And none that ended longer ago than this. An installation of one person asks a model
every few minutes: a shorter time would keep too few calls for a breaker to open on,
and a model that failed every question of an hour would be asked first for all of it."""

AT_LEAST = 5
"""It never opens on fewer calls kept than this, half or more of them failed: a first
failure, or two of three, says too little of a model to have it asked last."""

WAITS = (
    timedelta(seconds=30),
    timedelta(minutes=1),
    timedelta(minutes=2),
    timedelta(minutes=4),
    timedelta(minutes=5),
)
"""How long a breaker stays open: the first time, and after each call that failed once
the wait before it was over; the last one for every time after it. A model that is too
busy for a moment is tried again soon, and one that stays away is tried every five
minutes."""

type Outcome = Literal["answered", "failed"] | None
"""How a call ended: answered, failed, or with nothing to say of the model, `None`."""


def _seconds(wait: timedelta) -> int:
    """A wait in whole seconds, rounded up, and 1 at the least: as a log says it."""
    return max(1, math.ceil(wait.total_seconds()))


@dataclass(frozen=True, slots=True)
class Failing:
    """What a breaker says of a model that is failing (`Breaker.failing`)."""

    wait: timedelta | None
    """How long until the model is asked in its place again. `None`: its wait is over,
    and a request tries it in its place now (R4)."""

    @property
    def seconds(self) -> int | None:
        """The wait in whole seconds, rounded up, and 1 at the least. `None` while a
        request tries it."""
        return None if self.wait is None else _seconds(self.wait)


@dataclass(frozen=True, slots=True)
class Opened:
    """The breaker opened (R2): `failed` of the last `calls` calls kept failed, and the
    model is failing for `wait`."""

    failed: int
    calls: int
    wait: timedelta

    @property
    def seconds(self) -> int:
        """The wait in whole seconds, rounded up, and 1 at the least."""
        return _seconds(self.wait)


@dataclass(frozen=True, slots=True)
class OpenedAgain:
    """The model failed once its wait was over, and is failing again, for `wait` (R4)."""

    wait: timedelta

    @property
    def seconds(self) -> int:
        """The wait in whole seconds, rounded up, and 1 at the least."""
        return _seconds(self.wait)


@dataclass(frozen=True, slots=True)
class Closed:
    """The model answered while its breaker was open, and is asked in its place again
    (R5)."""


type Change = Opened | OpenedAgain | Closed
"""What the end of a call changed of its breaker, for whoever keeps a log of it."""


class Call:
    """One try of a model, from the moment it began (`Breaker.begins`) until it ends
    (`ends`), which it does once."""

    __slots__ = ("_breaker", "_ended")

    def __init__(self, breaker: "Breaker") -> None:
        self._breaker = breaker
        self._ended = False

    def ends(self, outcome: Outcome, now: datetime) -> Change | None:
        """The call ended at `now`, `answered`, `failed`, or with nothing to say of the
        model (`None`): what that changed of the breaker, or `None` when nothing did.
        Raises `RuntimeError` for a call that has ended already."""
        if self._ended:
            raise RuntimeError("a call ends once")
        self._ended = True
        return self._breaker._end(self, outcome, now)


class Breaker:
    """One model's breaker: see the module. A new one is closed, and has kept nothing."""

    def __init__(self) -> None:
        self._kept: deque[tuple[datetime, bool]] = deque(maxlen=LAST_CALLS)
        """The calls kept while it is closed: when each ended, and whether it failed."""
        self._until: datetime | None = None
        """When its wait is over, while it is open. `None`: closed."""
        self._wait = 0
        """Which of `WAITS` it is open for."""
        self._trying: set[Call] = set()
        """The calls under way that began at or after its wait's end (R4)."""

    @property
    def trying(self) -> int:
        """How many calls under way began once its wait was over, and try the model in
        its place: 0 while it is closed, or before its wait is over."""
        return len(self._trying)

    def failing(self, now: datetime) -> Failing | None:
        """Whether the model is failing at `now`, and for how long. `None`: it is asked
        in its place, as it is when its breaker is closed, and once its wait is over
        while no request tries it."""
        if self._until is None:
            return None
        if now < self._until:
            return Failing(self._until - now)
        return Failing(None) if self._trying else None

    def begins(self, now: datetime) -> Call:
        """A call to the model begins at `now`. Its caller says how it ended, once, on
        every path: `Call.ends`."""
        call = Call(self)
        if self._until is not None and now >= self._until:
            self._trying.add(call)
        return call

    def _end(self, call: Call, outcome: Outcome, now: datetime) -> Change | None:
        self._trying.discard(call)
        if outcome is None:
            return None
        if self._until is None:
            return self._keep(failed=outcome == "failed", now=now)
        if outcome == "answered":
            self._until = None
            self._wait = 0
            self._kept = deque([(now, False)], maxlen=LAST_CALLS)
            self._trying.clear()
            return Closed()
        if now < self._until:
            return None
        self._wait = min(self._wait + 1, len(WAITS) - 1)
        return OpenedAgain(self._open(now))

    def _keep(self, *, failed: bool, now: datetime) -> Opened | None:
        """Keep a call that ended while the breaker is closed, and open it when the
        calls kept say so (R1, R2)."""
        self._kept.append((now, failed))
        self._kept = deque(
            ((at, failure) for at, failure in self._kept if now - at <= WITHIN),
            maxlen=LAST_CALLS,
        )
        if not failed:
            return None
        calls = len(self._kept)
        failures = sum(failure for _, failure in self._kept)
        # Half or more, in whole numbers.
        if calls < AT_LEAST or failures * 2 < calls:
            return None
        return Opened(failures, calls, self._open(now))

    def _open(self, now: datetime) -> timedelta:
        """Open it at `now` for its wait, forgetting those that tried it: how long it is
        open for. The calls kept count no more from here on: nothing reads them while it
        is open, and the answer that closes it starts them anew (`_end`)."""
        wait = WAITS[self._wait]
        self._until = now + wait
        self._trying.clear()
        return wait
