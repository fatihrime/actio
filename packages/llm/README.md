# actio-llm

Actio's model clients: one neutral chat format, and two adapters that speak it.

- **The neutral format** (`chat`): a `ChatRequest` goes in, and text, reasoning, tool calls and one final event with token counts stream out. The rest of Actio never sees a provider's JSON.
- **The Ollama adapter** (`Ollama`) uses [Ollama](https://ollama.com)'s native API and turns off its silent truncation: a prompt that doesn't fit the model's context window is refused, not quietly cut.
- **The OpenAI-compatible adapter** (`OpenAICompatible`) serves every server that speaks OpenAI's Chat Completions API: [fakellm](https://fatihrime.github.io/actio/reference/fakellm/) in tests, and vLLM or hosted providers later.
- **A command-line tool** (`actio-llm`) to try the Ollama adapter from a terminal.

The design and its trade-offs are in [ADR 0007](https://fatihrime.github.io/actio/adr/0007-local-models-through-ollamas-native-api/).

## Try it

You need Ollama 0.40 or newer, running, and a model:

```sh
ollama pull granite4:micro        # 2.1 GB: small, and it can call tools
uv run actio-llm models
uv run actio-llm chat granite4:micro "Say hello in five words." --context 4096
```

The answer streams to the terminal, followed by what the adapter learned about it:

```text
Hello, world!
[stop] · prompt 36 tokens (0 from cache) · answer 5 tokens · 61.0 tokens/s · model load 1.5 s
```

- `--think` and `--no-think` turn a model's reasoning on or off, for models that have it.
- `--context`, `--max-output` and `--system` set the context window, the answer's length limit and a system prompt.
- `--url`, or `ACTIO_OLLAMA_URL`, points at a server other than `http://127.0.0.1:11434`.

## In code

```python
from contextlib import aclosing

import httpx2

from actio_llm import ChatRequest, Finish, Ollama, System, TextDelta, User


async def ask() -> None:
    async with httpx2.AsyncClient(base_url="http://127.0.0.1:11434") as client:
        request = ChatRequest(
            model="granite4:micro",
            messages=[System("Answer in one sentence."), User("Why is the sky blue?")],
            context_window=4096,
        )
        async with aclosing(Ollama(client).stream(request)) as events:
            async for event in events:
                match event:
                    case TextDelta(text=text):
                        print(text, end="", flush=True)
                    case Finish(reason=reason, usage=usage):
                        print(f"\n{reason}: {usage.input_tokens} in, {usage.output_tokens} out")
```

A stream is any number of `TextDelta`, `ReasoningDelta` and `ToolCall` events, then exactly one `Finish`. Leaving the `aclosing` block early closes the connection, and Ollama stops generating: that is how an answer is stopped.

## What the Ollama adapter guarantees

Measured against Ollama 0.40.16. The left column is what Ollama does when asked nothing special.

| Situation | Ollama by default | Through this adapter |
|---|---|---|
| The prompt is longer than the context window | Answers 200, from roughly the last half-window of the prompt. The start is dropped, system prompt included | `ContextOverflowError`, with the prompt's size and the window's size in tokens |
| The answer fills the context window | Drops the oldest tokens and keeps writing | Stops, with the finish reason `length` |
| The context window | 4,096 tokens on a machine with less than 24 GiB of video memory | Whatever the request's `context_window` says |
| The server is older than 0.40 | It may ignore the two flags behind the rows above | `UnsupportedServerError`, before any prompt is sent |
| The model asks for a tool | The finish reason is `stop` | The finish reason is `tool_calls` |

It also reports what the usage limits and the interface need:

- **Token counts:** the whole prompt, how much of it came from Ollama's prompt cache, and everything generated.
- **Timings:** model load, prompt reading and generation, so a cold start can be told from a slow model.
- **Capabilities:** `describe()` says whether a model can reason or call tools, and the largest window it was built for.

## OpenAI-compatible servers

```python
client = httpx2.AsyncClient(
    base_url="https://api.example.com/v1", headers={"Authorization": "Bearer ..."}
)
provider = OpenAICompatible(client, provider="Example")
```

The client carries the address and the credentials; the adapter never sees a key. Servers that share this format still differ, so the adapter reads leniently and sends explicitly:

| Difference between servers | What the adapter does |
|---|---|
| Reasoning arrives as `reasoning` (vLLM, Groq, OpenRouter) or as `reasoning_content`, the older name | Reads both |
| A tool call arrives in fragments: its name, then pieces of its arguments' JSON text | Joins and parses them, and yields the call whole |
| Some servers ignore the request for token counts | `Finish.usage` is `None`, never zeros: zeros would be charged as a free request |
| Some finish a tool call with the reason `stop` | The finish reason is `tool_calls` whenever a tool was called |
| The output limit is `max_completion_tokens`; older servers only know `max_tokens` | Sends the first; `output_limit_field="max_tokens"` switches |
| Keep-alive comments, a missing `[DONE]` marker | Tolerated |

`context_window` is not sent: these servers fix it per model. This is also why Ollama has its own adapter, although it offers this format too.

## Errors

Every failure is an `LLMError`. The subclasses answer what a caller has to decide:

| Error | Meaning | What a caller can do |
|---|---|---|
| `ProviderUnavailableError` | Can't connect, the server is overloaded, or it didn't start answering in time | Try another provider: nothing was generated |
| `RateLimitedError` | The provider answered 429. A kind of `ProviderUnavailableError` | Try another provider, or wait `retry_after_s` when the provider gave it |
| `StreamInterruptedError` | The answer started, then broke off or went silent | Keep what arrived; retry only if nothing was shown |
| `ContextOverflowError` | The prompt doesn't fit the context window | Shorten the history, or use a larger window |
| `UnsupportedFeatureError` | The model can't reason, or can't call tools | Ask `describe()` first |
| `ModelNotFoundError` | The server doesn't have the model | Pull it |
| `RequestRejectedError` | The server refused the request for another reason | Fix the request: retrying won't help |
| `ProviderError` | The server failed, refused Actio's credentials, or didn't speak its own protocol | Report it; another provider may work |
| `UnsupportedServerError` | Ollama is older than 0.40 | Upgrade Ollama |

Deadlines (`Timeouts`) apply to connecting, to the first chunk, and to the gap between chunks. They time each read from Ollama, never the caller's own work between two chunks.

## Not here yet

- **Images and structured output** (a JSON schema for the answer). They arrive with the features that use them.
- **Warm-up, unloading and pulling models.** They arrive with the model manager.
- **A token estimate.** A stream that is closed early ends without a `Finish`, and some servers report no token counts. In both cases the caller has to count, and the counter isn't written yet.
- **A router** that picks a provider, falls back to another and retries. The errors above are shaped for it.

## Tests

```sh
uv run pytest packages/llm      # replays recorded exchanges: no Ollama needed
make test-live                  # the same requests, sent to a real Ollama server
```

- **Recorded exchanges.** `tests/fixtures/ollama/` holds real responses from an Ollama server, one file per request. The tests replay them, and fail if the adapter no longer sends exactly the recorded request.
- **Live mode.** `make test-live` needs `granite4:micro` on the server. It also runs what a recording can't show: that closing a stream frees the server.
  - Set `ACTIO_TEST_OLLAMA_THINKING_MODEL` to a model that can reason, to test reasoning against it too.
  - Add `ACTIO_RECORD_OLLAMA=1` to save the server's answers as the new recordings, after an Ollama upgrade.
- **Hand-written responses** cover what a healthy server won't produce on demand: dropped connections, stalls, malformed lines, old versions.
- **The OpenAI-compatible adapter runs against fakellm,** in process. fakellm is a separate implementation of the format, itself checked with OpenAI's official SDK, so neither is only tested against itself. Directives in the prompt script its reasoning, tool calls and failures.
