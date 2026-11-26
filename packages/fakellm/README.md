# fakellm

A deterministic, OpenAI-compatible fake LLM server. Tests and the local stack use it in place of a real model, so they run offline and fast, with exact expected outputs.

## Run it

```sh
uv run fakellm                                   # http://127.0.0.1:8100, "fake-chat" and "fake-quiet"
uv run fakellm --port 9000 --model a --model b
```

Point any OpenAI-compatible client at `http://127.0.0.1:8100/v1`. To try it in a browser, open `http://127.0.0.1:8100/docs`. Like other API servers, it has no page at `/`.

## What it does

- `POST /v1/chat/completions` replies `Echo: <last user message>`:
    - as one JSON response;
    - or, with `"stream": true`, as server-sent events ending with `data: [DONE]`.
- **A second model, `fake-quiet`, says nothing by itself.** Its reply has no text, and ends as a finished reply does, with the finish reason `stop`. It says only what `[[say: ...]]` tells it to (below). Served under any other name, the fake echoes.
    - It stands in for a model asked for a conversation's title. That request quotes the question after the same instructions each time, so an echo would be the same words for every conversation.
- Each word, with the whitespace after it, counts as one token. `usage` reports those counts. With `stream_options.include_usage`, a stream ends with OpenAI's extra usage chunk.
- `max_completion_tokens` (or the older `max_tokens`) cuts the reply short and sets `finish_reason` to `length`.
- Unknown models get a 404 and invalid requests a 400, both in OpenAI's error format.
- `GET /v1/models` lists the served models, and `GET /healthz` reports `ok`.

The official `openai` SDK is tested against it, including its tool calls and its rate-limit error.

## Scripting a reply

Put directives in the last user message to change what the fake does. They are left out of the echoed text, and work alike for both models.

| Directive | Effect |
|---|---|
| `[[think: some text]]` | Reason first: the text streams as `reasoning`, before the answer |
| `[[call: get_weather {"city": "Izmir"}]]` | Call a tool instead of answering. Repeat it for several calls. The arguments stream in fragments, as OpenAI's format allows |
| `[[fail: 503]]` | Answer with this HTTP status, in OpenAI's error format. A 429 adds `Retry-After: 1` |
| `[[fail: 400 context_length_exceeded]]` | The same, with this error code |
| `[[retry-after: 30]]` | With `[[fail: 429]]`, the seconds its `Retry-After` says, in the place of 1: a whole number, 0 or more. With another status, or without `[[fail: ...]]`, it changes nothing. For a test of a provider that names a wait longer than Actio waits out itself |
| `[[drop: 2]]` | Stream 2 tokens, then end the stream without a finish reason or `[DONE]` |
| `[[delay: 0.5]]` | Wait this many seconds before answering (30 at most) |
| `[[pace: 0.05]]` | Wait this many seconds between the pieces of a streamed reply (5 at most), as a real model takes its time. For tests that stop an answer half-way, or watch a page follow it |
| `[[say: Flat tyres]]` | For `fake-quiet`: reply with this text. Several are joined by a space, in order. `fake-chat` echoes as ever. A request that quotes the question, such as one for a conversation's title, carries the question's directives with it, so a test names its conversation by typing |

```sh
curl -s http://127.0.0.1:8100/v1/chat/completions -H 'content-type: application/json' -d '{
  "model": "fake-chat",
  "messages": [{"role": "user", "content": "[[think: two plus two]] What is 2+2?"}]
}'
```

- **Why in the message, and not a control API:** the fake keeps no state between requests, so tests can run in any order and in parallel. A test that drives the whole stack through a browser can script it by typing into the chat box.
- **Only the user scripts it.** Directives in earlier messages, or inside a tool's result, are plain text.
- **After a tool's result,** the reply echoes that result, and `fake-quiet` says nothing. An agent loop that was scripted to call a tool therefore gets a plain answer on its next step, not the same call again.
- **A directive that can't be read is a 400,** so a typo in a test's script fails that test instead of being echoed back.
- **Tokens stay countable:** reasoning words count as output tokens, and a tool call costs its name plus the words of its arguments. The output limit cuts the reasoning first, then the answer; it never cuts a tool call.
