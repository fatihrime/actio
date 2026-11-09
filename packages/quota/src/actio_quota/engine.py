"""The production quota engine: the Lua scripts in `lua/`, run atomically by Valkey.

It takes the same arguments and returns the same results as `QuotaOracle`, and the
differential tests require it to. Each operation is one script call, so it is atomic
across every API and worker process sharing the Valkey server.

The scripts are sent once with SCRIPT LOAD and then called by their hash (EVALSHA); if
Valkey restarts and forgets them, the client sends them again.
"""

from collections.abc import Iterable, Sequence
from importlib.resources import files
from typing import Any

from valkey.asyncio import Valkey

from actio_quota.cost import MAX_CREDITS, check_count
from actio_quota.model import (
    DONE_TTL_MS,
    GC_BATCH,
    WARN_THRESHOLDS,
    Bucket,
    BucketView,
    Denied,
    Estimate,
    Finished,
    Fixed,
    Granted,
    LedgerEntry,
    NeedsRebuild,
    Outcome,
    ReserveResult,
    Rolling,
    UsageWarning,
    check_name,
    check_rid,
)
from actio_quota.state import BucketState, check_buckets, current, dump_state, rebuilt_buckets

IDLE_TTL_MS = 8 * 24 * 3_600_000
"""A user's keys vanish together after this long without a write; the next request
rebuilds them from the ledger. Longer than a week, so a weekly window survives."""


def _script(name: str) -> str:
    """A script with its constants and the shared prelude in front."""
    lua = files("actio_quota") / "lua"
    constants = (
        f"local WARN = {{{', '.join(map(str, WARN_THRESHOLDS))}}}\n"
        f"local GC_BATCH = {GC_BATCH}\n"
        f"local DONE_TTL = {DONE_TTL_MS}\n"
        f"local IDLE_TTL = {IDLE_TTL_MS}\n"
    )
    return constants + (lua / "common.lua").read_text() + (lua / f"{name}.lua").read_text()


