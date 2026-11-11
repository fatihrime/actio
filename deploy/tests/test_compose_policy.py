"""Security rules for the containers, checked on the configuration Compose resolves.

compose.yaml is the production-shaped stack behind `make up`, and must follow every
rule. compose.dev.yaml, the overlay behind `make dev`, publishes the data services'
ports for the native processes, so it is held to the rule that matters there: those
ports listen on 127.0.0.1 only.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = ["apps/api/Dockerfile", "apps/web/Dockerfile"]
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


def test_the_api_reads_the_models_file_from_a_read_only_mount(stack: dict[str, Any]) -> None:
    """config/models.toml is edited on the host and read in the container. The mount must
    land where the image expects the file, and the container must not be able to write it."""
    [mount] = stack["services"]["api"]["volumes"]
    dockerfile = (ROOT / "apps/api/Dockerfile").read_text()
    expected = re.search(r"ACTIO_MODELS_FILE=(\S+)", dockerfile)

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
