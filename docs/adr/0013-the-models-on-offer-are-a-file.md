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
