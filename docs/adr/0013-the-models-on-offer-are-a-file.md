# 0013. The models on offer are a file

- **Status:** accepted
- **Date:** 2026-11-11

## Context

Actio can talk to a model through two clients: one for Ollama, one for any server that speaks OpenAI's Chat Completions API ([ADR 0007](0007-local-models-through-ollamas-native-api.md)). Before the chat can use them, something has to say which models there are and where each one runs.

- **It differs per installation.** On a 16 GB laptop it is one small model on Ollama. Someone else has LM Studio, or a workstation with a larger model.
- **Conversations and usage limits need a name that lasts.** If a conversation remembers "qwen3.5:4b-q4_K_M", replacing that model orphans the name.
- **What a model can do differs too.** Asking a model to reason when it can't is a failed request: Ollama answers 400.
- **A model can be absent.** The list may name a model that was never downloaded, on a server that isn't running. People should learn that before their first message fails.

## Options considered

**Where the list lives:**

1. **In the code.** Changing a model means changing Actio.
2. **In environment variables.** Fine for one model. A list of models, each with a server, a size and limits, doesn't fit in a line of text.
3. **In a file.**
    - Pro: readable as a whole, with comments, and kept in version control.
    - Con: a change needs a restart.
4. **In the database,** edited on an admin page.
    - Pro: no restart, no file access needed.
    - Con: needs the admin pages first, and the first start needs a model before anyone can open them.

**The file's format:**

1. **YAML.** Common for configuration. Its parser is a dependency, and it guesses types: `no` is false, `3.10` is a number.
2. **TOML.** Read by Python's standard library, and every value's type is written out.
3. **JSON.** No comments.

**How Actio knows what a model can do:**

1. **Written in the file.** Then the file can be wrong, and nothing notices until a request fails.
2. **Asked from the server,** where the server can say.

**Where Ollama runs:**

1. **In a container,** with the rest of the stack. On macOS a container can't use the graphics processor, as Ollama's own documentation says, so the model would run on the processor alone.
2. **On the machine itself,** as Ollama's own installer sets it up.

## Decision

**A TOML file, `config/models.toml`.** Models are asked for by the names it gives them. What a model can do is asked from its server.

**The file:**

- **A model has a name of Actio's own,** such as `standard`. Everything else asks for a model by that name. `default` says which is used when none is named.
- **A provider is a server that runs models,** of one of two kinds:
    - `ollama`: its address is `ACTIO_OLLAMA_URL`, not in the file, because it depends on where Actio itself runs: `127.0.0.1` natively, `host.docker.internal` from a container;
    - `openai-compatible`: its `url` is in the file. If it needs a key, the file names the environment variable that holds it. The key itself is never in the file.
- **Each model says** its provider, its name there, its context window, and the most tokens one answer may have.
- **The file is read when the API starts.** A mistake stops it, with one line that names the file and the fault: a misspelt setting, a model on a provider that isn't listed, an answer as large as the whole window. An unknown setting is a mistake too, so a typo can't be silently ignored.
- `ACTIO_MODELS_FILE` says where the file is. The migration job never reads it.

**What a model can do, and whether it is there:**

- **Ollama is asked.** Whether a model can reason before it answers comes from the server (`/api/show`), so the file can't claim it wrongly. For an `openai-compatible` server, which can't be asked, the file says `thinking = true`.
- **`GET /api/models` lists the models with a status each:** `ready`, `missing` (the server answers, without this model) or `unreachable`. A server that is away is an answer, not an error.
- **An answer is reused for 15 seconds,** so a page can ask often.
- Checked against Ollama 0.40.16 on 2026-11-11, through the catalog:

    | Model | Status | Can reason |
    |---|---|---|
    | `qwen3.5:4b-q4_K_M` | ready | yes |
    | `qwen3.5:4b` (the MLX build) | ready | yes |
    | `granite4:micro` | ready | no |
    | `qwen3.5:9b`, not downloaded | missing | |

