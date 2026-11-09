from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from actio_fakellm import cli


def run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> tuple[Any, dict[str, Any]]:
    """Run the CLI with `uvicorn.run` replaced, and return the app and options it got."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    cli.main(argv)

    [((app,), kwargs)] = calls
    return app, kwargs


def served_models(app: Any) -> list[str]:
    return [model["id"] for model in TestClient(app).get("/v1/models").json()["data"]]


def test_defaults_serve_the_default_model_on_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    app, kwargs = run_cli(monkeypatch, [])

    assert kwargs == {"host": "127.0.0.1", "port": 8100, "server_header": False}
    assert served_models(app) == ["fake-chat"]


def test_flags_choose_the_port_and_models(monkeypatch: pytest.MonkeyPatch) -> None:
    app, kwargs = run_cli(monkeypatch, ["--port", "9100", "--model", "a", "--model", "b"])

    assert kwargs["port"] == 9100
    assert served_models(app) == ["a", "b"]
