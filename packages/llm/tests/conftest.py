"""Shared fixtures: recorded Ollama exchanges, replayed without a server.

Each cassette in `fixtures/ollama/` holds one real exchange with an Ollama server: the
request the adapter sent, and the bytes that came back. By default the tests replay
them, and also check that the adapter still sends exactly the recorded request.

- `ACTIO_TEST_OLLAMA_URL=http://127.0.0.1:11434` sends the same requests to a real
  server instead (`make test-live`). The server needs the tests' model, granite4:micro.
- With `ACTIO_RECORD_OLLAMA=1` as well, the server's answers become the new cassettes.
"""

import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from actio_llm import Ollama, Timeouts

LIVE_URL = os.environ.get("ACTIO_TEST_OLLAMA_URL")
RECORD = os.environ.get("ACTIO_RECORD_OLLAMA") == "1"
FIXTURES = Path(__file__).parent / "fixtures" / "ollama"

# `/api/show` also returns the license text, the prompt template and a Modelfile that
# names a path on the recording machine. The adapter reads none of them.
SHOW_KEYS = ("capabilities", "details", "model_info", "thinking")


def tidy(path: str, body: str) -> str:
    """What is saved of a model listing: the fields the adapter reads, and no file paths."""
    if path not in ("/api/show", "/api/tags"):
        return body
    data = json.loads(body)
    if path == "/api/show":
        data = {key: value for key, value in data.items() if key in SHOW_KEYS}
    for entry in [data, *data.get("models", [])]:
        entry.get("details", {}).pop("parent_model", None)
    return json.dumps(data)


class Tape(httpx2.AsyncBaseTransport):
    """Answers one request from a cassette, or passes it to a real server."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._path = FIXTURES / f"{name}.json"
        self._server = httpx2.AsyncHTTPTransport() if LIVE_URL else None
        self._exchange: dict[str, Any] | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/api/version" and self.name != "version":
            # The adapter's version check, not the exchange under test.
            if self._server is not None:
                return await self._server.handle_async_request(request)
            recorded = json.loads((FIXTURES / "version.json").read_text())
            return httpx2.Response(200, content=recorded["body"].encode())

        sent = json.loads(request.content) if request.content else None
        if self._server is not None:
            response = await self._server.handle_async_request(request)
            body = (await response.aread()).decode()
            await response.aclose()
            if response.status_code == 200:
                body = tidy(path, body)
            self._exchange = {
                "method": request.method,
                "path": path,
                "request": sent,
                "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "body": body,
            }
            if RECORD:
                text = json.dumps(self._exchange, indent=2, ensure_ascii=False)
                self._path.write_text(text + "\n")
        else:
            self._exchange = json.loads(self._path.read_text())
            assert (request.method, path) == (self._exchange["method"], self._exchange["path"])
            assert sent == self._exchange["request"], (
                f"the adapter no longer sends the request recorded in {self._path.name}"
            )
        return httpx2.Response(
            self._exchange["status"],
            headers={"content-type": self._exchange["content_type"]},
            content=self._exchange["body"].encode(),
        )

    async def aclose(self) -> None:
        if self._server is not None:
            await self._server.aclose()

    @property
    def chunks(self) -> list[dict[str, Any]]:
        """The answer's lines as JSON, parsed here independently of the adapter."""
        assert self._exchange is not None, "nothing was requested yet"
        return [json.loads(line) for line in self._exchange["body"].splitlines() if line]


type Start = Callable[..., tuple[Ollama, Tape]]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def ollama_with() -> AsyncIterator[Start]:
    """Call with a cassette's name to get an adapter that plays it, and the tape."""
    clients: list[httpx2.AsyncClient] = []

    def start(name: str, *, timeouts: Timeouts | None = None) -> tuple[Ollama, Tape]:
        tape = Tape(name)
        client = httpx2.AsyncClient(base_url=LIVE_URL or "http://ollama.test", transport=tape)
        clients.append(client)
        return Ollama(client, timeouts=timeouts), tape

    yield start
    for client in clients:
        await client.aclose()


@pytest.fixture
def live_url() -> str:
    """The real server's address; skips the test when none is given."""
    if LIVE_URL is None:
        pytest.skip("needs a real Ollama server: set ACTIO_TEST_OLLAMA_URL (make test-live)")
    return LIVE_URL
