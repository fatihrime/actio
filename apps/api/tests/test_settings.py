from pathlib import Path

import pytest
from pydantic import ValidationError

from actio.settings import Settings


def test_defaults_listen_on_localhost_only() -> None:
    settings = Settings()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.forwarded_allow_ips == "127.0.0.1"


def test_reads_actio_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIO_PORT", "9000")
    monkeypatch.setenv("ACTIO_LOG_LEVEL", "debug")

    settings = Settings()

    assert settings.port == 9000
    assert settings.log_level == "debug"


def test_never_reads_dotenv_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("ACTIO_PORT=9999\n")
    monkeypatch.chdir(tmp_path)

    assert Settings().port == 8000


@pytest.mark.parametrize("port", [0, 65536])
def test_rejects_out_of_range_ports(port: int) -> None:
    with pytest.raises(ValidationError):
        Settings(port=port)
