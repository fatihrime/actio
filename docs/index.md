# Actio

Actio is a self-hostable, local-first AI platform. It will combine:

- a chat assistant that runs on local models through Ollama, with cloud models as an option;
- a gateway compatible with OpenAI's and Anthropic's APIs, so coding agents can use the same models;
- usage limits inspired by Claude's, whose correctness is tested.

!!! info "Status: Phase 1, local chat and usage limits"

    The chat works on a local model: `make up`, open the page, and ask. Answers are formatted, conversations are kept and listed, and a guest can sign up. Every answer is held to [usage limits](getting-started.md#usage-limits), counted by a [tested engine](reference/quota.md), and the page shows how much is used and what to do at a limit. An answer is [written to its end](adr/0022-turns-that-run-apart-from-their-request.md) whether or not its page stays open, and a page that comes back to it, after a reload or from another window, reads along. A gate in front of the local model comes next, then the gateway; see the [roadmap](https://github.com/fatihrime/actio#roadmap).

## Where to start

- **[Getting started](getting-started.md):** run Actio with one command, or set it up for development.
- **[Architecture](architecture.md):** how the pieces fit together, today and as planned.
- **[Development](development.md):** the toolchains, checks, tests and conventions.
- **[How AI is used](ai-workflow.md):** how an AI assistant helps build Actio, and how its work is checked.
- **[Decisions](adr/index.md):** the architecture decision records, with the options weighed for each.
