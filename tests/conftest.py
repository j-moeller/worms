"""Shared fixtures for the offline test suite (`make test`).

Nothing here talks to Proxmox or a VM. Tests that need a tool that is not
installed (ansible-playbook, tofu, PostgreSQL, ip/tc, root) are skipped with
the reason, so the suite runs everywhere and does as much as it can.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def require(*tools: str) -> None:
    """Skip the calling test unless every tool is on PATH."""
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"not installed: {', '.join(missing)}")


def require_root() -> None:
    if os.geteuid() != 0:
        pytest.skip("needs root (network namespaces / PostgreSQL as another user)")


@pytest.fixture(scope="session")
def platform_outputs() -> dict:
    """`tofu output -json` of the platform stack, generated from
    opentofu/shared/shared-variables.tf (see test_tofu.py)."""
    return json.loads((FIXTURES / "platform-outputs.json").read_text())


@pytest.fixture(scope="session")
def infrastructure(platform_outputs) -> dict:
    return platform_outputs["infrastructure"]["value"]


@pytest.fixture(scope="session")
def generated_inventory(tmp_path_factory) -> Path:
    """ansible/inventory.generated.yml as scripts/generate_inventory.py writes
    it from the fixture (addressing only, no hosts)."""
    out = tmp_path_factory.mktemp("inventory") / "inventory.generated.yml"
    subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "generate_inventory.py"),
            "--repo-root",
            str(tmp_path_factory.mktemp("no-stacks")),
            "--platform-json",
            str(FIXTURES / "platform-outputs.json"),
            "--output",
            str(out),
            "--quiet",
        ],
        check=True,
        capture_output=True,
    )
    return out


@pytest.fixture(scope="session")
def render(generated_inventory, tmp_path_factory):
    """render(role_dir, template, **extra_vars) -> rendered text.

    Uses Ansible's own templating (tests/ansible/render.yml) with the role
    defaults and the generated inventory, like a real run."""
    require("ansible-playbook")
    out_dir = tmp_path_factory.mktemp("rendered")

    def _render(role_dir: str, template: str, **extra) -> str:
        dest = out_dir / f"{Path(role_dir).name}-{template}"
        cmd = [
            "ansible-playbook",
            "-i",
            str(generated_inventory),
            str(REPO / "tests" / "ansible" / "render.yml"),
            "-e",
            json.dumps(
                {"role_dir": str(REPO / role_dir), "template": template, "dest": str(dest), **extra}
            ),
        ]
        env = dict(os.environ, ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_NOCOLOR="1")
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return dest.read_text()

    return _render
