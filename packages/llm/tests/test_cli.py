"""The `actio-llm` command, run against a stand-in for the Ollama server."""

import json
from typing import Any

import httpx2
import pytest

from actio_llm import cli

TAGS = {
    "models": [
        {
            "name": "qwen3.5:4b",
            "size": 3_970_000_000,
            "details": {"parameter_size": "4.7B", "quantization_level": "Q4_K_M"},
        }
    ]
}
SHOW = {
    "capabilities": ["completion", "tools", "thinking"],
    "model_info": {"qwen35.context_length": 262_144},
}
ANSWER = [
    {"message": {"role": "assistant", "content": "", "thinking": "Let me think."}},
    {"message": {"role": "assistant", "content": "Mer"}},
    {"message": {"role": "assistant", "content": "haba!"}},
    {
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 12,
        "prompt_eval_cached_count": 4,
        "eval_count": 9,
        "eval_duration": 300_000_000,
        "load_duration": 1_500_000_000,
    },
]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Point the CLI at a stand-in server; returns the chat requests it receives."""
    chats: list[dict[str, Any]] = []

    def route(request: httpx2.Request) -> httpx2.Response:
        match request.url.path:
            case "/api/version":
                return httpx2.Response(200, json={"version": "0.40.16"})
            case "/api/tags":
                return httpx2.Response(200, json=TAGS)
            case "/api/show":
                return httpx2.Response(200, json=SHOW)
            case _:
                chats.append(json.loads(request.content))
                body = "".join(json.dumps(chunk) + "\n" for chunk in ANSWER)
                return httpx2.Response(200, content=body.encode())

    monkeypatch.setattr(
        cli,
        "make_client",
        lambda url: httpx2.AsyncClient(base_url=url, transport=httpx2.MockTransport(route)),
    )
    return chats


@pytest.mark.usefixtures("server")
def test_models_lists_each_model_with_what_it_can_do(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["models"])

    assert capsys.readouterr().out == (
        "qwen3.5:4b  4.0 GB  4.7B Q4_K_M  context up to 262,144  [completion, thinking, tools]\n"
    )


def test_chat_streams_the_reasoning_then_the_answer_then_the_statistics(
    server: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(
        ["chat", "qwen3.5:4b", "Selam", "--system", "Be brief.", "--context", "8192", "--think"]
    )

    captured = capsys.readouterr()
    assert captured.out == "Let me think.\n\nMerhaba!\n"
    assert captured.err == (
        "[stop] · prompt 12 tokens (4 from cache) · answer 9 tokens"
        " · 30.0 tokens/s · model load 1.5 s\n"
    )
    [sent] = server
    assert sent["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Selam"},
    ]
    assert sent["options"] == {"num_ctx": 8192}
    assert sent["think"] is True


def test_chat_leaves_the_models_defaults_alone(server: list[dict[str, Any]]) -> None:
    cli.main(["chat", "qwen3.5:4b", "Selam"])

    [sent] = server
    assert "options" not in sent
    assert "think" not in sent


def test_a_failure_is_one_line_and_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        cli,
        "make_client",
        lambda url: httpx2.AsyncClient(base_url=url, transport=httpx2.MockTransport(refuse)),
    )
    with pytest.raises(SystemExit) as stopped:
        cli.main(["chat", "qwen3.5:4b", "Selam"])

    assert stopped.value.code == 1
    assert capsys.readouterr().err.startswith("actio-llm: can't reach Ollama")


def test_the_server_address_comes_from_the_flag_or_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []

    def record(url: str) -> httpx2.AsyncClient:
        urls.append(url)
        return httpx2.AsyncClient(
            base_url=url,
            transport=httpx2.MockTransport(lambda _request: httpx2.Response(200, json=TAGS | SHOW)),
        )

    monkeypatch.setattr(cli, "make_client", record)
    monkeypatch.delenv("ACTIO_OLLAMA_URL", raising=False)
    cli.main(["models"])
    monkeypatch.setenv("ACTIO_OLLAMA_URL", "http://gpu-box:11434")
    cli.main(["models"])
    cli.main(["--url", "http://other:11434", "models"])

    assert urls == ["http://127.0.0.1:11434", "http://gpu-box:11434", "http://other:11434"]


def test_a_context_window_below_one_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["chat", "m", "hi", "--context", "0"])

    assert stopped.value.code == 2
    assert "must be at least 1" in capsys.readouterr().err
