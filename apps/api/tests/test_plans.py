"""The plans file: reading it, and what the rest of Actio asks of it."""

import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from actio import catalog
from actio.usage import plans
from actio.usage.plans import Plans, PlansError, PlansFile, read_file
from actio_quota import Bucket, Fixed, Rates, Rolling

ROOT = Path(__file__).resolve().parents[3]
HOUR_MS = 3_600_000

GOOD = """
guests = "guest"
accounts = "free"
session_hours = 5
week_resets = "MON 00:00 Europe/Istanbul"
min_answer_tokens = 256

[prices.standard]
input = 250_000
output = 1_000_000
cached_input = 25_000

[prices.local]
input = 0
output = 0

[plans.guest]
title = "Guest"
session_credits = 25_000
week_credits = 100_000

[plans.free]
title = "Free"
session_credits = 100_000
week_credits = 600_000

[plans.staff]
title = "Staff"
"""


def written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "plans.toml"
    path.write_text(text)
    return path


def parsed(text: str) -> Plans:
    return Plans(PlansFile.model_validate(tomllib.loads(text)))


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


# --- The file -------------------------------------------------------------------------


@pytest.mark.parametrize("folder", ["config", "deploy/e2e/config"], ids=["make up", "make e2e"])
def test_the_files_in_the_repository_price_the_models_beside_them(folder: str) -> None:
    """What `make up` and `make dev` start with, and what `make e2e` starts with: in
    each folder, a models file and the plans file that goes with it."""
    offered = catalog.read_file(ROOT / folder / "models.toml").models

    loaded = plans.load(ROOT / folder / "plans.toml", models=offered)

    assert loaded.plan_for(is_guest=True) != loaded.plan_for(is_guest=False)
    assert loaded.price_of("standard").output > 0


def test_the_end_to_end_stack_has_a_limit_within_reach_and_a_model_that_costs_nothing() -> None:
    """What its tests of the usage limits count on (apps/web/e2e/usage.spec.ts): an
    account reaches its session's limit in a few questions, a guest never does, and
    one model is free to carry on with."""
    folder = ROOT / "deploy/e2e/config"
    offered = catalog.read_file(folder / "models.toml").models

    loaded = plans.load(folder / "plans.toml", models=offered)

    assert loaded.free_models() == ["local"]
    assert loaded.plan_for(is_guest=False).session_credits == 500
    assert loaded.plan_for(is_guest=True).session_credits == 100_000


def test_a_file_says_what_models_cost_and_what_each_plan_allows(tmp_path: Path) -> None:
    file = read_file(written(tmp_path, GOOD))

    assert (file.guests, file.accounts) == ("guest", "free")
    assert list(file.prices) == ["standard", "local"]
    assert file.prices["standard"].cached_input == 25_000
    assert file.prices["local"].cached_input is None
    assert file.plans["free"].week_credits == 600_000
    assert file.plans["staff"].session_credits is None


