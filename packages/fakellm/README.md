# fakellm

A deterministic, OpenAI-compatible fake LLM server. Tests and the local stack use it in place of a real model, so they run offline and fast, with exact expected outputs.

## Run it

```sh
uv run fakellm                                   # http://127.0.0.1:8100, model "fake-chat"
uv run fakellm --port 9000 --model a --model b
```

Point any OpenAI-compatible client at `http://127.0.0.1:8100/v1`. To try it in a browser, open `http://127.0.0.1:8100/docs`. Like other API servers, it has no page at `/`.

## What it does

- `POST /v1/chat/completions` replies `Echo: <last user message>`:
  - as one JSON response;
  - or, with `"stream": true`, as server-sent events ending with `data: [DONE]`.
- Each word, with the whitespace after it, counts as one token. `usage` reports those counts. With `stream_options.include_usage`, a stream ends with OpenAI's extra usage chunk.
- `max_completion_tokens` (or the older `max_tokens`) cuts the reply short and sets `finish_reason` to `length`.
- Unknown models get a 404 and invalid requests a 400, both in OpenAI's error format.
- `GET /v1/models` lists the served models, and `GET /healthz` reports `ok`.

The official `openai` SDK is tested against it. Tool calls and injected faults (errors, delays, dropped streams) arrive in Phase 1, with the tests that need them.
