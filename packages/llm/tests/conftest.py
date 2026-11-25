"""Shared fixtures: recorded exchanges with real servers, replayed without them.

Each cassette holds one real exchange: the request the adapter sent, and what came
back. By default the tests replay them, and also check that the adapter still sends
exactly the recorded request. They have three homes in `fixtures/`:

- `ollama/`, an Ollama server's. `ACTIO_TEST_OLLAMA_URL=http://127.0.0.1:11434` sends
  the same requests to a real server instead (`make test-live`). The server needs the
  tests' model, granite4:micro. With `ACTIO_RECORD_OLLAMA=1` as well, the server's
  answers become the new cassettes.
- `groq/` and `gemini/`, the cloud providers' OpenAI-compatible layers.
  `ACTIO_TEST_CLOUD=1` sends the same requests to the real servers instead, with the
  keys in `GROQ_API_KEY` and `GEMINI_API_KEY` (`make test-cloud`): a provider without a
  key has its tests skipped. With `ACTIO_RECORD_CLOUD=1` as well, the servers' answers
  become the new cassettes, tidied as they are written (`tidy_cloud`).
"""

import json
import os
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest

from actio_llm import Ollama, OpenAICompatible, Timeouts

LIVE_URL = os.environ.get("ACTIO_TEST_OLLAMA_URL")
RECORD = os.environ.get("ACTIO_RECORD_OLLAMA") == "1"
CLOUD = os.environ.get("ACTIO_TEST_CLOUD") == "1"
RECORD_CLOUD = os.environ.get("ACTIO_RECORD_CLOUD") == "1"
FIXTURES = Path(__file__).parent / "fixtures"

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


type Home = Literal["ollama", "groq", "gemini"]


@dataclass(frozen=True, slots=True)
class Cloud:
    """A cloud provider whose cassettes are kept here."""

    url: str
    """Where its OpenAI-compatible API starts."""
    key_variable: str
    """The environment variable that holds its key, live."""


