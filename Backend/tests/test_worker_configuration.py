from pathlib import Path

import yaml

from app.config import Settings


REPO_ROOT = Path(__file__).resolve().parents[2]


def _example_environment() -> dict[str, str]:
    lines = (REPO_ROOT / "Backend" / ".env.example").read_text().splitlines()
    return dict(
        line.split("=", maxsplit=1)
        for line in lines
        if line and not line.startswith("#")
    )


def test_worker_defaults_use_standalone_topology_and_ten_minute_lease():
    assert Settings.model_fields["run_worker"].default is False
    assert Settings.model_fields["worker_lease_timeout"].default == 600

    environment = _example_environment()
    assert environment["RUN_WORKER"] == "false"
    assert environment["WORKER_LEASE_TIMEOUT"] == "600"


def test_compose_worker_is_standalone_and_always_restarted():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    worker = compose["services"]["worker"]

    assert worker["command"] == [
        "uv",
        "run",
        "--frozen",
        "python",
        "-m",
        "app.worker",
    ]
    assert worker["restart"] == "always"
    assert worker["environment"]["RUN_WORKER"] == "false"
    assert worker["profiles"] == ["worker"]