def _str(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _grant(rid: str, parts: Sequence[Any]) -> Granted:
    """Parse credits, units, then name/start pairs."""
    values = [_str(p) for p in parts]
    windows = {values[i]: int(values[i + 1]) for i in range(2, len(values), 2)}
    return Granted(rid, int(values[0]), int(values[1]), windows)


class ValkeyQuota:
    """The quota engine, for any number of users, backed by one Valkey database."""

    def __init__(self, client: Valkey, *, prefix: str = "q") -> None:
        check_name("key prefix", prefix)
        self._client = client
        self._prefix = prefix
        self._scripts = {
            name: client.register_script(_script(name))
            for name in ("reserve", "finish", "extend", "collect", "rebuild")
        }

    def _keys(self, user_id: str) -> list[str]:
        check_name("user id", user_id)
        tag = f"{self._prefix}:{{u:{user_id}}}"
        return [f"{tag}:{part}" for part in ("meta", "b", "rsv", "rsv_exp", "done", "done_exp")]

    async def _run(self, name: str, user_id: str, *args: int | str) -> Any:
        return await self._scripts[name](keys=self._keys(user_id), args=list(args))

    # --- Operations ------------------------------------------------------------------

    async def reserve(
        self,
        user_id: str,
        rid: str,
        buckets: Sequence[Bucket],
        estimate: Estimate,
        *,
        ttl_ms: int,
        now_ms: int,
    ) -> ReserveResult:
        """Admit a request if it fits in every bucket. See `QuotaOracle.reserve`."""
        check_rid(rid)
        check_count("ttl_ms", ttl_ms)
        check_buckets(buckets)
        args: list[int | str] = [
            now_ms,
            rid,
            ttl_ms,
            estimate.base_micro,
            estimate.unit_micro,
            estimate.min_units,
            estimate.max_units,
            len(buckets),
        ]
        for b in buckets:
            match b.window:
                case Rolling(duration_ms=duration):
                    args += [b.name, b.limit, "r", duration, 0]
                case Fixed(start_ms=start, end_ms=end):
                    args += [b.name, b.limit, "f", start, end]
        reply = await self._run("reserve", user_id, *args)
        match _str(reply[0]):
            case "granted":
                return _grant(rid, reply[1:])
            case "denied":
                retry_at = int(reply[1])
                names = tuple(_str(n) for n in reply[2:])
                return Denied(rid, names, None if retry_at < 0 else retry_at)
            case _:
                return NeedsRebuild()

    async def settle(self, user_id: str, rid: str, credits: int, *, now_ms: int) -> Finished:
        """Record what an admitted request actually cost. See `QuotaOracle.settle`."""
        check_count("credits", credits, MAX_CREDITS)
        return await self._finish(user_id, rid, Outcome.SETTLED, credits, now_ms)

    async def release(self, user_id: str, rid: str, *, now_ms: int) -> Finished:
        """Give back an admitted request's held credits without charging anything."""
        return await self._finish(user_id, rid, Outcome.RELEASED, 0, now_ms)

    async def _finish(
        self, user_id: str, rid: str, outcome: Outcome, credits: int, now_ms: int
    ) -> Finished:
        check_rid(rid)
        reply = await self._run("finish", user_id, now_ms, rid, str(outcome), credits)
        warnings = tuple(
            UsageWarning(_str(reply[i]), int(reply[i + 1])) for i in range(2, len(reply), 2)
        )
        return Finished(Outcome(_str(reply[0])), bool(reply[1]), warnings)

    async def extend(self, user_id: str, rid: str, *, ttl_ms: int, now_ms: int) -> bool:
        """Keep a running request's reservation alive until `now_ms + ttl_ms`."""
        check_rid(rid)
        check_count("ttl_ms", ttl_ms)
        return bool(await self._run("extend", user_id, now_ms, rid, ttl_ms))

    async def collect_expired(self, user_id: str, *, now_ms: int) -> int:
        """Release up to `GC_BATCH` expired reservations, as `reserve` does first."""
        return int(await self._run("collect", user_id, now_ms))

    async def rebuild(
        self,
        user_id: str,
        buckets: Sequence[Bucket],
        ledger: Iterable[LedgerEntry],
        *,
        now_ms: int,
        if_epoch: int | None,
    ) -> bool:
        """Replace the user's counters with totals from the ledger, if the epoch matches."""
        states = rebuilt_buckets(buckets, ledger, now_ms)
        args: list[int | str] = ["" if if_epoch is None else if_epoch, len(states)]
        for name, s in states.items():
            warned = ",".join(map(str, sorted(s.warned)))
            args += [name, s.start_ms, s.reset_ms, s.limit, s.used, warned]
        return bool(await self._run("rebuild", user_id, *args))

    # --- Reading ---------------------------------------------------------------------

    async def _read(self, *commands: tuple[str | int, ...]) -> list[Any]:
        """Run read commands in one MULTI/EXEC transaction, so they see one moment."""
        async with self._client.pipeline(transaction=True) as pipe:
            for command in commands:
                pipe.execute_command(*command)
            return list(await pipe.execute())

    async def snapshot(
        self, user_id: str, buckets: Sequence[Bucket], *, now_ms: int
    ) -> tuple[BucketView, ...] | None:
        """The buckets as of `now_ms`, for the usage meter. `None` means: rebuild first."""
        check_buckets(buckets)
        meta, raw = self._keys(user_id)[:2]
        epoch, fields = await self._read(("hget", meta, "epoch"), ("hgetall", raw))
        if epoch is None:
            return None
        stored = _buckets(fields)
        return tuple(current(stored.get(b.name), b, now_ms).view() for b in buckets)

    async def epoch(self, user_id: str) -> int | None:
        (value,) = await self._read(("hget", self._keys(user_id)[0], "epoch"))
        return None if value is None else int(value)

    async def dump(self, user_id: str, *, now_ms: int) -> dict[str, Any] | None:
        """The user's stored state as plain data (see `state.dump_state`)."""
        meta, raw, rsv, rsv_exp, done, done_exp = self._keys(user_id)
        epoch, fields, grants, expiries, markers, forgets = await self._read(
            ("hget", meta, "epoch"),
            ("hgetall", raw),
            ("hgetall", rsv),
            ("zrange", rsv_exp, 0, -1, "WITHSCORES"),
            ("hgetall", done),
            ("zrange", done_exp, 0, -1, "WITHSCORES"),
        )
        expiries, forgets = _pairs(expiries), _pairs(forgets)
        if epoch is None:
            return None
        rsv_expires = {_str(rid): int(score) for rid, score in expiries}
        done_expires = {_str(rid): int(score) for rid, score in forgets}
        reservations = {
            _str(rid): (_grant(_str(rid), _str(g).split("|")), rsv_expires[_str(rid)])
            for rid, g in grants.items()
        }
        finished = {
            _str(rid): (Outcome(_str(m).split("|", 1)[0]), done_expires[_str(rid)])
            for rid, m in markers.items()
        }
        return dump_state(
            epoch=int(epoch),
            buckets=_buckets(fields),
            reservations=reservations,
            done=finished,
            now_ms=now_ms,
        )


def _pairs(flat: Any) -> list[tuple[Any, float]]:
    """ZRANGE ... WITHSCORES as (member, score) pairs, whatever the client returned."""
    items = list(flat)
    if items and isinstance(items[0], (list, tuple)):
        return [(m, float(s)) for m, s in items]
    return [(items[i], float(items[i + 1])) for i in range(0, len(items), 2)]


def _buckets(fields: Any) -> dict[str, BucketState]:
    """Parse the buckets HASH: `<name>/<field>` -> value."""
    by_name: dict[str, dict[str, str]] = {}
    for key, value in fields.items():
        name, _, part = _str(key).rpartition("/")
        by_name.setdefault(name, {})[part] = _str(value)
    return {
        name: BucketState(
            start_ms=int(f["start"]),
            reset_ms=int(f["reset"]),
            limit=int(f["limit"]),
            used=int(f["used"]),
            reserved=int(f["reserved"]),
            warned={int(p) for p in f["warned"].split(",") if p},
        )
        for name, f in by_name.items()
    }
