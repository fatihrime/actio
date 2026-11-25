# actio-llm

Actio's model clients: one neutral chat format, and two adapters that speak it.

- **The neutral format** (`chat`): a `ChatRequest` goes in, and text, reasoning, tool calls and one final event with token counts stream out. The rest of Actio never sees a provider's JSON.
- **The Ollama adapter** (`Ollama`) uses [Ollama](https://ollama.com)'s native API so that a model never answers from a quietly shortened prompt, and no answer runs without a limit.
- **The OpenAI-compatible adapter** (`OpenAICompatible`) serves every server that speaks OpenAI's Chat Completions API: [fakellm](https://fatihrime.github.io/actio/reference/fakellm/) in tests, Groq and the Gemini API, and vLLM or any other.
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

## Loading a model ahead

Ollama loads a model at the first request for it, and lets it go five minutes after the last one (`keep_alive`). Until it is loaded again, the next question waits for the load: seconds, for a model of a few gigabytes. `load` has it loaded before anything is asked:

```python
await Ollama(client).load("granite4:micro", context_window=4096)
```

- **The request is a chat's, without its messages:** the one `stream` would send for that model and window. Measured on Ollama 0.40.16: after a load that left out `"shift": false`, or named another window, the next question loaded the model again, for 1.4 to 3.4 s. So give it the window the questions will have.
- **It returns once the server says that the model is loaded,** and waits for that as long as a chat waits for its first line. A model that is loaded already is answered in milliseconds, and kept for another five minutes. Hanging up before the answer abandons the load at the server.
- **`isinstance(provider, LoadsAhead)` says whether a provider can do this.** The OpenAI-compatible adapter can't: that API has no such call.

Why, and what Actio does with it: [ADR 0026](https://fatihrime.github.io/actio/adr/0026-models-loaded-ahead-of-the-first-question/).

## What the Ollama adapter guarantees

**A model never answers from a shortened prompt, and an answer is never unbounded.** How that is achieved depends on which of Ollama's two engines runs the model. Both were measured against Ollama 0.40.16.

| | Default engine (GGUF models) | MLX engine (the usual build on Apple Silicon) |
|---|---|---|
| What Ollama does by itself | Cuts a prompt that doesn't fit: it answers 200 from roughly the last half-window, and the start is gone, system prompt included. Lets an answer that fills the window push the oldest tokens out | Ignores the context window. Reads every prompt in full, and lets an answer run until the model stops |
| A prompt longer than the window, through this adapter | Refused: `ContextOverflowError`, with the prompt's and the window's size in tokens | Answered in full. Nothing is cut; `Usage.input_tokens` reports the real size |
| An answer, through this adapter | Stops when the window is full, or at its token limit | Stops at its token limit, which the adapter always sends |
| Memory | Fixed by the window | Grows with the conversation |
| `ModelInfo.enforces_window` | `True` | `False`: the window is the caller's budget |

On both engines:

- **Every answer has a token limit:** the request's `max_output_tokens`, never more than its `context_window`, and 4,096 when it names neither. Measured without one: a small reasoning model thought for 7,297 tokens, three minutes, to say hello.
- **A server older than 0.40 is refused** with `UnsupportedServerError` before any prompt is sent, because it may ignore the flags that stop the default engine from cutting prompts.
- **A model that asks for a tool** finishes with the reason `tool_calls`. Ollama itself says `stop`.

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
| The Gemini API sends a model's thoughts inside the text, each piece marked as a thought, the first opened with `<thought>` and the answer's first piece with `</thought>` | Reads them as reasoning. `<thought>` is taken off the start of a thought piece, and `</thought>` off the start of the first piece of text after thoughts, nowhere else: an answer may write them itself |
| A tool call arrives in fragments: its name, then pieces of its arguments' JSON text | Joins and parses them by their `index`, and yields the call whole |
| The Gemini API sends two calls at once as two whole calls, neither with an `index` | A fragment without one that carries a new id starts a new call |
| Some servers ignore the request for token counts | `Finish.usage` is `None`, never zeros: zeros would be charged as a free request |
| The Gemini API's `completion_tokens` leaves the reasoning out, which its `total_tokens` holds | The output is `total_tokens - prompt_tokens` when that is more |
| Groq says where the time went, in seconds | `Finish.timings`, as from Ollama, with no time to load |
| Some finish a tool call with the reason `stop` | The finish reason is `tool_calls` whenever a tool was called |
| The output limit is `max_completion_tokens`; older servers only know `max_tokens` | Sends the first; `output_limit_field="max_tokens"` switches |
| Groq's models and `gemini-3.5-flash-lite` refuse to be told `reasoning_effort: "none"`, and the Gemini API sends a model's thoughts only when asked to | `thinking_off` and `thinking_on` say, for each model, what goes into a request that asks for no reasoning and for some. Left out: `{"reasoning_effort": "none"}`, and nothing. An empty mapping is nothing |
| A refusal is `{"error": {...}}`, or a list of one (the Gemini API). A 429 says how long to wait in `Retry-After`, or in its body (the Gemini API) | Reads both |
| The Gemini API, too busy in the middle of an answer, sends that list as plain lines that are no event, and ends the stream | A `StreamInterruptedError`, whose message says what the list said |
| The Gemini API refuses a key it doesn't know with 400, and Groq a request larger than the account takes in a minute with 413 | The first is a refusal of the credentials, as a 401 is; the second a `ContextOverflowError` |
| The Gemini API's list of models names `models/gemini-3.6-flash`, and its chat takes `gemini-3.6-flash`. Another server may name a model `models/qwen` and take exactly that, as vLLM started with a path does | `models()` gives such an id both ways, as written and without `models/` |
| Keep-alive comments, a missing `[DONE]` marker | Tolerated |

For a model on Groq, which always reasons:

```python
provider = OpenAICompatible(client, provider="groq", thinking_off={"reasoning_effort": "low"})
```

`context_window` is not sent: these servers fix it per model. This is also why Ollama has its own adapter, although it offers this format too. What Groq and the Gemini API were seen to send, and why the adapter reads it so: [ADR 0027](https://fatihrime.github.io/actio/adr/0027-cloud-models-through-their-openai-compatible-layers/).

## Errors

Every failure is an `LLMError`. The subclasses answer what a caller has to decide:

| Error | Meaning | What a caller can do |
|---|---|---|
| `ProviderUnavailableError` | Can't connect, the server is overloaded, or it didn't start answering in time | Try another provider: nothing was generated |
| `RateLimitedError` | The provider answered 429. A kind of `ProviderUnavailableError` | Try another provider, or wait `retry_after_s` when the provider gave it |
| `StreamInterruptedError` | The answer started, then broke off or went silent | Keep what arrived; retry only if nothing was shown |
| `ContextOverflowError` | The prompt doesn't fit the context window, or is larger than the provider takes from the account in one request | Shorten the history, or use a larger window |
| `UnsupportedFeatureError` | The model can't reason, or can't call tools | Ask `describe()` first |
| `ModelNotFoundError` | The server doesn't have the model | Pull it |
| `RequestRejectedError` | The server refused the request for another reason | Fix the request: retrying won't help |
| `ProviderError` | The server failed, refused Actio's credentials, or didn't speak its own protocol | Report it; another provider may work |
| `UnsupportedServerError` | Ollama is older than 0.40 | Upgrade Ollama |

Deadlines (`Timeouts`) apply to connecting, to the first chunk, and to the gap between chunks. They time each read from Ollama, never the caller's own work between two chunks.

## A guess at token counts

Sometimes nobody has counted. Before a request is sent, its prompt has no count yet. A stream that is closed early ends without a `Finish`, and some servers report no counts at all. For those cases `actio_llm.estimate` guesses, without any model's tokenizer:

```python
from actio_llm import System, User, prompt_tokens, tokens_in

tokens_in("1,250.00 TL")  # 9: six digits, two marks, and "TL"
prompt = [System("You are a helpful assistant."), User("Hello")]
prompt_tokens(prompt)  # 31, with what a chat template writes around the two messages
```

- **Three rules:** a digit is a token, a punctuation mark is a token, and everything else is four bytes to a token. White space is free.
- **Measured against two models' own counts,** for whole prompts, as guess ÷ count:

    | | English | Turkish | Python | Japanese | JSON full of numbers |
    |---|---|---|---|---|---|
    | `qwen3.5:4b` | 1.05 | 0.90 | 1.11 | 1.26 | 1.06 |
    | `granite4:micro` | 1.05 | 0.70 | 1.16 | 0.66 | 1.47 |

- **It can be a third out either way,** because tokenizers differ. Counting bytes alone, at three to a token, came out at 0.46 for the JSON: under half. A caller that must not come out short adds its own margin, and uses the provider's count when one arrives.

## Not here yet

- **Images and structured output** (a JSON schema for the answer). They arrive with the features that use them.
- **Unloading and pulling models.** They arrive with the model manager.
- **A guess that learns.** The estimate uses the same rules for every model. Correcting it per model, from the counts providers report, comes with the router.
- **A router** that picks a provider, falls back to another and retries. The errors above are shaped for it.

## Tests

```sh
uv run pytest packages/llm      # replays recorded exchanges: no Ollama needed
make test-live                  # the same requests, sent to a real Ollama server
make test-cloud                 # those of Groq and the Gemini API, sent to the real servers
```

- **Recorded exchanges.** `tests/fixtures/ollama/` holds real responses from an Ollama server, one file per request. The tests replay them, and fail if the adapter no longer sends exactly the recorded request.
- **Live mode.** `make test-live` needs `granite4:micro` on the server. It also runs what a recording can't show: that closing a stream frees the server, that a model loaded ahead isn't loaded again by the next question, and that the guess at a prompt's tokens stays near the server's count. That test unloads `granite4:micro` first.
    - Set `ACTIO_TEST_OLLAMA_THINKING_MODEL` to a model that can reason, to test reasoning against it too.
    - Set `ACTIO_TEST_OLLAMA_MLX_MODEL` to a model that runs on the MLX engine, to pin that engine's behaviour. On an Apple Silicon Mac, `qwen3.5:4b` serves for both.
    - Add `ACTIO_RECORD_OLLAMA=1` to save the server's answers as the new recordings, after an Ollama upgrade.
- **Groq and the Gemini API.** `tests/fixtures/groq/` and `gemini/` hold real exchanges with the two, through the OpenAI-compatible adapter, replayed the same way. A test compares what the adapter yields with the recorded lines, read apart from the adapter.
    - `make test-cloud` sends the same requests to the real servers, with the keys in `GROQ_API_KEY` and `GEMINI_API_KEY`: about fifteen short requests, which count against the accounts' limits. A provider without a key has its tests skipped.
    - Add `ACTIO_RECORD_CLOUD=1` to save the answers as the new recordings. What is saved holds no header of the request, and a fixed stand-in for Groq's ids of a request and of a chat completion, for the account's organization and for a thought's signature. The Gemini API's ids of a chat and the tool calls' ids stay: they name no account. A test fails at an organization's id other than the stand-in, a longer signature, or a header named `authorization`.
    - Two of the requests go to `gemini-3.6-flash`, of which a free account had 20 a day on 2026-11-25. The Gemini API's other exchanges are `gemini-3.5-flash-lite`'s, which had 500. `make test-cloud args="-k thoughts"` asks one alone.
    - Five exchanges depend on the account's or the provider's state, such as a limit that was reached or a model too busy. They were written from real answers, and are never asked live.
- **Hand-written responses** cover what a healthy server won't produce on demand: dropped connections, stalls, malformed lines, old versions.
- **The OpenAI-compatible adapter runs against fakellm,** in process. fakellm is a separate implementation of the format, itself checked with OpenAI's official SDK, so neither is only tested against itself. Directives in the prompt script its reasoning, tool calls and failures.
