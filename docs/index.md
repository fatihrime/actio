# Actio

Actio is a self-hostable, local-first AI platform. It will combine:

- a chat assistant that runs on local models through Ollama, with cloud models as an option;
- a gateway compatible with OpenAI's and Anthropic's APIs, so coding agents can use the same models;
- usage limits inspired by Claude's, whose correctness is tested.

!!! info "Status: Phase 1, local chat and usage limits"

    The repository, tooling, local stack, container images and CI are in place. So is the [usage-limits engine](reference/quota.md), as a tested package that nothing calls yet. The chat that uses it comes next, then the gateway; see the [roadmap](https://github.com/fatihrime/actio#roadmap).

## Where to start

- **[Getting started](getting-started.md):** run Actio with one command, or set it up for development.
- **[Architecture](architecture.md):** how the pieces fit together, today and as planned.
- **[Development](development.md):** the toolchains, checks, tests and conventions.
- **[How AI is used](ai-workflow.md):** how an AI assistant helps build Actio, and how its work is checked.
- **[Decisions](adr/index.md):** the architecture decision records, with the options weighed for each.
