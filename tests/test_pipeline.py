"""The build pipeline (src/worms_cli/pipeline.py): which OpenTofu stacks and
Ansible playbooks each `worms build <component>` runs, in which order, and
that the internet window always closes again.

The tools are not run: the Runner is replaced by a recorder, so this checks
the orchestration only (what the Makefile used to do).
"""

from __future__ import annotations

import sys

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "src"))

from worms_cli import pipeline  # noqa: E402
from worms_cli.build import BuildError  # noqa: E402

CONFIGURE = [("ansible", p) for p in pipeline.CONFIGURE_PLAYBOOKS]
ALLOW, DENY = ("ansible", "Internet.yml", "internet=allowed"), ("ansible", "Internet.yml", "internet=denied")
OFFLINE = [("ansible", "SetupDNS.yml"), ("ansible", "SetupPrivateInternet.yml")]
INIT = [("tofu", s, "init") for s in ("platform", "firewall", "infrastructure/persistent",
                                      "infrastructure/ephemeral", "sandbox", "devbox", "private_internet")]  # fmt: skip

# component -> the tofu/ansible steps, in order.
EXPECTED = {
    "platform": [("tofu", "platform", "apply")],
    "firewall": [("tofu", "firewall", "apply")],
    "host-network": [("ansible", "SetupHostNetwork.yml")],
    "infrastructure": [("tofu", "private_internet", "apply"), ("tofu", "infrastructure/persistent", "apply"),
                       ("tofu", "infrastructure/ephemeral", "apply")],
    "configure": CONFIGURE,
    "provision": [ALLOW, *CONFIGURE, DENY, *OFFLINE],
    "offline": OFFLINE,
    "internet-allow": [ALLOW],
    "internet-deny": [DENY],
    "round-configure-sandbox": [("ansible", "SetupSandbox.yml")],
    "devbox-apply": [("tofu", "devbox", "apply")],
    "devbox-configure": [("ansible", "SetupSSHGateway.yml"), ("ansible", "SetupDevbox.yml")],
    "devbox": [("tofu", "devbox", "apply"), ("ansible", "SetupSSHGateway.yml"), ("ansible", "SetupDevbox.yml")],
    "round-reset-services": [("tofu", "infrastructure/ephemeral", "destroy"), ("tofu", "private_internet", "destroy"),
                             ALLOW, ("tofu", "private_internet", "apply"), ("tofu", "infrastructure/ephemeral", "apply"),
                             *CONFIGURE, DENY, *OFFLINE],
    "simulate": [("ansible", "Simulate.yml")],
    "all": [*INIT, ("tofu", "platform", "apply"), ("tofu", "firewall", "apply"), ("ansible", "SetupHostNetwork.yml"),
            ALLOW, ("tofu", "private_internet", "apply"), ("tofu", "infrastructure/persistent", "apply"),
            ("tofu", "infrastructure/ephemeral", "apply"), *CONFIGURE, DENY, *OFFLINE],
}  # fmt: skip


class Recorder:
    """Stands in for worms_cli.build.Runner; records the tool steps."""

    fail_on = None

    def __init__(self, config=None, dry_run=False, **_kwargs):
        self.config = config
        self.calls = []

    def _record(self, step):
        self.calls.append(step)
        if Recorder.fail_on and step[:2] == Recorder.fail_on:
            raise BuildError(f"{step} failed")

    def tofu(self, stack, *args, **_kwargs):
        verb = next(a for a in args if not a.startswith("-"))
        self._record(("tofu", stack, verb))
        return 0

    def ansible(self, playbook, *args, **_kwargs):
        internet = tuple(a for a in args if a.startswith("internet="))
        self._record(("ansible", playbook, *internet))
        return 0

    def __getattr__(self, name):  # sh, python, flag_exists, ...: no tool step
        return lambda *a, **k: 0 if name != "flag_exists" else False


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(pipeline, "Runner", Recorder)

    def _run(component):
        pipe = pipeline.Pipeline(config=None)
        getattr(pipe, pipeline.COMPONENTS[component][0])()
        return [c for c in pipe.runner.calls if c[0] in ("tofu", "ansible")]

    return _run


@pytest.mark.parametrize("component", sorted(EXPECTED))
def test_component_steps(run, component):
    assert run(component) == EXPECTED[component]


def test_every_component_is_covered():
    assert set(pipeline.COMPONENTS) - set(EXPECTED) <= {"secrets", "init", "inventory", "show-config", "ping"}


def test_failed_step_still_closes_the_window(monkeypatch):
    monkeypatch.setattr(pipeline, "Runner", Recorder)
    Recorder.fail_on = ("ansible", "SetupDbAndDashboard.yml")
    try:
        pipe = pipeline.Pipeline(config=None)
        with pytest.raises(BuildError):
            pipe.provision()
        steps = [c for c in pipe.runner.calls if c[0] == "ansible"]
        assert steps[-1] == DENY
        assert ("ansible", "SetupSandboxApi.yml") not in steps  # stopped at the failure
    finally:
        Recorder.fail_on = None
