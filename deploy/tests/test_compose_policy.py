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
        for problem in hardening_problems(svc)
    ]

    assert problems == []


def hardening_problems(svc: dict[str, Any]) -> list[str]:
    failed = {
        "runs as root": svc.get("user", "root") in {"", "root", "0", "0:0"},
        "has a writable root filesystem": svc.get("read_only") is not True,
        "keeps Linux capabilities": svc.get("cap_drop") != ["ALL"],
        "can gain new privileges": "no-new-privileges:true" not in svc.get("security_opt", []),
        "has no memory limit": not svc.get("mem_limit"),
        "has no health check": "test" not in svc.get("healthcheck", {}),
    }
    return [problem for problem, is_failed in failed.items() if is_failed]


def test_dev_overlay_publishes_ports_only_on_localhost() -> None:
    dev = resolved_config("compose.yaml", "compose.dev.yaml")
    host_ips = {
        port["host_ip"] for svc in dev["services"].values() for port in svc.get("ports", [])
    }

    assert host_ips == {"127.0.0.1"}
