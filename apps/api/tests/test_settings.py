from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from actio.core.rate_limit import Rate
from actio.core.settings import Settings

DATABASE = "postgresql://actio:s3cret-db@db.internal:5432/actio"
VALKEY = "valkey://:s3cret-kv@kv.internal:6379/0"


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two required settings, as the environment would give them."""
    monkeypatch.setenv("ACTIO_DATABASE_URL", DATABASE)
    monkeypatch.setenv("ACTIO_VALKEY_URL", VALKEY)


@pytest.mark.usefixtures("stores")
def test_defaults_listen_on_localhost_only() -> None:
    settings = Settings()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.forwarded_allow_ips == "127.0.0.1"


@pytest.mark.usefixtures("stores")
def test_reads_actio_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIO_PORT", "9000")
    monkeypatch.setenv("ACTIO_LOG_LEVEL", "debug")

    settings = Settings()

    assert settings.port == 9000
    assert settings.log_level == "debug"
    assert settings.database_url.get_secret_value() == DATABASE
    assert settings.valkey_url.get_secret_value() == VALKEY


@pytest.mark.usefixtures("stores")
def test_never_reads_dotenv_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("ACTIO_PORT=9999\n")
    monkeypatch.chdir(tmp_path)

    assert Settings().port == 8000


@pytest.mark.usefixtures("stores")
@pytest.mark.parametrize("port", [0, 65536])
def test_rejects_out_of_range_ports(port: int) -> None:
    with pytest.raises(ValidationError):
        Settings(port=port)


def test_the_database_and_valkey_are_required() -> None:
    """A process without them fails at startup, with the names of what is missing."""
    with pytest.raises(ValidationError) as failure:
        Settings()

    missing = {error["loc"][0] for error in failure.value.errors()}
    assert missing == {"database_url", "valkey_url"}


@pytest.mark.usefixtures("stores")
def test_passwords_never_show_when_settings_are_printed() -> None:
    settings = Settings()

    shown = f"{settings!r} {settings} {settings.model_dump()} {settings.model_dump_json()}"
    assert "s3cret" not in shown


@pytest.mark.usefixtures("stores")
def test_the_public_address_defaults_to_the_local_stack() -> None:
    assert Settings().public_url == "http://localhost:8080"


@pytest.mark.usefixtures("stores")
@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("https://chat.example.com", "https://chat.example.com"),
        ("https://chat.example.com/", "https://chat.example.com"),  # as browsers write an origin
        ("http://192.168.1.20:8080", "http://192.168.1.20:8080"),
    ],
)
def test_the_public_address_is_kept_as_an_origin(given: str, kept: str) -> None:
    assert Settings(public_url=given).public_url == kept


@pytest.mark.usefixtures("stores")
@pytest.mark.parametrize(
    "given", ["chat.example.com", "ftp://chat.example.com", "https://chat.example.com/app", ""]
)
def test_a_public_address_that_is_not_an_origin_is_refused(given: str) -> None:
    with pytest.raises(ValidationError, match="public_url"):
        Settings(public_url=given)


@pytest.mark.parametrize(
    ("field", "url"),
    [
        ("database_url", "mysql://root@localhost/actio"),
        ("database_url", "localhost:5432"),
        ("valkey_url", "http://localhost:6379"),
    ],
)
def test_rejects_urls_for_other_kinds_of_server(field: str, url: str) -> None:
    database = SecretStr(url if field == "database_url" else DATABASE)
    valkey = SecretStr(url if field == "valkey_url" else VALKEY)
    with pytest.raises(ValidationError, match=field) as failure:
        Settings(database_url=database, valkey_url=valkey)

    assert url not in str(failure.value)  # a rejected URL may hold a password too


@pytest.mark.usefixtures("stores")
def test_models_are_listed_in_the_repositorys_file_and_run_on_this_machines_ollama() -> None:
    settings = Settings()

    assert settings.models_file == Path("config/models.toml")
    assert settings.ollama_url == "http://127.0.0.1:11434"


@pytest.mark.usefixtures("stores")
def test_the_models_file_and_ollama_can_be_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIO_MODELS_FILE", "/app/config/models.toml")
    monkeypatch.setenv("ACTIO_OLLAMA_URL", "http://host.docker.internal:11434")

    settings = Settings()

    assert settings.models_file == Path("/app/config/models.toml")
    assert settings.ollama_url == "http://host.docker.internal:11434"


@pytest.mark.usefixtures("stores")
@pytest.mark.parametrize(
    "url", ["127.0.0.1:11434", "ollama", "ftp://ollama:11434", "http://ollama:11434/api", ""]
)
def test_ollamas_address_is_a_server_not_a_path_on_one(url: str) -> None:
    with pytest.raises(ValidationError, match="ollama_url"):
        Settings(ollama_url=url)


@pytest.mark.usefixtures("stores")
def test_the_rate_limits_have_defaults() -> None:
    settings = Settings()

    assert str(settings.limit_guests_per_address) == "30/hour"
    assert str(settings.limit_signups_per_address) == "10/hour"
    assert str(settings.limit_logins_per_address) == "30/hour"
    assert str(settings.limit_logins_per_email) == "10/hour"
    assert str(settings.limit_turns_per_user) == "120/hour"


def test_every_rate_limit_can_be_set_for_the_container_stack() -> None:
    """compose.yaml passes settings on by name, and .env.example is where people look
    for them. A limit added to the settings must be added to both."""
    root = Path(__file__).resolve().parents[3]
    compose, example = (root / "compose.yaml").read_text(), (root / ".env.example").read_text()
    limits = [name for name in Settings.model_fields if name.startswith("limit_")]

    assert len(limits) == 5
    for name in limits:
        default = Settings.model_fields[name].default
        assert f"ACTIO_{name.upper()}:" in compose
        assert f"# ACTIO_{name.upper()}={default}\n" in example  # with its real default


@pytest.mark.usefixtures("stores")
def test_a_rate_limit_is_read_from_the_environment_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIO_LIMIT_GUESTS_PER_ADDRESS", "3/day")

    settings = Settings()

    assert settings.limit_guests_per_address == Rate(count=3, period_ms=86_400_000)
    assert settings.limit_guests_per_address.interval_ms == 8 * 3_600_000


@pytest.mark.usefixtures("stores")
@pytest.mark.parametrize("written", ["lots", "0/hour", "30 per hour", "30/fortnight", ""])
def test_a_rate_limit_written_wrongly_stops_the_process_at_startup(
    monkeypatch: pytest.MonkeyPatch, written: str
) -> None:
    monkeypatch.setenv("ACTIO_LIMIT_LOGINS_PER_EMAIL", written)

    with pytest.raises(ValidationError, match="must look like 30/hour") as failure:
        Settings()

    assert [error["loc"] for error in failure.value.errors()] == [("limit_logins_per_email",)]


@pytest.mark.usefixtures("stores")
def test_valkey_keys_start_with_a_prefix_that_can_be_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings().valkey_prefix == "actio"

    monkeypatch.setenv("ACTIO_VALKEY_PREFIX", "actio-staging")
    assert Settings().valkey_prefix == "actio-staging"


@pytest.mark.usefixtures("stores")
@pytest.mark.parametrize("prefix", ["", "Actio", "two words", "a:b", "{tag}", "x" * 33])
def test_a_prefix_that_would_make_odd_keys_is_refused(prefix: str) -> None:
    with pytest.raises(ValidationError, match="valkey_prefix"):
        Settings(valkey_prefix=prefix)
