"""Usage limits for one request: credits held before it runs, and counted when it ends.

A request that costs credits goes through three moments:

1. **`reserve`**, before it runs. Its largest possible cost is held in every window of
   the account's plan, or the request is refused: `LimitReachedError`. Near a limit it
   is given less room for its answer instead.
2. **`record`**, when it has ended: what it really used is written to the ledger, in
   the transaction that records the request's own outcome.
3. **`settle`**, after that transaction is committed: the counters give back what was
   held and count what was used. A request that used nothing is `release`d instead.

The ledger comes first on purpose. The counters live in Valkey, which keeps nothing on
disk: when they are missing, they are rebuilt from the ledger, so nothing may reach the
counters that the ledger doesn't hold.

The counting is the engine's (`actio_quota`): one Lua script per operation, atomic
across every process. Time comes in as `now`, so tests can move it.
"""

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from valkey.exceptions import ValkeyError

from actio.usage import ledger
from actio.usage.plans import WINDOWS, Plan, Plans, WindowName
from actio_quota import (
    Bucket,
    Denied,
    Estimate,
    Fixed,
    NeedsRebuild,
    Rates,
    ReserveResult,
    Rolling,
    Usage,
    UsageWarning,
    cost_credits,
)
from actio_quota.engine import ValkeyQuota

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


def to_ms(moment: datetime) -> int:
    """A moment as the engine counts time: whole milliseconds since 1970."""
    return (moment - _EPOCH) // _MILLISECOND


def to_moment(ms: int) -> datetime:
    return _EPOCH + ms * _MILLISECOND


