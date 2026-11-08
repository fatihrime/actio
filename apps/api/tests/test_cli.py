from typing import Any

import pytest
import uvicorn

from actio import cli


def test_api_role_serves_the_app_factory_with_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("ACTIO_PORT", "9000")

    cli.main(["--role", "api"])

    [(args, kwargs)] = calls
    assert args == ("actio.app:create_app",)
    assert kwargs["factory"] is True
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9000
    assert kwargs["server_header"] is False


def test_unknown_role_exits_with_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--role", "nope"])

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
