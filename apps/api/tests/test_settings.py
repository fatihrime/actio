from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

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
