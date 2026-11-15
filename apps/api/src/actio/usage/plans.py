"""What using a model costs, and how much each kind of account may use.

Both are read from a TOML file, `config/plans.toml`:

- a **price** for each model on offer, in credits per million tokens;
- **plans:** how many credits a session and a week allow. Guests get one plan, and
  accounts another.

`load` reads and checks the file. A mistake raises `PlansError`, which says where the
file is and what is wrong in it. `Plans` is what the rest of Actio asks: which plan an
account has, what a model costs, and which windows a request has to fit in right now.

Nothing here counts anything, or reads a clock: the engine in `packages/quota` counts,
and is told the time.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from actio.core import config_file
from actio.core.config_file import Name
from actio_quota import Bucket, CalendarReset, Fixed, Rates, Rolling
from actio_quota.cost import MAX_CREDITS

type WindowName = Literal["session", "week"]

SESSION: WindowName = "session"
"""The window that opens with an account's first request and closes some hours later."""

WEEK: WindowName = "week"
"""The window that starts over at the same moment every week."""

WINDOWS: Mapping[str, WindowName] = {SESSION: SESSION, WEEK: WEEK}
"""The windows there are, by the names the engine keeps them under."""

MAX_PRICE = 100_000_000
"""The highest price accepted, in credits per million tokens: a hundred credits a
token. More than that is a slip of the hand."""

_HOUR_MS = 3_600_000


class PlansError(Exception):
    """The plans file is missing, unreadable, or says something that can't be right."""


# --- The file -------------------------------------------------------------------------


class PriceEntry(BaseModel):
    """One `[prices.<model>]` table: what a model costs, in credits per million tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: int = Field(ge=0, le=MAX_PRICE)
    """For what the model reads: the conversation so far."""
    output: int = Field(ge=0, le=MAX_PRICE)
    """For what it writes: its answer, and its reasoning."""
    cached_input: int | None = Field(default=None, ge=0, le=MAX_PRICE)
    """For what it reads again from its server's cache. Left out: the price of `input`."""

    @model_validator(mode="after")
    def _charges_no_more_for_what_was_cached(self) -> Self:
        if self.cached_input is not None and self.cached_input > self.input:
            raise ValueError(
                "cached_input must not be more than input: reading from the cache saves work"
            )
        return self


class PlanEntry(BaseModel):
    """One `[plans.<name>]` table: how many credits each window allows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    session_credits: int | None = Field(default=None, ge=0, le=MAX_CREDITS)
    """Left out: a session has no limit of its own."""
    week_credits: int | None = Field(default=None, ge=0, le=MAX_CREDITS)
    """Left out: a week has no limit."""

    @model_validator(mode="after")
    def _lets_a_session_reach_its_limit(self) -> Self:
        if (
            self.session_credits is not None
            and self.week_credits is not None
            and self.session_credits > self.week_credits
        ):
            raise ValueError(
                "session_credits must not be more than week_credits: "
                "the session's limit could never be reached"
            )
        return self


class PlansFile(BaseModel):
    """The whole file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    guests: Name
    """The plan of someone who hasn't signed up."""
    accounts: Name
    """The plan of an account."""
    session_hours: int = Field(ge=1, le=7 * 24)
    """How long a session lasts, from the first request that opens it."""
    week_resets: str
    """When the week starts over, as `MON 00:00 Europe/Istanbul`."""
    min_answer_tokens: int = Field(ge=1, le=100_000)
    """The shortest answer worth starting: a request that can't afford this many tokens
    is refused, instead of being answered in a handful of words."""
    prices: dict[Name, PriceEntry] = Field(min_length=1)
    plans: dict[Name, PlanEntry] = Field(min_length=1)

    @field_validator("week_resets")
    @classmethod
    def _names_a_moment_of_the_week(cls, value: str) -> str:
        try:
            reset = CalendarReset.parse(value)
        except ZoneInfoNotFoundError:
            raise ValueError(
                f"no time zone is called {value.split()[-1]!r}. "
                "Write one such as Europe/Istanbul, or UTC"
            ) from None
        except ValueError:
            raise ValueError("must look like MON 00:00 Europe/Istanbul") from None
        if reset.weekday is None:
            raise ValueError("needs a day of the week, as in MON 00:00 Europe/Istanbul")
        return value

    @model_validator(mode="after")
    def _names_plans_that_exist(self) -> Self:
        for setting, name in (("guests", self.guests), ("accounts", self.accounts)):
            if name not in self.plans:
                raise ValueError(f'{setting} = "{name}" is not one of the plans')
        return self