**In the container stack:**

- **Ollama runs on the machine itself.** The API's container reaches it as `host.docker.internal`.
- **The image carries a copy of the file,** so it starts on its own. `compose.yaml` mounts `./config` over it, read-only: edit the file and restart the API, without a rebuild.

**In the code,** `actio.catalog` reads the file and builds the clients. The rest of Actio gets an `OfferedModel`: the name to send, the limits, and the client to stream through.

## Consequences

- **Easier:**
    - the model behind `standard` can change, and nothing that asked for `standard` has to know;
    - another server, such as LM Studio or vLLM, is a few lines in the file;
    - an interface can say "run `ollama pull …`" or "start Ollama" before the first message, instead of failing at it.
- **Harder:**
    - a change to the file needs a restart of the API;
    - a provider's address is in one of two places: Ollama's in the environment, the others' in the file;
    - on Linux, Ollama listens only on `127.0.0.1` by default, which a container can't reach. It has to be told to listen on an address the container can reach (`OLLAMA_HOST`), and that address must not be open to the network.
- **Not done yet:**
    - **nothing streams an answer through the catalog yet.** Turns do, in the next step;
    - one model per name, with no second choice when its server is away. Routing between providers comes with the router;
    - which accounts may use which model, and what a model costs in credits, come with the usage limits.
- **How to reverse it:** the rest of Actio depends on `Catalog` and `OfferedModel`, not on the file. Keeping the list in the database instead changes `actio.catalog`, and nothing above it.

## Revisions

### 2026-11-14: a model has a price, in a file of its own

"What a model costs in credits" was left for the usage limits above. It is decided in [ADR 0020](0020-usage-limits-on-turns.md):

- **Prices are in a second file, `config/plans.toml`,** under the names the models have here. The models file says where a model runs and what it can hold; the plans file says what using it costs and how much each kind of account may use. They change for different reasons, and are read by different parts of the code.
- **The two are checked against each other when the API starts.** A model without a price stops the start, and so does a price for a model that isn't offered. So offering a model now means editing two files.
- Which accounts may use which model is still open: every account may use every model.

### 2026-11-18: a provider says how many answers it writes at once

A provider has one setting more, `answers_at_once`: how many answers its server writes at the same time, across all the models it runs ([ADR 0023](0023-answers-that-wait-their-turn.md)).

- **An answer that would be one more waits inside Actio,** at a gate that every model of that provider shares, and the server is asked only once it has room.
- **Left out, it is 1 for `ollama`, and there is no limit for `openai-compatible`,** which may batch many. A server started to write several at once is given its number.
- **Like every setting, it is checked when the API starts:** 0, or a number below it, stops the start with one line.
- `OfferedModel` carries its provider's gate, or none. The rest of Actio still sees only `Catalog` and `OfferedModel`.

### 2026-11-20: the file names the model that writes titles

The file has one more top-level key beside `default`: `titles`, the model that names a conversation after its first answer ([ADR 0025](0025-titles-written-by-a-model.md)).

- **Checked like `default`:** a name that isn't one of the models stops the start, with one line.
- **Left out, no model writes titles,** and a conversation keeps its first question's first line. The repository's file names `standard`; the end-to-end stack's leaves it out.
- **The worker reads the file now too,** for that one job, and its container mounts the folder the API's does. A mistake in the file stops it as it stops the API; the migration job still never reads it.
- `Catalog.titles` carries the name. The rest of Actio still sees only `Catalog` and `OfferedModel`.

### 2026-11-25: a model names those that stand in for it

"One model per name, with no second choice when its server is away" was left under "Not done yet". A model's table may now have `then`, a list of other models of the file, asked in its place, in order, when it can't answer before the first word of an answer ([ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md)).

