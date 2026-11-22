"""Usage limits: what a model's answer costs, and how much each account may use.

- `plans`: the prices and the plans, read from `config/plans.toml`.
- `models`: the ledger's table, the lasting record of what was used, and the notes of
  what the counters haven't been told of yet.
- `ledger`: writing to the ledger, and reading it back.
- `limits`: holding credits before a request runs, and counting what it used.
- `missed`: telling the counters again of what they missed, as a job of the worker.
- `drift`: comparing the counters with the ledger, and saying how far apart they are,
  as another.

The counting itself is done by the engine in `packages/quota`. This module is where it
meets the application: its plans, its database and its Valkey.
"""