def test_a_missing_file_says_where_it_looked_and_how_to_point_elsewhere(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "plans.toml"

    with pytest.raises(PlansError) as failure:
        read_file(missing)

    assert str(failure.value).startswith(f"the plans file '{missing}' doesn't exist.")
    assert "ACTIO_PLANS_FILE" in str(failure.value)


# Each mistake is the good file with one piece replaced, and what the error must mention.
MISTAKES = {
    "a plan for guests that isn't there": (
        'guests = "guest"',
        'guests = "visitor"',
        'guests = "visitor" is not one of the plans',
    ),
    "a plan for accounts that isn't there": (
        'accounts = "free"',
        'accounts = "pro"',
        'accounts = "pro" is not one of the plans',
    ),
    "a session longer than a week": ("session_hours = 5", "session_hours = 200", "session_hours"),
    "a week that starts over on no day": (
        '"MON 00:00 Europe/Istanbul"',
        '"00:00 Europe/Istanbul"',
        "week_resets: needs a day of the week",
    ),
    "a time zone nobody has": (
        "Europe/Istanbul",
        "Europe/Atlantis",
        "week_resets: no time zone is called 'Europe/Atlantis'",
    ),
    "a moment written loosely": (
        '"MON 00:00 Europe/Istanbul"',
        '"Monday at midnight"',
        "week_resets: must look like MON 00:00 Europe/Istanbul",
    ),
    "a price below nothing": ("input = 250_000", "input = -1", "prices.standard.input"),
    "a price nobody means": ("output = 1_000_000", "output = 1_000_000_000", "prices.standard"),
    "a cache that costs more than reading anew": (
        "cached_input = 25_000",
        "cached_input = 300_000",
        "prices.standard: cached_input must not be more than input",
    ),
    "a session that allows more than its week": (
        "session_credits = 25_000",
        "session_credits = 200_000",
        "plans.guest: session_credits must not be more than week_credits",
    ),
    "a misspelt setting": ("week_credits = 100_000", "weak_credits = 100_000", "weak_credits"),
    "credits written as a word": (
        "session_credits = 100_000",
        'session_credits = "plenty"',
        "plans.free.session_credits",
    ),
    "a name with a capital letter": ("[plans.staff]", "[plans.Staff]", "plans.Staff"),
    "a plan without a title": ('title = "Staff"', "", "plans.staff.title"),
    "an answer of no tokens": ("min_answer_tokens = 256", "min_answer_tokens = 0", "min_answer"),
    "the same plan twice": ("[plans.staff]", "[plans.free]", "valid TOML"),
    "a line that stops half-way": ("session_hours = 5", "session_hours = ", "valid TOML"),
}


@pytest.mark.parametrize("mistake", MISTAKES)
def test_a_mistake_in_the_file_is_named(tmp_path: Path, mistake: str) -> None:
    right, wrong, fault = MISTAKES[mistake]
    assert right in GOOD
    path = written(tmp_path, GOOD.replace(right, wrong, 1))

    with pytest.raises(PlansError) as failure:
        read_file(path)

    assert str(path) in str(failure.value)
    assert fault in str(failure.value)


@pytest.mark.parametrize("missing", ["[prices.standard]", "[plans.guest]"])
def test_a_file_without_prices_or_without_plans_says_so(tmp_path: Path, missing: str) -> None:
    path = written(tmp_path, GOOD.split(missing)[0])

    with pytest.raises(PlansError, match=missing[1:-1].split(".")[0]):
        read_file(path)


# --- Prices, checked against the models on offer --------------------------------------


def test_every_model_on_offer_needs_a_price(tmp_path: Path) -> None:
    path = written(tmp_path, GOOD)

    with pytest.raises(PlansError) as failure:
        plans.load(path, models=["standard", "local", "smart"])

    assert str(failure.value) == (
        f'{path}: prices: the model "smart" is on offer and has no price. '
        "Add [prices.smart]; input = 0 and output = 0 make it free"
    )


def test_a_price_for_a_model_that_is_not_on_offer_is_taken_for_a_misspelt_name(
    tmp_path: Path,
) -> None:
    path = written(tmp_path, GOOD)

    with pytest.raises(PlansError) as failure:
        plans.load(path, models=["standard"])

    assert str(failure.value) == f'{path}: prices.local: no model on offer is called "local"'


def test_a_file_that_prices_exactly_the_models_on_offer_loads(tmp_path: Path) -> None:
    loaded = plans.load(written(tmp_path, GOOD), models=["local", "standard"])

    assert loaded.price_of("standard") == Rates(250_000, 1_000_000, 25_000)
    assert loaded.price_of("local") == Rates(0, 0)
    assert loaded.min_answer_tokens == 256


def test_only_a_model_that_costs_nothing_to_read_and_to_write_is_free() -> None:
    reads_for_free = GOOD.replace("input = 250_000", "input = 0").replace(
        "cached_input = 25_000\n", ""
    )

    assert parsed(GOOD).free_models() == ["local"]
    assert parsed(reads_for_free).free_models() == ["local"]


def test_asking_the_price_of_a_model_without_one_is_an_error_not_a_free_ride() -> None:
    with pytest.raises(PlansError, match='"smart" has no price'):
        parsed(GOOD).price_of("smart")


# --- Plans and their windows ----------------------------------------------------------


def test_a_guest_has_one_plan_and_an_account_another() -> None:
    loaded = parsed(GOOD)

    guest, account = loaded.plan_for(is_guest=True), loaded.plan_for(is_guest=False)

    assert (guest.name, guest.title, guest.session_credits) == ("guest", "Guest", 25_000)
    assert (account.name, account.title, account.week_credits) == ("free", "Free", 600_000)


def test_a_plan_has_a_session_that_opens_when_used_and_a_week_by_the_calendar() -> None:
    loaded = parsed(GOOD)
    tuesday_noon = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    # Monday 00:00 in Istanbul is Sunday 21:00 in UTC: Istanbul is three hours ahead.
    week = Fixed(
        ms(datetime(2026, 11, 8, 21, 0, tzinfo=UTC)), ms(datetime(2026, 11, 15, 21, 0, tzinfo=UTC))
    )

    windows = loaded.windows(loaded.plan_for(is_guest=True), now_ms=ms(tuesday_noon))

    assert windows == [
        Bucket("session", 25_000, Rolling(5 * HOUR_MS)),
        Bucket("week", 100_000, week),
    ]


def test_the_week_starts_over_at_the_moment_the_file_names() -> None:
    loaded = parsed(GOOD)
    guest = loaded.plan_for(is_guest=True)
    reset = datetime(2026, 11, 15, 21, 0, tzinfo=UTC)

    [_, before] = loaded.windows(guest, now_ms=ms(reset) - 1)
    [_, after] = loaded.windows(guest, now_ms=ms(reset))

    assert isinstance(before.window, Fixed)
    assert isinstance(after.window, Fixed)
    assert before.window.end_ms == after.window.start_ms == ms(reset)


def test_a_plan_limits_only_the_windows_it_names() -> None:
    weekly_only = GOOD.replace("session_credits = 100_000\n", "")
    loaded = parsed(weekly_only.replace('accounts = "free"', 'accounts = "staff"'))
    now = ms(datetime(2026, 11, 10, 12, 0, tzinfo=UTC))

    assert loaded.windows(loaded.plan_for(is_guest=False), now_ms=now) == []  # staff: no limits
    [week] = parsed(weekly_only).windows(parsed(weekly_only).plan_for(is_guest=False), now_ms=now)
    assert (week.name, week.limit) == ("week", 600_000)