def read_file(path: Path) -> PlansFile:
    """Read and check the plans file. Raises `PlansError` naming the file and the fault."""
    return config_file.read(
        path,
        PlansFile,
        what="plans",
        elsewhere=(
            "Set ACTIO_PLANS_FILE to where it is; the repository has one at config/plans.toml"
        ),
        failure=PlansError,
    )


# --- What the rest of Actio uses ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Plan:
    """What one kind of account may use."""

    name: str
    title: str
    session_credits: int | None
    week_credits: int | None


class Plans:
    """The prices and the plans of an installation."""

    def __init__(self, file: PlansFile) -> None:
        self._plans = {
            name: Plan(name, entry.title, entry.session_credits, entry.week_credits)
            for name, entry in file.plans.items()
        }
        self._guests = self._plans[file.guests]
        self._accounts = self._plans[file.accounts]
        self._prices = {
            name: Rates(entry.input, entry.output, entry.cached_input)
            for name, entry in file.prices.items()
        }
        self._session_ms = file.session_hours * _HOUR_MS
        self._week = CalendarReset.parse(file.week_resets)
        self.min_answer_tokens = file.min_answer_tokens

    def plan_for(self, *, is_guest: bool) -> Plan:
        """The plan of a guest, or of an account."""
        return self._guests if is_guest else self._accounts

    def price_of(self, model: str) -> Rates:
        """What the model called `model` costs. Every model on offer has a price:
        `require_prices_for` saw to that when the application started."""
        try:
            return self._prices[model]
        except KeyError:
            raise PlansError(f'the model "{model}" has no price') from None

    def free_models(self) -> list[str]:
        """The models that cost nothing, which stay usable when a limit is reached."""
        return [
            name for name, price in self._prices.items() if price.input == 0 and price.output == 0
        ]

    def windows(self, plan: Plan, *, now_ms: int) -> list[Bucket]:
        """The windows a request under `plan` has to fit in at `now_ms`, as the engine
        takes them. A plan without limits has none."""
        windows = []
        if plan.session_credits is not None:
            windows.append(Bucket(SESSION, plan.session_credits, Rolling(self._session_ms)))
        if plan.week_credits is not None:
            week = Fixed(*self._week.window(now_ms))
            windows.append(Bucket(WEEK, plan.week_credits, week))
        return windows

    def require_prices_for(self, models: Iterable[str]) -> Self:
        """Check the prices against the models on offer. Every model needs a price, and
        a price for a model that isn't offered is most likely a misspelt name. Raises
        `PlansError`."""
        offered = list(models)
        for name in offered:
            if name not in self._prices:
                raise PlansError(
                    f'prices: the model "{name}" is on offer and has no price. Add '
                    f"[prices.{name}]; input = 0 and output = 0 make it free"
                )
        for name in self._prices:
            if name not in offered:
                raise PlansError(f'prices.{name}: no model on offer is called "{name}"')
        return self


def load(path: Path, *, models: Iterable[str]) -> Plans:
    """The plans in the file at `path`, for an installation that offers `models`.
    Raises `PlansError` naming the file and the fault."""
    plans = Plans(read_file(path))
    try:
        return plans.require_prices_for(models)
    except PlansError as error:
        raise PlansError(f"{path}: {error}") from None
