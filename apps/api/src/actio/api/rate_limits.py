"""Limits on what anyone can ask for: new guests, new accounts, password attempts.

A route counts an attempt before doing its work, in one line: `await limits.log_in(...)`.
With no room left, that raises a 429 problem with a `Retry-After` header, and the work
never starts: a refused login costs one question to Valkey, and no password hashing.

Attempts are counted per client address. Password attempts are also counted per email
address, wherever they come from, so that guessing one account's password from many
addresses is limited too. The rates are settings: see `Settings.limit_*`.

When Valkey doesn't answer, these routes answer 503 instead of going uncounted.
"""

import ipaddress
import logging
import math
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request

from actio.api.dependencies import AppResources, AppSettings, Now
from actio.api.problems import Problem
from actio.core.rate_limit import LimiterUnavailableError, RateLimiter, Take
from actio.core.settings import Settings
from actio.identity.accounts import normalize_email

logger = logging.getLogger(__name__)


def client_address(request: Request) -> str:
    """What a client's attempts are counted under: its network address.

    - An IPv4 address counts as itself.
    - An IPv6 address counts as its /64 network: one home or one machine is usually
      given a whole /64, and could otherwise use a new address for every attempt.
    - Anything that isn't an address shares one bucket, `unknown`.

    Behind the reverse proxy, the server takes the address from `X-Forwarded-For`, from
    the proxies named in `ACTIO_FORWARDED_ALLOW_IPS`.
    """
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:  # an IPv4 client, written the IPv6 way
            return str(address.ipv4_mapped)
        network = ipaddress.IPv6Address(int(address) >> 64 << 64)
        return f"{network}/64"
    return str(address)


class RateLimits:
    """The limits, for one request: its client address and its time are filled in."""

    def __init__(
        self, limiter: RateLimiter, settings: Settings, *, address: str, now: datetime
    ) -> None:
        self._limiter = limiter
        self._settings = settings
        self._address = address
        self._now = now

    async def new_guest(self) -> None:
        """Count a new guest account against the client's address."""
        await self._spend(
            "Too many new guests from your network.",
            Take("guests-address", self._address, self._settings.limit_guests_per_address),
        )

    async def sign_up(self) -> None:
        """Count a sign-up attempt against the client's address."""
        await self._spend(
            "Too many sign-up attempts from your network.",
            Take("signups-address", self._address, self._settings.limit_signups_per_address),
        )

    async def log_in(self, email: str) -> None:
        """Count a login attempt against the client's address and the email it names.

        The email counts whether or not an account has it, so the answer doesn't tell.
        """
        await self._spend(
            "Too many login attempts.",
            Take("logins-address", self._address, self._settings.limit_logins_per_address),
            self._password_attempt(email),
        )

    async def change_password(self, email: str) -> None:
        """Count an attempt at an account's current password. It shares the bucket of
        logins to that account: both are guesses at the same password."""
        await self._spend("Too many password attempts.", self._password_attempt(email))

    async def turn(self, user_id: uuid.UUID) -> None:
        """Count a question put to a model against the account that asks.

        Per account, not per address: a guest is an account too, and new guests are
        limited per address. What an answer may cost is for the usage limits to say;
        this only keeps one account from asking faster than anyone could read.
        """
        await self._spend(
            "Too many messages in a short time.",
            Take("turns-user", str(user_id), self._settings.limit_turns_per_user),
        )

    def _password_attempt(self, email: str) -> Take:
        return Take("logins-email", normalize_email(email), self._settings.limit_logins_per_email)

    async def _spend(self, what: str, *takes: Take) -> None:
        try:
            wait = await self._limiter.spend(*takes, now=self._now)
        except LimiterUnavailableError as error:
            logger.warning(
                "rate limits: Valkey is unreachable (%s)", type(error.__cause__).__name__
            )
            raise Problem(503, "This can't be done right now. Try again in a moment.") from None
        if wait:
            seconds = math.ceil(wait.total_seconds())
            raise Problem(
                429,
                f"{what} Try again in {_in_words(seconds)}.",
                headers={"Retry-After": str(seconds)},
                retry_after=seconds,
            )


def _in_words(seconds: int) -> str:
    if seconds == 1:
        return "1 second"
    if seconds < 120:
        return f"{seconds} seconds"
    minutes = math.ceil(seconds / 60)
    if minutes < 120:
        return f"{minutes} minutes"
    return f"{math.ceil(minutes / 60)} hours"


def get_rate_limits(
    request: Request, resources: AppResources, settings: AppSettings, now: Now
) -> RateLimits:
    return RateLimits(resources.limiter, settings, address=client_address(request), now=now)


Limits = Annotated[RateLimits, Depends(get_rate_limits)]
