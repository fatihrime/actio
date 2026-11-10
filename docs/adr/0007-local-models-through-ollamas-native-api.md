# 0007. Local models through Ollama's native API, with silent truncation turned off

- **Status:** accepted
- **Date:** 2026-11-10

## Context

Actio's default models run locally, in [Ollama](https://ollama.com). The chat, the bots and the developer gateway will all send their prompts through one client, so its behaviour becomes the platform's.

Before writing that client we measured what Ollama does, on version 0.40.16 with a small model, on a 16 GB Mac:

- **The default context window is 4,096 tokens.** Ollama picks it from the machine's video memory: 4k below 24 GiB.
- **A prompt that doesn't fit is cut, silently.** A 5,653-token prompt sent into a 2,048-token window came back as HTTP 200 with a normal-looking answer. Ollama had kept about the last 1,000 tokens. The start was gone, including the system prompt.
- **An answer that fills the window keeps going.** Ollama drops the oldest tokens to make room, again without saying so.
- **The OpenAI-compatible endpoint behaves the same,** and can't change the window: its documentation says "The OpenAI API does not have a way of setting the context size for a model."

For Actio that isn't cosmetic. The system prompt will carry the safety rules and a support bot's instructions. The usage limits charge for the tokens a request used. The interface shows how full the context is. All three are wrong if the prompt the model saw isn't the prompt Actio sent.

## Options considered

1. **Ollama's OpenAI-compatible endpoint,** through the same adapter as every other provider.
   - Pro: one adapter for Ollama, the test fake and the hosted providers.
   - Con: no way to set the context window per request, and the silent cut above.
   - Con: no report of model load time or of prompt tokens served from cache.
2. **The official `ollama` Python client.**
   - Pro: maintained by Ollama, with typed responses.
   - Con: checked on 2026-11-10 (version 0.6.3), its `chat()` has no parameter for the two strict flags described below, and passes no extra fields through.
   - Con: timeouts, cancellation and error handling would still be ours to write around it.
3. **A universal client library,** such as LiteLLM.
   - Pro: many providers behind one call.
   - Con: a large dependency to speak two wire formats, and it hides exactly the provider behaviour that has to be controlled here.
4. **Our own thin adapter for Ollama's native API** (`/api/chat`).
   - Pro: the native API sets the context window per request, and accepts two flags that turn both silent behaviours into explicit ones.
   - Con: a second adapter to maintain next to the OpenAI-compatible one.

## Decision

We chose option 4: `actio_llm.ollama`, one module of about 450 lines on top of an HTTP client.

- **Strict by default.** Every chat request carries two flags:
  - `"truncate": false`: a prompt that doesn't fit is refused with HTTP 400. The adapter raises `ContextOverflowError`, with the token counts Ollama reports;
  - `"shift": false`: an answer that fills the window stops there, and finishes with the reason `length`.
- **The window is explicit.** A request names its `context_window`, sent as `num_ctx`. Changing it reloads the model, so callers keep one size per model.
- **Old servers are refused.** A server that ignored the flags would bring the silent cut back, so the adapter checks the version once and refuses anything older than 0.40.0, the release line the flags were measured on.
- **A neutral chat format.** The adapter takes a `ChatRequest` and yields text, reasoning, tool calls and one final event with token counts and timings. The rest of Actio never sees Ollama's JSON. An OpenAI-compatible adapter for the test fake and hosted providers will speak the same format.
- **Typed errors** that answer what the caller must decide:
  - the provider is unavailable, so another one can be tried;
  - the answer broke off half-way;
  - the request itself was refused, so sending it again won't help.
- **Deadlines per stage:** connecting, the first chunk (which also covers loading a cold model and waiting in Ollama's queue), and the gap between chunks. They time each read from Ollama, never the caller's own work between two chunks.
- **Stopping is hanging up.** Closing the stream closes the connection, and Ollama stops generating. No separate cancel call exists or is needed.
- **`httpx2` as the HTTP client.** Checked on 2026-11-10: httpx's last stable release is from December 2024, and `httpx2` is its continuation, maintained by Pydantic. The OpenAI SDK, which the tests already use, depends on it.

**How it is tested:**
- **Recorded exchanges.** The tests replay real responses, recorded from an Ollama server, and fail if the adapter stops sending exactly the recorded request. CI needs no model.
- **A live mode.** `make test-live` sends the same requests to a real server, which also covers what a recording can't show: that hanging up really frees the server.
- **A planted bug.** With the two flags removed, the live tests fail: the server accepts the over-long prompt, and writes 2,560 tokens into a 256-token window.

## Consequences

- **Easier:**
  - a prompt is either answered in full or refused with a clear error, so the context meter, the usage limits and the system prompt can be trusted;
  - model capabilities, load time and cached prompt tokens are available to the interface and to the usage limits.
- **Harder:**
  - two adapters instead of one;
  - the strict flags aren't in Ollama's documented list of request fields (checked 2026-11-10), so they could change without notice. Three things guard against that: the recorded requests, the live tests, and the version check;
  - recordings go stale when Ollama changes, and have to be recorded again.
- **Not yet measured:** everything above was measured with one model in the GGUF format. Ollama also ships models for Apple's MLX engine, which may behave differently. The same tests will be run against one before it becomes a default.
- **How to reverse it:** the adapter is one module behind the `ChatProvider` interface. Removing it and pointing the OpenAI-compatible adapter at Ollama's `/v1` endpoint would work, at the price of the guarantees above.