def held_for_prompt(prompt_tokens: int) -> int:
    """How many tokens to hold credits for, given a guess at a prompt's size: a quarter
    more than the guess, rounded up.

    The guess is made before any model has counted the prompt, and it can come out
    short: down to 0.66 of a real count was measured (`actio_llm.estimate`). A request
    whose prompt is larger than what was held uses more than was reserved for it. The
    margin makes that rare, and settling with the provider's own count makes it right
    afterwards.
    """
    return prompt_tokens + -(-prompt_tokens // 4)


class LimitReachedError(Exception):
    """The request doesn't fit in what the account's plan has left."""

    def __init__(
        self, window: WindowName, resets_at: datetime | None, alternatives: list[str]
    ) -> None:
        super().__init__(window)
        self.window = window
        """The window that is full. When both are, the one that starts over last."""
        self.resets_at = resets_at
        """When that window starts over. `None`: the request is larger than the plan's
        whole allowance, so waiting doesn't help."""
        self.alternatives = alternatives
        """The models that cost nothing, which can answer all the same."""


class UsageUnavailableError(Exception):
    """Valkey didn't answer, so nothing could be held or read."""


@dataclass(frozen=True, slots=True)
class Account:
    """Whose usage it is, and which plan that makes it."""

    user_id: uuid.UUID
    is_guest: bool


@dataclass(frozen=True, slots=True)
class Reserved:
    """An admitted request, and the credits held for it until it is settled or released."""

    id: uuid.UUID
    user_id: uuid.UUID
    model: str
    price: Rates
    prompt_tokens: int
    """The guess at the prompt's size that was made before the request ran, without the
    margin. It is what gets counted when the provider never reports a count."""
    max_output_tokens: int
    """How long the answer may be. Pass it on to the model."""
    capped: bool
    """Whether that is less than was asked for, because a limit is near."""
    windows: Mapping[str, int]
    """The windows that admitted the request, each with the moment it opened."""


@dataclass(frozen=True, slots=True)
class Window:
    """One of an account's windows, as it stands."""

    name: WindowName
    limit: int
    used: int
    reserved: int
    """Credits held for requests that are running."""
    resets_at: datetime | None
    """When it starts over. `None` for a session that isn't open: the next request
    that costs something opens it."""


@dataclass(frozen=True, slots=True)
class Status:
    plan: Plan
    windows: list[Window]


class UsageLimits:
    """The usage limits of every account, on one Valkey database and one ledger."""

    def __init__(self, engine: ValkeyQuota, plans: Plans) -> None:
        self._engine = engine
        self.plans = plans

    async def reserve(
        self,
        db: AsyncSession,
        account: Account,
        reservation_id: uuid.UUID,
        *,
        model: str,
        prompt_tokens: int,
        max_output_tokens: int,
        hold_for: timedelta,
        now: datetime,
    ) -> Reserved:
        """Hold the credits a request may cost, or refuse it.

        `prompt_tokens` is a guess at the prompt's size. The answer gets the most
        tokens that still fit, up to `max_output_tokens` and down to the plan file's
        `min_answer_tokens`; with less room than that, the request is refused.

        The hold lasts `hold_for`, unless the request is settled or released sooner:
        whatever happens to the process, the credits come back.

        `db` is only read, and only when the account's counters are missing and have to
        be rebuilt from the ledger. Raises `LimitReachedError`, or
        `UsageUnavailableError` when Valkey doesn't answer.
        """
        price = self.plans.price_of(model)
        now_ms = to_ms(now)
        windows = self.plans.windows(self.plans.plan_for(is_guest=account.is_guest), now_ms=now_ms)
        estimate = Estimate.tokens(
            price,
            input_tokens=held_for_prompt(prompt_tokens),
            max_output=max_output_tokens,
            min_output=self.plans.min_answer_tokens,
        )
        user = str(account.user_id)

        async def ask() -> ReserveResult:
            return await self._engine.reserve(
                user,
                str(reservation_id),
                windows,
                estimate,
                ttl_ms=hold_for // _MILLISECOND,
                now_ms=now_ms,
            )

        try:
            result = await ask()
            if isinstance(result, NeedsRebuild):
                await self._rebuild(db, account.user_id, windows, now_ms)
                result = await ask()
            if isinstance(result, Denied):
                raise await self._refusal(user, windows, result, model, now_ms)
        except (ValkeyError, OSError) as error:
            raise UsageUnavailableError from error
        if isinstance(result, NeedsRebuild):
            # The counters went missing twice within one request: Valkey is restarting.
            raise UsageUnavailableError
        return Reserved(
            id=reservation_id,
            user_id=account.user_id,
            model=model,
            price=price,
            prompt_tokens=prompt_tokens,
            max_output_tokens=result.units,
            capped=result.units < max_output_tokens,
            windows=dict(result.windows),
        )

    def record(
        self,
        db: AsyncSession,
        reserved: Reserved,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        estimated: bool,
        now: datetime,
    ) -> int:
        """Write what a request used to the ledger, and return what that costs.

        The row is added to `db`'s transaction, which the caller commits together with
        the request's own outcome. Then, and only then, `settle` the same credits.
        """
        # A provider's counts are someone else's numbers: more cached than read at all
        # would be nonsense, and must not stop a request from being recorded.
        tokens = Usage(input_tokens, output_tokens, min(cached_input_tokens, input_tokens))
        credits = cost_credits(tokens, reserved.price)
        ledger.add(
            db,
            reservation_id=reserved.id,
            user_id=reserved.user_id,
            model=reserved.model,
            credits=credits,
            tokens=tokens,
            estimated=estimated,
            windows=reserved.windows,
            now=now,
        )
        return credits

    async def settle(
        self, reserved: Reserved, credits: int, *, now: datetime
    ) -> tuple[UsageWarning, ...]:
        """Count what a request cost, and give back what was held for it.

        Returns the thresholds its windows crossed, each announced once per window. Call
        it after the ledger row is committed. It doesn't raise: when Valkey is away, the
        hold runs out by itself, and the usage is in the ledger for the next rebuild.
        """
        try:
            finished = await self._engine.settle(
                str(reserved.user_id), str(reserved.id), credits, now_ms=to_ms(now)
            )
        except (ValkeyError, OSError) as error:
            logger.warning(
                "usage: reservation %s is in the ledger with %d credits, and couldn't be "
                "settled in Valkey (%s)",
                reserved.id,
                credits,
                type(error).__name__,
            )
            return ()
        if not finished.applied:
            # The counters were lost and rebuilt while the request ran. If that was
            # after its ledger row was written, the rebuild has counted it already.
            logger.info(
                "usage: reservation %s was no longer held when it was settled (%s)",
                reserved.id,
                finished.outcome,
            )
        return finished.warnings

    async def release(self, reserved: Reserved, *, now: datetime) -> None:
        """Give back what was held for a request that used nothing. Doesn't raise: a
        hold that can't be given back runs out by itself."""
        try:
            await self._engine.release(str(reserved.user_id), str(reserved.id), now_ms=to_ms(now))
        except (ValkeyError, OSError) as error:
            logger.warning(
                "usage: reservation %s couldn't be released in Valkey (%s)",
                reserved.id,
                type(error).__name__,
            )

    async def status(self, db: AsyncSession, account: Account, *, now: datetime) -> Status:
        """An account's plan, and how full its windows are right now.

        `db` is read as in `reserve`. Raises `UsageUnavailableError`.
        """
        plan = self.plans.plan_for(is_guest=account.is_guest)
        now_ms = to_ms(now)
        windows = self.plans.windows(plan, now_ms=now_ms)
        user = str(account.user_id)
        try:
            views = await self._engine.snapshot(user, windows, now_ms=now_ms)
            if views is None:
                await self._rebuild(db, account.user_id, windows, now_ms)
                views = await self._engine.snapshot(user, windows, now_ms=now_ms)
        except (ValkeyError, OSError) as error:
            raise UsageUnavailableError from error
        if views is None:
            raise UsageUnavailableError
        return Status(
            plan,
            [
                Window(
                    name=WINDOWS[view.name],
                    limit=view.limit,
                    used=view.used,
                    reserved=view.reserved,
                    resets_at=None if view.reset_ms is None else to_moment(view.reset_ms),
                )
                for view in views
            ],
        )

    async def _rebuild(
        self, db: AsyncSession, user_id: uuid.UUID, windows: Sequence[Bucket], now_ms: int
    ) -> None:
        """Give an account its counters back, from what the ledger says it used in the
        windows that are still open. A new account gets empty ones the same way."""
        opened = [_opened_no_earlier_than(window, now_ms) for window in windows]
        used = await ledger.since(db, user_id, to_moment(min(opened))) if opened else []
        # `if_epoch=None`: only if the counters are still missing. When another request
        # rebuilt them in the meantime, this one changes nothing.
        await self._engine.rebuild(str(user_id), windows, used, now_ms=now_ms, if_epoch=None)

    async def _refusal(
        self, user: str, windows: Sequence[Bucket], denied: Denied, model: str, now_ms: int
    ) -> LimitReachedError:
        alternatives = [name for name in self.plans.free_models() if name != model]
        if denied.retry_at_ms is None:
            return LimitReachedError(WINDOWS[denied.buckets[0]], None, alternatives)
        # Which window to name: of those that are full, the one that starts over last.
        views = await self._engine.snapshot(user, windows, now_ms=now_ms) or ()
        resets = {
            view.name: view.reset_ms
            for view in views
            if view.name in denied.buckets and view.reset_ms is not None
        }
        last = max(resets, key=lambda name: resets[name], default=denied.buckets[-1])
        return LimitReachedError(WINDOWS[last], to_moment(denied.retry_at_ms), alternatives)


def _opened_no_earlier_than(window: Bucket, now_ms: int) -> int:
    """The earliest moment at which a window that is open now can have opened."""
    match window.window:
        case Fixed(start_ms=start):
            return start
        case Rolling(duration_ms=duration):
            return now_ms - duration
