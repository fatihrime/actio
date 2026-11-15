"""Usage limits: what a model's answer costs, and how much each account may use.

- `plans`: the prices and the plans, read from `config/plans.toml`.
- `models`: the ledger's table, the lasting record of what was used.
- `ledger`: writing to the ledger, and reading it back.
- `limits`: holding credits before a request runs, and counting what it used.

The counting itself is done by the engine in `packages/quota`. This module is where it
meets the application: its plans, its database and its Valkey.
"""
