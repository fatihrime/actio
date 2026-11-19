"""Security rules for the containers, checked on the configuration Compose resolves.

compose.yaml is the production-shaped stack behind `make up`, and must follow every
rule. compose.dev.yaml, the overlay behind `make dev`, publishes the data services'
ports for the native processes, so it is held to the rule that matters there: those
ports listen on 127.0.0.1 only. compose.e2e.yaml, the overlay behind `make e2e`, adds a
fake model as one more container: the stack it makes is held to every rule again, so
what the end-to-end tests drive is locked down like what people run.
"""

import json
import os
import re
import subprocess
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from actio.api.turns import MAX_QUESTION_CHARS
from actio.chat.running import GRACE, LAST_WORDS
from actio.cli import WAITS_FOR_REQUESTS_S
from actio.core import alive, jobs

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = [
    "apps/api/Dockerfile",
    "apps/web/Dockerfile",
    "deploy/caddy/Dockerfile",
    "packages/fakellm/Dockerfile",
]
DATA_SERVICES = ["postgres", "valkey"]
# Containers that do one thing and exit. Listed by name on purpose: adding one here is
# a decision, because a job is exempt from the health check rule.
ONE_SHOT_JOBS = {"migrate"}


def resolved_config(*files: str) -> dict[str, Any]:
    """`docker compose config` for these files, with placeholder passwords."""
    command = ["docker", "compose", "--env-file", ".env.example"]
    for name in files:
        command += ["--file", name]
    env = os.environ | {"POSTGRES_PASSWORD": "placeholder", "VALKEY_PASSWORD": "placeholder"}
    # The docker CLI, with arguments fixed by this file.
    result = subprocess.run(  # noqa: S603
        [*command, "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    config: dict[str, Any] = json.loads(result.stdout)
    return config


@pytest.fixture(scope="module")
def stack() -> dict[str, Any]:
    return resolved_config("compose.yaml")


def test_only_caddy_publishes_ports_and_only_on_localhost(stack: dict[str, Any]) -> None:
    published = {name: svc["ports"] for name, svc in stack["services"].items() if svc.get("ports")}

    assert list(published) == ["caddy"]
    assert {port["host_ip"] for port in published["caddy"]} == {"127.0.0.1"}


def test_pulled_images_are_pinned_by_digest(stack: dict[str, Any]) -> None:
    unpinned = [
        name
        for name, svc in stack["services"].items()
        if "build" not in svc and "@sha256:" not in svc["image"]
    ]

    assert unpinned == []


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_built_images_start_from_bases_pinned_by_digest(dockerfile: str) -> None:
    bases = re.findall(r"^FROM\s+(\S+)", (ROOT / dockerfile).read_text(), flags=re.MULTILINE)

    assert bases
    assert [image for image in bases if "@sha256:" not in image] == []


@pytest.mark.parametrize("service", DATA_SERVICES)
def test_data_services_sit_only_on_internal_networks(stack: dict[str, Any], service: str) -> None:
    internal = {name for name, network in stack["networks"].items() if network.get("internal")}

    assert set(stack["services"][service]["networks"]) <= internal


def test_every_container_is_locked_down(stack: dict[str, Any]) -> None:
    problems = [
        f"{name}: {problem}"
        for name, svc in stack["services"].items()
        for problem in hardening_problems(svc, is_job=name in ONE_SHOT_JOBS)
    ]

    assert problems == []


def hardening_problems(svc: dict[str, Any], *, is_job: bool = False) -> list[str]:
    failed = {
        "runs as root": svc.get("user", "root") in {"", "root", "0", "0:0"},
        "has a writable root filesystem": svc.get("read_only") is not True,
        "keeps Linux capabilities": svc.get("cap_drop") != ["ALL"],
        "can gain new privileges": "no-new-privileges:true" not in svc.get("security_opt", []),
        "has no memory limit": not svc.get("mem_limit"),
        # A job exits when it is done, so there is nothing to probe: see ADR 0008.
        "has no health check": not is_job and "test" not in svc.get("healthcheck", {}),
    }
    return [problem for problem, is_failed in failed.items() if is_failed]


@pytest.mark.parametrize("job", sorted(ONE_SHOT_JOBS))
def test_a_job_runs_once_and_something_waits_for_it_to_succeed(
    stack: dict[str, Any], job: str
) -> None:
    """The exemption from health checks is only for containers that are really jobs."""
    assert stack["services"][job]["restart"] == "no"
    waiting = [
        name
        for name, svc in stack["services"].items()
        if svc.get("depends_on", {}).get(job, {}).get("condition")
        == "service_completed_successfully"
    ]
    assert waiting, f"nothing waits for {job} to succeed"


def test_the_api_starts_only_after_the_schema_is_migrated(stack: dict[str, Any]) -> None:
    depends_on = stack["services"]["api"]["depends_on"]

    assert depends_on["migrate"]["condition"] == "service_completed_successfully"
    assert stack["services"]["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_the_migration_job_cannot_be_reached_or_reach_out(stack: dict[str, Any]) -> None:
    internal = {name for name, network in stack["networks"].items() if network.get("internal")}

    assert set(stack["services"]["migrate"]["networks"]) <= internal


def test_the_worker_starts_only_after_the_schema_is_migrated(stack: dict[str, Any]) -> None:
    """It checks the schema itself too, and ends with one line when it isn't the one it
    expects. Waiting for the migration means it never has to."""
    depends_on = stack["services"]["worker"]["depends_on"]

    assert depends_on["migrate"]["condition"] == "service_completed_successfully"


def test_the_worker_cannot_be_reached_or_reach_out(stack: dict[str, Any]) -> None:
    """It serves nothing, and needs only the data services. (No port either: the rule
    for every container, above.)"""
    internal = {name for name, network in stack["networks"].items() if network.get("internal")}

    assert set(stack["services"]["worker"]["networks"]) <= internal


def test_only_the_worker_is_started_again_and_only_when_it_failed(stack: dict[str, Any]) -> None:
    """The worker ends when it can't go on, such as 30 seconds after it lost its
    database, with a status that says so. Docker then starts it again, and only then:
    not after it ended well because it was told to stop.

    `on-failure`, and not `always` or `unless-stopped`: those two bring a container
    back whenever Docker itself starts again. Nothing else of the stack comes back
    then, and a worker alone would wait for a database that isn't there. With
    `on-failure` Docker brings back only a container whose last end was a failure, and
    the worker ends well when Docker stops it (both measured: ADR 0024).

    Every other service has no policy: the API is built to stay up without its database,
    and to say so at `/api/readyz`; a job runs once. See ADR 0024."""
    restarting = {
        name: svc["restart"]
        for name, svc in stack["services"].items()
        if svc.get("restart", "no") != "no"
    }

    assert restarting == {"worker": "on-failure"}


def test_the_worker_is_given_the_time_it_takes_to_finish_the_job_in_hand(
    stack: dict[str, Any],
) -> None:
    """Told to stop, the worker gives the job in hand `jobs.SHUTDOWN_GRACE_S` to finish,
    then cuts it off, takes itself off the queue's list of workers and closes its
    connections. Docker kills the container after `stop_grace_period`, ten seconds
    unless the file says otherwise: longer than the grace, with room for what follows
    it, which takes a fraction of a second while the database answers.

    And an init process stands in front of it: a stop that comes before Python listens
    for it, in its first half second, still ends it at once. See ADR 0024."""
    worker = stack["services"]["worker"]
    room_after_the_grace = timedelta(seconds=5)

    assert timedelta(seconds=jobs.SHUTDOWN_GRACE_S) + room_after_the_grace <= docker_waits(worker)
    assert worker["init"] is True


def test_the_worker_is_healthy_while_it_touches_a_file_on_its_tmpfs(
    stack: dict[str, Any],
) -> None:
    """It has no port to probe. While it works it touches the file it is told of, and
    its health check says whether it did so lately (`actio.core.alive`). Its root
    filesystem is read-only, so the file is on a tmpfs of its own."""
    worker = stack["services"]["worker"]
    alive_file = PurePosixPath(worker["environment"][alive.VARIABLE])
    in_memory = [PurePosixPath(mount.split(":")[0]) for mount in worker["tmpfs"]]

    assert worker["healthcheck"]["test"] == ["CMD", "python", "-m", "actio.core.alive"]
    assert any(mount in alive_file.parents for mount in in_memory)


def test_a_client_cannot_choose_the_address_it_is_counted_under(stack: dict[str, Any]) -> None:
    """Rate limits count per client address, which the API reads from `X-Forwarded-For`.

    Here the API trusts that header from whoever reaches it, and takes the first address
    in it. That is sound only because of two other facts, so all three are held together:

    - only Caddy and the web app can reach the API: it publishes no port (checked above)
      and shares a network with nothing else;
    - Caddy writes the header itself. It keeps what a client sent only when
      `trusted_proxies` tells it to, and then the first address is the client's to choose.

    Behind another proxy or a CDN, set `trusted_proxies` in the Caddyfile and replace
    `*` with the proxies' addresses in ACTIO_FORWARDED_ALLOW_IPS, both in one change.
    See ADR 0011.
    """
    api = stack["services"]["api"]
    caddyfile = (ROOT / "deploy/caddy/Caddyfile").read_text()
    sharing_a_network = {
        name
        for name, svc in stack["services"].items()
        if name not in ONE_SHOT_JOBS and set(svc["networks"]) & {"edge"}
    }

    assert api["environment"]["ACTIO_FORWARDED_ALLOW_IPS"] == "*"
    assert "trusted_proxies" not in caddyfile
    assert sharing_a_network == {"caddy", "web", "api"}


@pytest.mark.parametrize(
    "limit",
    [
        "ACTIO_LIMIT_GUESTS_PER_ADDRESS",
        "ACTIO_LIMIT_SIGNUPS_PER_ADDRESS",
        "ACTIO_LIMIT_LOGINS_PER_ADDRESS",
        "ACTIO_LIMIT_LOGINS_PER_EMAIL",
        "ACTIO_LIMIT_TURNS_PER_USER",
    ],
)
def test_a_rate_limit_set_in_env_reaches_the_api(limit: str) -> None:
    """compose.yaml names each setting it passes on, so a new one is easy to forget."""
    env = os.environ | {"POSTGRES_PASSWORD": "placeholder", "VALKEY_PASSWORD": "placeholder"}
    command = ["docker", "compose", "--env-file", ".env.example", "--file", "compose.yaml"]

    def api_environment(**extra: str) -> dict[str, str | None]:
        result = subprocess.run(  # noqa: S603 - the docker CLI, with arguments fixed here
            [*command, "config", "--format", "json"],
            cwd=ROOT,
            env=env | extra,
            capture_output=True,
            text=True,
            check=True,
        )
        environment: dict[str, str | None] = json.loads(result.stdout)["services"]["api"][
            "environment"
        ]
        return environment

    assert api_environment(**{limit: "3/day"})[limit] == "3/day"
    assert api_environment()[limit] is None  # left out: the API's own default applies


def test_the_api_is_given_the_time_it_takes_to_end_its_answers(stack: dict[str, Any]) -> None:
    """Told to stop, the API doesn't drop the answers that are being written. It gives
    them a moment to finish, then halts what is left and records it as interrupted,
    while their readers are still connected. Three waits have to fit inside each other
    for that:

    - the answers' own: `GRACE`, and `LAST_WORDS` to record them;
    - the web server's, for the requests it is answering, readers among them. Longer
      than the first, or a reader is cut off before the answer has ended;
    - Docker's, `stop_grace_period`, after which it kills the container: ten seconds
      unless the file says otherwise. Longer than the second and one more recording,
      or a turn is left `running` with what it had written lost.

    See ADR 0022."""
    answers_take = GRACE + LAST_WORDS
    server_waits = timedelta(seconds=WAITS_FOR_REQUESTS_S)

    assert answers_take < server_waits
    assert server_waits + LAST_WORDS < docker_waits(stack["services"]["api"])


def docker_waits(svc: dict[str, Any]) -> timedelta:
    """How long Docker waits for a container it told to stop, before it kills it."""
    written = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", svc["stop_grace_period"])
    assert written is not None, "written as minutes and seconds, such as 35s or 1m5s"
    return timedelta(minutes=int(written[1] or 0), seconds=int(written[2] or 0))


CADDY_SIZES = {"B": 1, "KB": 1000, "KiB": 1024, "MB": 1000**2, "MiB": 1024**2}
"""What a size in the Caddyfile means, in bytes: Caddy reads `MB` as a million."""


def caddy_block(caddyfile: str, opening: str) -> str:
    """What is inside the block of the Caddyfile that starts with `opening`, up to the
    brace that closes it."""
    start = caddyfile.index(opening) + len(opening)
    depth = 1
    for at in range(start, len(caddyfile)):
        depth += {"{": 1, "}": -1}.get(caddyfile[at], 0)
        if depth == 0:
            return caddyfile[start:at]
    raise AssertionError(f"{opening!r} is never closed")


def test_the_proxy_takes_every_request_the_api_would_and_refuses_much_larger_ones() -> None:
    """The API reads a request's body whole before it can say that it is too large.
    So the proxy refuses a body over a size of its own, for the API's routes, first.

    The limit must let through every request the API takes: the longest question,
    `MAX_QUESTION_CHARS` characters, and the rest of the request around it. A character
    takes at most twelve bytes in JSON: one beyond the first 65,536, written as two
    `\\u` escapes, as a client that escapes everything outside ASCII writes it (Python's
    `json.dumps` does). The API counts it as one character.

    And the refusal is a problem document, as every error of the API is, so that the
    page has words to show. See ADR 0023."""
    room_for_the_rest = 64 * 1024  # ids, a model's name, a flag, 16 parts' keys: far less
    longest_character = len(json.dumps("\U0001f6b2")) - 2  # a bicycle: two escapes
    caddyfile = (ROOT / "deploy/caddy/Caddyfile").read_text()
    api_routes = caddy_block(caddyfile, "handle @api {")
    [(number, unit)] = re.findall(
        r"request_body\s*\{\s*max_size\s+(\d+)([KM]?i?B)\s*\}", api_routes
    )
    refusal = caddy_block(caddyfile, "handle_errors 413 {")
    [body] = re.findall(r"respond `(.+)` 413", refusal)
    problem = json.loads(body)

    assert longest_character == 12
    assert (
        int(number) * CADDY_SIZES[unit]
        >= longest_character * MAX_QUESTION_CHARS + room_for_the_rest
    )
    assert caddyfile.count("request_body") == 1  # the web app's own routes are its own
    assert "header Content-Type application/problem+json" in refusal
    assert set(problem) == {"type", "title", "status", "detail"}
    assert (problem["type"], problem["status"]) == ("about:blank", 413)


def test_the_proxy_runs_the_config_it_was_built_with(stack: dict[str, Any]) -> None:
    """The proxy's config holds a limit that protects the API: the size of a request. A
    limit that isn't applied protects nothing, and nothing says that it isn't.

    A stack that is updated in place keeps a container running unless its image or its
    settings changed: a file mounted into it may change, or be replaced and lost to it,
    without Compose doing anything. So the config is part of the proxy's image, as code
    is part of the API's. A change to it is a new image, which Compose starts the proxy
    from; nothing is mounted, and nothing watches the file. The build checks the config,
    so a mistake in it stops the build, and the proxy that runs keeps serving. Its base
    image is pinned like every other: see `test_built_images_start_from_bases_pinned_by_digest`.
    See ADR 0023."""
    caddy = stack["services"]["caddy"]
    dockerfile = (ROOT / "deploy/caddy/Dockerfile").read_text()
    lines = [" ".join(line.split()) for line in dockerfile.replace("\\\n", " ").splitlines()]

    assert Path(caddy["build"]["context"]).resolve() == (ROOT / "deploy" / "caddy").resolve()
    assert caddy["build"]["dockerfile"] == "Dockerfile"
    assert caddy.get("image") is None  # what runs is what was built here
    assert not caddy.get("volumes")
    assert caddy.get("command") is None  # the image's own: `caddy run` on its Caddyfile
    # And the base image's own, too: nothing here starts Caddy otherwise, watching the
    # file or reading another.
    assert [line for line in lines if line.startswith(("CMD", "ENTRYPOINT"))] == []
    assert "COPY Caddyfile /etc/caddy/Caddyfile" in lines
    [validate] = [line for line in lines if line.startswith("RUN ")]
    assert validate.endswith("caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile")


def test_valkey_keeps_nothing_on_disk(stack: dict[str, Any]) -> None:
    """The usage limits' counters are rebuilt from the ledger when they are missing. A
    Valkey that restarted with a file from before would hold counters that are there,
    and stale: nothing would rebuild them. So it has no file to come back with: no
    snapshots, no append-only file, and no volume to keep either in. See ADR 0020."""
    valkey = stack["services"]["valkey"]
    command = valkey["command"]

    assert command[command.index("--save") + 1] == ""
    assert command[command.index("--appendonly") + 1] == "no"
    assert not valkey.get("volumes")
    assert valkey["tmpfs"] == ["/data"]  # or Docker would make a volume of its own there
    assert set(stack["volumes"]) == {"postgres-data"}


@pytest.mark.parametrize(
    "setting", ["ACTIO_MODELS_FILE", "ACTIO_PLANS_FILE"], ids=["models.toml", "plans.toml"]
)
def test_the_api_reads_its_two_files_from_a_read_only_mount(
    stack: dict[str, Any], setting: str
) -> None:
    """config/models.toml and config/plans.toml are edited on the host and read in the
    container. The mount must land where the image expects each file, and the container
    must not be able to write them."""
    [mount] = stack["services"]["api"]["volumes"]
    dockerfile = (ROOT / "apps/api/Dockerfile").read_text()
    expected = re.search(rf"{setting}=(\S+)", dockerfile)

    assert expected is not None
    assert mount["type"] == "bind"
    assert Path(mount["source"]) == ROOT / "config"
    assert mount["read_only"] is True
    assert Path(expected[1]).parent == Path(mount["target"])
    assert (ROOT / "config" / Path(expected[1]).name).is_file()
    assert "COPY config /app/config" in dockerfile  # so the image also starts on its own


def test_the_api_finds_ollama_on_the_host_unless_told_otherwise() -> None:
    """Ollama runs on the machine itself. A container's own 127.0.0.1 is the container."""
    env = os.environ | {"POSTGRES_PASSWORD": "placeholder", "VALKEY_PASSWORD": "placeholder"}
    env.pop("ACTIO_OLLAMA_URL", None)
    command = ["docker", "compose", "--env-file", ".env.example", "--file", "compose.yaml"]

    def api(**extra: str) -> dict[str, Any]:
        result = subprocess.run(  # noqa: S603 - the docker CLI, with arguments fixed here
            [*command, "config", "--format", "json"],
            cwd=ROOT,
            env=env | extra,
            capture_output=True,
            text=True,
            check=True,
        )
        service: dict[str, Any] = json.loads(result.stdout)["services"]["api"]
        return service

    by_default = api()
    elsewhere = api(ACTIO_OLLAMA_URL="http://192.168.1.50:11434")

    assert by_default["environment"]["ACTIO_OLLAMA_URL"] == "http://host.docker.internal:11434"
    assert any(host.startswith("host.docker.internal") for host in by_default["extra_hosts"])
    assert elsewhere["environment"]["ACTIO_OLLAMA_URL"] == "http://192.168.1.50:11434"


def test_dev_overlay_publishes_ports_only_on_localhost() -> None:
    dev = resolved_config("compose.yaml", "compose.dev.yaml")
    host_ips = {
        port["host_ip"] for svc in dev["services"].values() for port in svc.get("ports", [])
    }

    assert host_ips == {"127.0.0.1"}


# --- The stack the end-to-end tests drive: compose.yaml with compose.e2e.yaml ---------


@pytest.fixture(scope="module")
def e2e() -> dict[str, Any]:
    return resolved_config("compose.yaml", "compose.e2e.yaml")


def test_the_e2e_stack_is_the_real_one_with_a_fake_model_added(
    stack: dict[str, Any], e2e: dict[str, Any]
) -> None:
    """Tests that pass on another stack than the one people run would prove little."""
    assert set(e2e["services"]) == set(stack["services"]) | {"fakellm"}
    for name in ("caddy", "web", "migrate", "worker", "postgres", "valkey"):
        assert e2e["services"][name] == stack["services"][name]


def test_the_e2e_stack_follows_the_same_rules(e2e: dict[str, Any]) -> None:
    published = {name: svc["ports"] for name, svc in e2e["services"].items() if svc.get("ports")}
    unpinned = [
        name
        for name, svc in e2e["services"].items()
        if "build" not in svc and "@sha256:" not in svc["image"]
    ]
    problems = [
        f"{name}: {problem}"
        for name, svc in e2e["services"].items()
        for problem in hardening_problems(svc, is_job=name in ONE_SHOT_JOBS)
    ]
    on_the_edge = {
        name
        for name, svc in e2e["services"].items()
        if name not in ONE_SHOT_JOBS and set(svc["networks"]) & {"edge"}
    }

    assert list(published) == ["caddy"]
    assert {port["host_ip"] for port in published["caddy"]} == {"127.0.0.1"}
    assert unpinned == []
    assert problems == []
    # The API still trusts the address that only Caddy can have written (see above).
    assert on_the_edge == {"caddy", "web", "api"}


def test_the_fake_model_is_reached_by_the_api_alone_and_has_no_way_out(
    e2e: dict[str, Any],
) -> None:
    internal = {name for name, network in e2e["networks"].items() if network.get("internal")}
    its_networks = set(e2e["services"]["fakellm"]["networks"])
    sharing_one = {
        name for name, svc in e2e["services"].items() if set(svc["networks"]) & its_networks
    }

    assert its_networks <= internal
    assert sharing_one == {"api", "fakellm"}
    assert e2e["services"]["api"]["depends_on"]["fakellm"]["condition"] == "service_healthy"


def test_the_e2e_stack_reads_its_own_files_where_the_api_expects_them(
    stack: dict[str, Any], e2e: dict[str, Any]
) -> None:
    """The overlay swaps the folder that is mounted, not the place it is mounted at.
    What the files say is checked by the API's own tests, which can read them."""
    [usual] = stack["services"]["api"]["volumes"]
    [mount] = e2e["services"]["api"]["volumes"]

    assert Path(mount["source"]) == ROOT / "deploy" / "e2e" / "config"
    assert (Path(mount["source"]) / "models.toml").is_file()
    assert (Path(mount["source"]) / "plans.toml").is_file()
    assert mount["target"] == usual["target"]
    assert mount["read_only"] is True


def test_no_test_meets_a_rate_limit_made_for_people(e2e: dict[str, Any]) -> None:
    """Every end-to-end test is a new guest, and all of them come from one address.
    Some sign up and log in, from that address too."""
    environment = e2e["services"]["api"]["environment"]

    assert environment["ACTIO_LIMIT_GUESTS_PER_ADDRESS"] == "100000/hour"
    assert environment["ACTIO_LIMIT_SIGNUPS_PER_ADDRESS"] == "100000/hour"
    assert environment["ACTIO_LIMIT_LOGINS_PER_ADDRESS"] == "100000/hour"
    assert environment["ACTIO_LIMIT_TURNS_PER_USER"] == "100000/hour"
    # The one limit that guards an account's password is the one people get. A test
    # makes up its own address, so it has that address's attempts to itself.
    assert environment["ACTIO_LIMIT_LOGINS_PER_EMAIL"] is None