- **A stand-in is a model on offer of its own,** with its own server, window, limits and price. Only the asked model's list is followed.
- **Checked like the other settings:** a name that isn't one of the models, the model's own name, or a name twice stops the start, with one line.
- **`GET /api/models` lists each model's `then`,** and says a model is `ready` when it or one of its stand-ins is.
- `OfferedModel.then` carries the names, and `Catalog.chain` gives a model with its stand-ins. The rest of Actio still sees only `Catalog` and `OfferedModel`.

### 2026-11-25: a model says how many tokens its server takes in a minute

The example for the cloud providers' free plans wrote what a plan takes in a minute in the place of a model's `context_window`, since the file had no other place for it. A model's table may now have `tokens_a_minute` ([ADR 0029](0029-a-model-is-passed-over-for-a-request-too-large.md)).

- **A model's room for one request is the smaller of its window and that figure.** A model whose room is smaller than a request, by Actio's guess at its tokens, is passed over while another model of its chain has room for it.
- **Checked like the other settings:** a whole number of 1 or more, and anything else stops the start, with one line. Left out, the model's room is its window.
- **`GET /api/models` gives a model's room as its `context_window`.**
- `OfferedModel.tokens_a_minute` and `OfferedModel.room` carry it, and `Chain.asked_with` is the rule.

### 2026-11-25: a model states its provider's limits, its budget

Beside `tokens_a_minute`, a model's table may have three more keys of its provider's limits for it: `requests_a_minute`, `requests_a_day` and `tokens_a_day` ([ADR 0030](0030-the-providers-budgets-are-counted.md)).

- **The four are the model's budget.** Actio counts in Valkey what it sends to the model, keeps to nine tenths of each figure, and passes over a ready model that has nothing left of one for a request while another model of its chain has some. When none has, each is asked all the same.
- **Checked like `tokens_a_minute`:** a whole number of 1 or more, and anything else stops the start, with one line. Any may be left out, and a model that states none has no budget.
- **Two models of the file that are the same model on the same provider share one budget,** and must state the same four figures, a key left out being one: a difference stops the start, with one line that names both models and the key.
- `OfferedModel.budget` carries it, `Chain.look` reads it, `Chain.asked_with` is the rule, and `counted` counts what a request takes.

### 2026-11-26: a model without budget is asked last, and a request no model has budget for is refused for now

"Passes over a ready model that has nothing left", and "when none has, each is asked all the same", in the revision above, are no longer what happens. A ready model without budget for a request is asked after those of its chain that have some. And when some ready models have room for the request and none of those has budget left for it, the question is refused for now, with how long to wait; a title is asked for again later. The file's keys and what is kept of each figure are as they were ([ADR 0030](0030-the-providers-budgets-are-counted.md), revision of 2026-11-26).

### 2026-11-26: a file the TOML parser gives up on is one line too

"A mistake stops it, with one line that names the file and the fault" didn't hold for three files. The reader (`actio.core.config_file.read`, which reads the plans file too) caught the parser's error for a mistake in TOML, and nothing else the parser raises:

- **bytes that aren't UTF-8,** as an editor that saves in another encoding leaves them, an `é` in a comment being enough;
- **a whole number of more than 4,300 digits,** which Python refuses to read;
- **lists or tables nested thousands deep:** 3,000 were tried.

Seen on 2026-11-26, with a file of each kind: the API's start ended with a trace of 22, 35 and 2,509 lines, and status 1. Now each is one line, and status 2, as every other mistake is: "… isn't valid TOML: it isn't UTF-8, from its byte 26 on", "… isn't valid TOML: Exceeds the limit (4300 digits) for integer string conversion: value has 5000 digits", "can't read the models file '…': it is nested too deep". A test reads each (`test_catalog.py`).

**Why it surfaced only now:** the reader's tests wrote their files as text, so none was ever in another encoding, and the two others take a file nobody writes by hand. `make preflight` began to read the models file with this reader ([ADR 0030](0030-the-providers-budgets-are-counted.md), revision "`make preflight` holds the models file against what the providers state"), and its build tried files that can't be read.