CLOUDS: dict[Home, Cloud] = {
    "groq": Cloud("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "gemini": Cloud("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
}

# What a cloud provider's answer holds that names an account or one request of it, each
# with the fixed stand-in a cassette holds in its place. The cassettes are public.
STAND_INS = {
    # The account's organization, which Groq names in its refusals.
    re.compile(r"\borg_[A-Za-z0-9]+"): "org_00000000000000000000000000",
    # Groq's id of a request, and the id of a chat completion.
    re.compile(r"\breq_[A-Za-z0-9]+"): "req_00000000000000000000000000",
    re.compile(r"\bchatcmpl-[A-Za-z0-9-]+"): "chatcmpl-00000000-0000-0000-0000-000000000000",
}
SIGNATURE = "c2lnbmF0dXJl"
"""What a cassette holds in place of a thought signature: "signature", in base64. The
Gemini API's own are some hundreds of characters, which only it can read."""
SIGNED = re.compile(r'("thought_signature"\s*:\s*)"[^"]*"')
KEPT_HEADERS = ("retry-after",)
"""The headers of a cloud provider's answer that a cassette keeps, besides its type. No
header of the request is kept: one of them is the key."""


def tidy_cloud(path: str, status: int, body: str) -> str:
    """What is saved of a cloud provider's answer: of a list of models, only what the
    client reads, each model's id; of anything else, the body with each id above, and
    each thought signature, replaced by its stand-in."""
    if path.endswith("/models") and status == 200:
        data = json.loads(body)
        return json.dumps({"data": [{"id": entry["id"]} for entry in data["data"]]})
    for pattern, stand_in in STAND_INS.items():
        body = pattern.sub(stand_in, body)
    return SIGNED.sub(rf'\1"{SIGNATURE}"', body)


class Tape(httpx2.AsyncBaseTransport):
    """Answers one request from a cassette, or passes it to a real server."""

    def __init__(self, name: str, home: Home = "ollama") -> None:
        self.name = name
        self._home = home
        self._path = FIXTURES / home / f"{name}.json"
        live = bool(LIVE_URL) if home == "ollama" else CLOUD
        self._record = RECORD if home == "ollama" else RECORD_CLOUD
        self._server = httpx2.AsyncHTTPTransport() if live else None
        self._exchange: dict[str, Any] | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if self._home == "ollama" and path == "/api/version" and self.name != "version":
            # The adapter's version check, not the exchange under test.
            if self._server is not None:
                return await self._server.handle_async_request(request)
            recorded = json.loads((FIXTURES / "ollama" / "version.json").read_text())
            return httpx2.Response(200, content=recorded["body"].encode())

        sent = json.loads(request.content) if request.content else None
        if self._server is not None:
            response = await self._server.handle_async_request(request)
            body = (await response.aread()).decode()
            await response.aclose()
            self._exchange = {
                "method": request.method,
                "path": path,
                "request": sent,
                "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
            }
            if self._home == "ollama":
                self._exchange["body"] = tidy(path, body) if response.status_code == 200 else body
            else:
                self._exchange["body"] = tidy_cloud(path, response.status_code, body)
                if kept := {
                    name: response.headers[name]
                    for name in KEPT_HEADERS
                    if name in response.headers
                }:
                    self._exchange["headers"] = kept
            if self._record:
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
            headers={"content-type": self._exchange["content_type"]}
            | self._exchange.get("headers", {}),
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

    @property
    def events(self) -> list[dict[str, Any]]:
        """The data of a streamed answer's events as JSON, parsed here independently of
        the adapter: every `data:` line but the `[DONE]` that ends them."""
        assert self._exchange is not None, "nothing was requested yet"
        lines = self._exchange["body"].splitlines()
        data = [line.removeprefix("data:").strip() for line in lines if line.startswith("data:")]
        return [json.loads(each) for each in data if each != "[DONE]"]

    @property
    def rest(self) -> str:
        """What a streamed answer holds besides its events: its lines that aren't
        empty and aren't `data:` lines, joined again."""
        assert self._exchange is not None, "nothing was requested yet"
        lines = self._exchange["body"].splitlines()
        return "\n".join(line for line in lines if line and not line.startswith("data:"))

    @property
    def body(self) -> Any:
        """The whole answer as JSON, such as a refusal or a list of models."""
        assert self._exchange is not None, "nothing was requested yet"
        return json.loads(self._exchange["body"])


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


type StartCloud = Callable[..., tuple[OpenAICompatible, Tape]]


@pytest.fixture
async def cloud_with() -> AsyncIterator[StartCloud]:
    """Call with a provider's home, `groq` or `gemini`, and a cassette's name, to get an
    adapter that plays it, and the tape. Options go to the adapter.

    - `key` is the key it is sent with, replayed and live alike: for a made-up one.
      Otherwise, live, the provider's own, and without that the test is skipped.
    - `replay_only` says why the exchange is never asked live, such as a limit that was
      reached, which depends on the account's state: its test is skipped there.
    """
    clients: list[httpx2.AsyncClient] = []

    def start(
        home: Home,
        name: str,
        *,
        key: str | None = None,
        replay_only: str | None = None,
        **options: Any,
    ) -> tuple[OpenAICompatible, Tape]:
        cloud = CLOUDS[home]
        if CLOUD and replay_only:
            pytest.skip(f"never asked live: {replay_only}")
        if CLOUD:
            # A provider without a key is skipped whole, its test of a made-up key too.
            if not os.environ.get(cloud.key_variable):
                pytest.skip(f"needs {cloud.key_variable}, the key it is asked with live")
            key = key or os.environ[cloud.key_variable]
        tape = Tape(name, home)
        client = httpx2.AsyncClient(
            base_url=cloud.url,
            headers={"Authorization": f"Bearer {key or 'replayed-without-a-key'}"},
            transport=tape,
        )
        clients.append(client)
        return OpenAICompatible(client, provider=home, **options), tape

    yield start
    for client in clients:
        await client.aclose()


@pytest.fixture
def tidied() -> Callable[[str, int, str], str]:
    """`tidy_cloud`, for a test of its own: a test module can't import this file."""
    return tidy_cloud


@pytest.fixture
def live_url() -> str:
    """The real server's address; skips the test when none is given."""
    if LIVE_URL is None:
        pytest.skip("needs a real Ollama server: set ACTIO_TEST_OLLAMA_URL (make test-live)")
    return LIVE_URL
