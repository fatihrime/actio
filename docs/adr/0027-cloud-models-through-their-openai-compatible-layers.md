# 0027. Groq and the Gemini API, through their OpenAI-compatible layers, by the one client

- **Status:** accepted
- **Date:** 2026-11-25

## Context

Actio is to send answers to the free tiers of two cloud providers, Groq and Google's Gemini API, beside the local Ollama. Both offer their models on a layer that speaks OpenAI's Chat Completions API. Actio has a client for that API (`actio_llm.openai_compatible`, [ADR 0007](0007-local-models-through-ollamas-native-api.md)), and a models file in which a server that speaks it is a provider of the kind `openai-compatible`, with its address and the name of the variable that holds its key ([ADR 0013](0013-the-models-on-offer-are-a-file.md)). Until now that client had been tested against fakellm, a fake written here to the format's description, and against no real server.

So before a model of either provider was offered, each was asked with a free key, through that layer, with made-up questions, and what it answered was kept. What was seen is under [Seen on 2026-11-25, with free keys](#seen-on-2026-11-25-with-free-keys). The client would have got five things wrong:

1. **Tokens.** The Gemini API's `completion_tokens` leaves out the model's reasoning, which only `total_tokens` holds. An answer that cost 127 tokens was counted as 7, and the usage limits charge what is counted ([ADR 0020](0020-usage-limits-on-turns.md)).
2. **No reasoning.** A turn that asks for none is sent `reasoning_effort: "none"`. Groq's models refuse it with 400, so every such turn on them would fail. They always reason.
3. **Thoughts inside the text.** The Gemini API sends a model's thoughts in `content`, between tags, when it sends them at all. They would have been shown as the answer, tags and all. And `gemini-3.5-flash-lite` refuses `"none"` too, while it reasons only when it is told to.
4. **Tool calls without an index.** The Gemini API sends two calls at once as two whole calls, neither with an `index`. The client joined them into one call that couldn't be read.
5. **Refusals.** The Gemini API's are a list of one object, so the client's message was the whole body. A key it doesn't know is refused with 400, not 401, which the client took for a request at fault. A spent quota says how long to wait in its body, not in a header. Its list of models names `models/gemini-3.6-flash`, where its chat takes `gemini-3.6-flash`, so the catalog would have called every Gemini model `missing`. And Groq refuses a request larger than the account's tokens a minute with 413, which the client called a refused request.

What else the choice had to fit:

- **Only `actio.catalog` names a provider's own model,** or builds a client for one: the rest of Actio asks for a model by its name in the models file (ADR 0013).
- **A model error's text never reaches a log or a reader.** A log line names the error's kind, and the page says what went wrong in Actio's own words ([ADR 0008](0008-postgres-through-sqlalchemy-and-psycopg.md), revision of 2026-11-20).
- **The client's tests replay real exchanges** where a real server can be had, as Ollama's do (ADR 0007).
- **The API and the worker build the providers' clients,** each in its own container: the worker asks a model for a conversation's title ([ADR 0025](0025-titles-written-by-a-model.md)).

## Seen on 2026-11-25, with free keys

Each through the provider's OpenAI-compatible layer. Both took `Authorization: Bearer <key>`, the output limit as `max_completion_tokens` and as the older `max_tokens`, and `stream_options.include_usage`.

**Groq,** at `https://api.groq.com/openai/v1`, with `openai/gpt-oss-120b` and `openai/gpt-oss-20b`:

| What | What it did |
|---|---|
| Reasoning | Sent as `delta.reasoning`, with a member `channel` beside it, before the answer |
| `reasoning_effort` | `low`, `medium` or `high`. `"none"` is refused: 400, "\`reasoning_effort\` must be one of \`low\`, \`medium\`, or \`high\`". The two models always reason |
| Token counts | The last chunk with a choice carries `usage`, and the same under `x_groq.usage`; one more chunk, with `"choices": []`, carries it again. `completion_tokens` holds the reasoning (`completion_tokens_details.reasoning_tokens`). `prompt_tokens_details.cached_tokens` appears when part of the prompt was cached |
| Where the time went | In the usage, in seconds: `queue_time`, `prompt_time`, `completion_time`, `total_time`. 0.2817, 0.0039, 0.0829 and 0.0868 for an answer of 40 tokens |
| A tool call | Whole in one chunk, with `index: 0`, an id `fc_…`, and `finish_reason: "tool_calls"` |
| A model it hasn't got | 404, `code: "model_not_found"` |
| A made-up key | 401, `code: "invalid_api_key"` |
| A request larger than the account's tokens a minute, 8,000 on the free plan | 413, with `retry-after: 14`: "Request too large for model … in organization `org_…` … on tokens per minute (TPM): Limit 8000, Requested 9851, please reduce your message size and try again.", `code: "rate_limit_exceeded"`. No wait makes it fit. The message names the account's organization |
| At the rate limit | 429, with `retry-after: 2`, the same shape: "Rate limit reached for model … Limit 8000, Used 3171, Requested 5055. Please try again in 1.695s." |
| Limits in the headers | Requests a day and tokens a minute, and nothing else: 1,000 requests a day and 8,000 tokens a minute for each of the two models, on the free plan |
| `GET /models` | `{"object": "list", "data": [{"id": "openai/gpt-oss-20b", …}]}`, each entry with its window and its longest answer |

**The Gemini API,** at `https://generativelanguage.googleapis.com/v1beta/openai`, with `gemini-3.6-flash` and `gemini-3.5-flash-lite`:

| What | What it did |
|---|---|
| Token counts | `usage` in every chunk, and no chunk with `"choices": []`. `completion_tokens` leaves the reasoning out, and `total_tokens` has it: prompt 24, completion 7, total 151, for 120 tokens of reasoning that no member names |
| Thoughts | Not sent unless asked for, with `"extra_body": {"google": {"thinking_config": {"include_thoughts": true}}}` in the body. Then inside `content`: each thought piece is a delta with `"extra_content": {"google": {"thought": true}}`, the first one's text starts with `<thought>`, later ones have no tag, and the answer's first piece starts with `</thought>`. The last chunk has `extra_content.google.thought_signature` and the `finish_reason`. `reasoning_effort` together with `thinking_config` is refused: 400, "Expected one of either \`reasoning_effort\` or custom \`thinking_config\`; found both." |
| `reasoning_effort: "none"` | Turns reasoning off on `gemini-3.6-flash`: total = prompt + completion. So does `"minimal"`. Refused by `gemini-3.5-flash-lite`, 400 "Request contains an invalid argument.": that model doesn't reason unless it is told to |
| `gemini-3.5-flash-lite`'s thoughts | With `include_thoughts` alone, none: it didn't reason. With `"thinking_level": "high"` beside it, and a question that takes thinking, such as a seating puzzle, it reasoned and sent its thoughts as above, in several pieces |
| Two tool calls at once | Two chunks, each a whole call with its own `id`, neither with an `index`; `finish_reason: "stop"` |
| Refusals of the chat | A JSON list of one object: `[{"error": {"code": 404, "message": "models/gemini-9.9-nothing is not found …", "status": "NOT_FOUND"}}]`. `code` is a number, and `status` its name |
| A made-up key | 400, not 401: "Please pass a valid API key", `INVALID_ARGUMENT`. A list from the chat, a plain object from `GET /models` |
| A spent quota | 429 with no `retry-after`, `RESOURCE_EXHAUSTED`, and in `error.details` an entry `{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "83280s"}`. Seen for `gemini-3.1-pro-preview`, which its list offers and its free tier doesn't include: its limits are 0 |
| Overloaded | 503, `UNAVAILABLE`: "This model is currently experiencing high demand…", before a stream, or in the middle of one. In the middle, not as a server-sent event: after the pieces sent so far, the same list of one error, pretty-printed over eight plain lines that are no `data:` line, and then the end, with no `finish_reason` and no `[DONE]`. `gemini-3.8-flash` answered 503 twice, and once nothing within 30 s, in three tries. `gemini-3.6-flash` and `gemini-3.5-flash-lite` answered every time in the first probes, 0.5 to 6.4 s to the first piece; later the same day `gemini-3.5-flash-lite` broke off twice in three requests, in the middle of its thoughts |
| Limits in the headers | None, in any answer |
| Limits of one free account, read on AI Studio's page (<https://aistudio.google.com/rate-limit>), which no answer's headers state | `gemini-3.6-flash` and `gemini-3.8-flash`: 5 requests a minute, 250,000 tokens a minute, 20 requests a day. `gemini-3.5-flash-lite`: 15 requests a minute, 250,000 tokens a minute, 500 requests a day |
| `GET /models` | 65 models in 0.34 s, each named as `models/gemini-3.6-flash` is, where the chat takes `gemini-3.6-flash` |

## Options considered

1. **The Gemini API's own format,** `generateContent`, with an adapter of its own.
    - Pro: thoughts are parts of their own, the reasoning has a count of its own, and the signature of a thought has a place to go back to.
    - Con: a third wire format to write and keep, for models that its OpenAI-compatible layer serves too.
    - Con: Groq would still need the OpenAI-compatible client, with the problems of 2 above.
2. **Each provider's own SDK.**
    - Con: two dependencies, each with its own way to stream, to time out and to fail, which would still have to be turned into Actio's typed errors, as ADR 0007 found of Ollama's client.
    - Con: it hides the wire, where the differences above are, and a recorded exchange can't be replayed through it as simply.
3. **The one client, with a flag for each provider in its code,** such as `if provider == "gemini"`.
    - Con: the client would know providers by name, and a third one that sends one of these shapes would need code.
4. **The one client, reading leniently what differs, and the models file saying what each model is told about reasoning.**
    - Pro: no new dependency, and one place where a provider's shape is read. A shape is read wherever it comes from: a server that sends `total_tokens` with the reasoning in it, or a whole call without an index, is read the same.
    - Pro: whether a model takes `"none"` is a fact of the model, not of its provider. Groq's two models and the Gemini API's two each needed something else, and an installation can say it for a model of its own.
    - Con: a lenient reading may take a shape for something another server means otherwise. Each rule is narrow for that: a tag is taken off only where the thoughts open and close, and the output is made larger, never smaller.
    - Con: the models file has two more settings that an installation must get right. An example of the two providers is there to copy.

## Decision

**Option 4. Groq and the Gemini API are servers of the kind `openai-compatible`, spoken to by the one client, which reads what differs leniently; what a model is told about reasoning is kept in the models file.**

**The client** (`actio_llm.openai_compatible`), which names no provider in its code:

- **Output is counted whole.** When `total_tokens - prompt_tokens` is more than `completion_tokens`, that is the output.
- **Where the time went,** when the usage says it in seconds: `Finish.timings`, with `prompt_ms` and `generation_ms` from `prompt_time` and `completion_time`, `total_ms` from `queue_time + total_time`, and `load_ms` 0. A time that isn't a number, is negative, or is longer than a million seconds gives no timings rather than wrong ones. A million seconds is some eleven days, longer than any answer, and two of them in milliseconds still fit the 32-bit count a turn's record holds. Comparisons alone decide it, which raise nothing for an infinity or an integer of 400 digits.
- **A thought sent inside the text is reasoning:** a delta marked `extra_content.google.thought` is a `ReasoningDelta`. `<thought>` is taken off the start of a thought piece, and `</thought>` off the start of the first piece of text after thoughts, and nowhere else: never by a pattern over the text, since an answer may write either word itself.
- **A tool call without an `index`** starts a new call when it carries an id other than that of the call collected last, or when there is none yet, and belongs to that call otherwise.
- **A stream that breaks off** without a finish is a `StreamInterruptedError`, as before. When lines that are no event of the stream came before its end, they are read as an error, and its message, when they have one, is said in the error's: so the Gemini API's 503 in the middle of an answer reads as what it is. At most 256 such lines are kept: the one refusal of a spent quota that was seen is 64 lines by itself, pretty-printed.
- **A body nested too deep** for the JSON reader, which raises a `RecursionError` then and no `ValueError`, is read as one that isn't JSON, wherever the package reads what a server sent, Ollama's adapter included.
- **Refusals:** a list whose first item is the error object is read as the object, and `status` is the code when `code` isn't a string. A 429 without `Retry-After` takes its wait from a `google.rpc.RetryInfo` in the body, the header winning when both are there. A 413 is a `ContextOverflowError`: what was sent is larger than the provider takes from this account in one request, which no wait changes. The page says what helps, as for a conversation too long for the model's window, and a router can later send it where there is more room. A 400 whose message speaks of the API key is a `ProviderError`, as a 401 is: Actio's credentials were refused.
- **The list of models** gives an id written `models/<name>` both ways: as written, and as `<name>`, which the Gemini API's chat takes. Another server may name a model so and take exactly that, as vLLM started with a path does. No id is there twice.
- **What a model is told about reasoning** is its caller's: `thinking_off` and `thinking_on`, the members put into a request's body when a turn asks for no reasoning and when it asks for some. Left out, they are what they were: `{"reasoning_effort": "none"}`, and nothing. An empty mapping is nothing, also for no reasoning: `gemini-3.5-flash-lite` needs that. They may hold no member the client writes itself (`OWN_MEMBERS`), and each request gets a copy of its own.

**The catalog** (`actio.catalog`): a model of an `openai-compatible` server may have `thinking_off` and `thinking_on` in the models file, as TOML tables of what JSON can hold, when its `thinking` is true. Either of them on an Ollama model, on a model whose `thinking` isn't true, holding a member the client writes itself, or holding what JSON can't, such as a TOML date, stops the API and the worker from starting, with a line that names the model and the member. Each such model gets a client of its own, over its provider's one HTTP client, and shares its provider's gate as before.

**The keys:** `compose.yaml` passes `GROQ_API_KEY` and `GEMINI_API_KEY` from `.env` to the API and the worker, in one block that both take, and leaves out one that isn't set. A models file that names a key that isn't set stops the start, as before.

**An example to copy,** `config/examples/free-cloud/`: the shipped model as the default and for titles, with Groq's `openai/gpt-oss-120b` and `openai/gpt-oss-20b` and the Gemini API's `gemini-3.6-flash` and `gemini-3.5-flash-lite` beside it. Groq's two are told `"low"` for no reasoning and `"medium"` for some, and have 8,000 tokens of window: the free plan's tokens a minute, which a request may not exceed. Not `"high"`: told that, the larger one used up an answer's 2,048 tokens on its thoughts (see Measured). `gemini-3.6-flash` is told to send its thoughts when a turn asks for reasoning, and takes the client's own `"none"`. `gemini-3.5-flash-lite` is told nothing when a turn asks for no reasoning (`thinking_off = {}`), and to think hard and send its thoughts when it asks for some. Both have 250,000 tokens of window, the free tier's tokens a minute for each of them that day, less than their own window of 1,048,576. A comment gives the Gemini models' limits as one free account had them, so that a reader sees why flash-lite is the one for more than a few questions a day. Tests read it and its plans file as they read the repository's own pair.

How it is used, which its files' heads say too: under `make dev`, copied to `config/local/`, which git ignores, with `ACTIO_MODELS_FILE` and `ACTIO_PLANS_FILE` in `.env` pointing at the copies, so that nothing tracked changes. Under `make up`, copied over `config/models.toml` and `config/plans.toml`, which the containers read: the image names those, and the stack passes on neither setting. That changes two tracked files, and the repository's own tests read `config/` as it is shipped, so they are run with the shipped pair back. The page asks the default model and has no way yet to choose another, so a cloud model is chatted with in the page by making it the default in the copy; `GET /api/models` lists them all, and a turn's request may name one.

**Tests:** the client's tests replay cassettes of the two providers, as Ollama's do, from `packages/llm/tests/fixtures/groq/` and `gemini/`: an answer with reasoning and its times, the refusal of `"none"`, a tool call, a model it hasn't got, a made-up key and the list of models of Groq; an answer, reasoning unseen and counted whole, reasoning off, thoughts in several pieces, two tool calls at once, a model it hasn't got, a made-up key to the chat and to the list, and the list of models of the Gemini API. Of those, reasoning unseen and reasoning off are `gemini-3.6-flash`'s, of which the account had 20 requests a day, and the rest `gemini-3.5-flash-lite`'s. A test compares what the client yields with the cassette's lines, read apart from the client. `make test-cloud` sends the same requests to the real servers with the keys of `.env`, about fifteen, and `ACTIO_RECORD_CLOUD=1` records them again, all or one alone (`args="-k …"`). Five exchanges depend on the state of the account or the provider, and were written from the answers above, with a short request in place of a long one where it was long: Groq's 413 and 429, and the Gemini API's spent quota, its 503 before a stream and its 503 in the middle of one. They are never asked live. What is saved is tidied where it is written: no header of the request, and the organization's id, Groq's ids of a request and of a chat completion, and a thought's signature each replaced by a fixed stand-in. The Gemini API's ids of a chat and the tool calls' ids stay: they name no account. A test fails when a cassette holds an organization's id other than the stand-in, a thought's signature longer than it, or a header named `authorization`.

## Consequences

- **Easier:**
    - a model of either provider is a few lines in the models file, with its key in `.env`: the example has four;
    - an answer of the Gemini API's is charged for what it cost, and Groq's say how fast they were written, as Ollama's do;
    - the cassettes hold what the servers really sent, so a change to the client that would misread them fails a test, without a key or a network.
- **Harder:**
    - the client reads shapes it learned from two providers, in a format they each extend; a change on their side shows only when the cassettes are recorded again;
    - the models file has two more settings, and a model on Groq that is left without `thinking_off` fails every turn that asks for no reasoning;
    - a recording run counts against the free tiers, `gemini-3.6-flash`'s 20 requests a day above all, and the five exchanges that depend on the account or the provider can't be recorded on demand. An answer that broke off because its model was too busy is no recording, and is asked again alone.
- **Known, and left:**
    - **a thought's signature isn't sent back with a tool call,** which Gemini 3 asks for once tools are offered. No tool is offered to a model yet;
    - **a prompt larger than a Gemini model's window wasn't provoked,** so its refusal isn't known, and isn't recognised as an overflow;
    - **the Gemini API's refusal at a limit a minute was seen only for a model beyond the free tier,** whose limits are 0;
    - **Groq's message names the account's organization.** That is one more reason why a model error's text never reaches a log or a reader, and the cassettes hold a stand-in for it;
    - **a model that always reasons can't write a title.** A title gets room for 24 tokens ([ADR 0025](0025-titles-written-by-a-model.md)), and the thoughts of Groq's `openai/gpt-oss-20b` took all of it, told `"low"`: none of five titles came with 24 tokens, four with 64 and five with 128, of 46 to 89 tokens each. The conversation keeps its first line, with a warning in the worker's log. The example leaves titles with the shipped model, and says so; more room for a title from such a model is a change to the title job;
    - **the page's tokens a second is too high for an answer of the Gemini API's that reasoned.** The API says nothing of where the time went, so the page divides the answer's tokens by the time from its first piece to its end ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md), revision "an answer says what it took"), and the Gemini API reasons before it sends a first piece: an answer of 1,071 tokens that took 3.9 s from the question, 2.0 s of them before the first thought arrived, read 544 tokens a second, where 270 is nearer. Groq's rate is its server's own. Saying it right takes the count of the tokens that came before the first piece, from the client to the page;
    - **`gemini-3.5-flash-lite` was too busy for most of its answers with reasoning that day:** 11 of 17 were refused or broke off, 8 of them in the middle of the thoughts, at each level of thinking that was tried, and none of 12 answers without reasoning. The page says that the answer broke off, or that the model's server is too busy, and offers another try; an answer that broke off after its first piece is past the point where a second provider could take over;
    - a chain of providers, falling back from one to another, trying again, and the cloud's own timeouts come in the next steps.
- **How to reverse it:** the differences are each a few lines in one module, behind the same `ChatProvider` interface. An adapter of the Gemini API's own format would replace the Gemini models' provider kind in the catalog, and leave Groq's as it is.

## Measured

On 2026-11-25, with the maintainer's two free keys. The rows of the tables above are from some forty single requests to the two servers, most of them made before anything was built. A first recording run (`ACTIO_RECORD_CLOUD=1 make test-cloud`), at 09:15 UTC by the answers' own times, then made the fifteen exchanges that are asked live in 15.9 s, every test passing on its first answer; its cassette of thoughts was `gemini-3.6-flash`'s, and was replaced. Once that model's 20 requests a day were known, the thoughts were asked of `gemini-3.5-flash-lite` alone (`args="-k sends_its_thoughts"`), three times, the last at 09:38 UTC: the first two broke off in the middle of the thoughts, after one piece and after two, each with the 503 of plain lines above, and the third is the cassette. The recordings took 3 requests to `gemini-3.6-flash` in all.

What the cassettes hold, and three of the single requests beside them:

| Exchange | What came back |
|---|---|
| Groq, `openai/gpt-oss-120b`, told `"low"`, the capital of France | Four pieces of reasoning, then the answer in seven pieces. Prompt 96 tokens, completion 21, of which 5 reasoning, total 117. In seconds: queue 0.053, prompt 0.014, completion 0.044, total 0.059; to the client, 14 ms to read, 44 to write and 111 in all, so 477 tokens a second |
| Groq, the same model, asked to call the weather tool for Paris and for Rome at once | Reasoning, then one call, for Paris alone, with `index: 0`, and `finish_reason: "tool_calls"`. Prompt 151, completion 39, of which 12 reasoning |
| Groq, `"none"` | 400, as above |
| Groq's list of models | 11 models, the two above among them |
| The Gemini API, `gemini-3.5-flash-lite`, the capital of France | The answer in two pieces. Prompt 24, completion 7, total 31: no reasoning |
| `gemini-3.6-flash`, asked when a train that leaves at 9:40 and takes 95 minutes arrives, with reasoning and nothing more | The answer in one piece, no thought sent. Prompt 38, completion 11, total 204: 166 tokens counted, where `completion_tokens` said 11 |
| The same, with `"none"` | Prompt 38, completion 11, total 49: no reasoning |
| `gemini-3.6-flash`, a seating puzzle, told to send its thoughts: a single request, no cassette | Three thought pieces, the first opened with `<thought>`, then the answer in eight pieces, the first opened with `</thought>`. Prompt 69, completion 173, total 1,584: 1,515 tokens counted |
| `gemini-3.5-flash-lite`, the same, told to think hard and send its thoughts, the third time | Four thought pieces, the first opened with `<thought>`, then the answer in twelve pieces, the first opened with `</thought>`. Prompt 69, completion 276, total 1,956: 1,887 tokens counted, where `completion_tokens` said 276. A right seating, every rule checked |
| `gemini-3.5-flash-lite`, the same puzzle and the same telling, with room for 64 tokens: a single request, no cassette | `finish_reason: "length"` after 3 tokens of answer, and no thought sent. Prompt 69, completion 3, total 129: 60 tokens counted, 57 of them reasoning. So the output limit holds the reasoning too, and an answer can't cost more than its limit |
| `gemini-3.5-flash-lite`, the two cities | Two chunks, a whole call in each, with ids of their own and no `index`, then `finish_reason: "stop"` |
| Groq, `openai/gpt-oss-20b`, a prompt of 1,817 tokens with room for 7,000 more, 8,817 in all against 8,000 a minute: a single request, no cassette | Answered. The same model refused a prompt of 5,035 tokens while 3,171 of the minute's were used. So Groq admits a request by its prompt alone, by a count of its own (it said 5,055 and 5,085 of that prompt), and takes what the answer used afterwards. The request before that one had used 5,050 tokens, 1,792 of them served from Groq's cache, and under a second later 3,171 counted as used: what the cache serves seems not to count |
| The Gemini API's list of models | 65, each named `models/…` |

**Mistakes planted on purpose.** After the first build, 64 mistakes were planted one at a time in a scratch copy of the tree, and the tests that should notice each were run: 52 failed a test. Of the other twelve, one was no mistake (a line that no 5xx reaches), one was never planted, one changes only the provider's name in an error's text, which nothing reads, and nine were rules that no test held:

- a piece marked otherwise than as a thought stays text;
- a call without an `index` that comes after the calls were given out;
- a wait in a refusal's body that isn't plain seconds;
- the bound on the lines kept from a stream, and the comments that aren't among them;
- the short patience with which a cloud model's status is asked;
- what is taken out of a recording as it is written, the organization's id and a thought's signature;
- the replay's check that the client still sends the recorded request.

Each got a test, in one round of fixes with the review's findings. The review's own must-fix was a time in a usage too large to hold, which raised an error that is no `LLMError`: the range check above is its fix. On the final tree, 71 mistakes were planted and 70 failed a test: the provider's name in an error's text is the one left.

**On the real page,** the whole stack built from this tree, with the example's files and a cloud model as the default, one guest in a real browser:

| What was asked | What the page showed |
|---|---|
| Groq's `openai/gpt-oss-120b`, the capital of France, no reasoning asked for | A short thought, folded, and the answer, 0.9 s after the question. Under it "first word after 330 ms" and 52 credits; under the box "134 of 8,000 tokens". 121 tokens read and 21 written |
| The same model, 150 words on a derailleur, reasoning asked for, told `"high"` | The answer cut short after about 100 words, with "The answer reached the longest a single answer may be": 2,048 tokens written in 4.3 s, 7,799 characters of them thoughts that counted the answer's words one by one. "475 tokens a second" |
| The same, told `"medium"`, twice | Whole answers, of 222 and 267 tokens, in 0.9 s each: "473 tokens a second" and "467 tokens a second" |
| `gemini-3.5-flash-lite`, the capital of France, no reasoning | The answer and no thought, "first word after 2.4 s", 22 credits, "64 of 250,000 tokens". The conversation's title, written by the same model in a title's 24 tokens: "Capital of France" |
| The same model, the seating puzzle, reasoning asked for, six times | Four times "The answer broke off", with the thoughts so far kept; once "The model's server can't be reached, or is too busy", before anything came; once the thoughts, folded, and the answer, "Eli, Dua, Ada, Ben, Cem", with no tag in either: 108 tokens read, 1,071 written |
| Groq's `openai/gpt-oss-20b` as the model for titles | No title: "its model wrote nothing to make a title of" in the worker's log, and the conversation kept its first line |

No request of the page's was refused, and no script on it failed, in any of them.
