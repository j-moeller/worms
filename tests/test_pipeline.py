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
    "rebuild-services": [("tofu", "infrastructure/ephemeral", "destroy"), ("tofu", "private_internet", "destroy"),
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
    node_vms = []  # what the node reports: see the `node` fixture
    node_snapshots = {}  # vmid -> the snapshot's fields

    def __init__(self, config=None, **_kwargs):
        self.config = config
        self.calls = []

    def vms(self):
        return Recorder.node_vms

    def snapshot(self, vm, name):
        assert name == pipeline.SERVICE_SNAPSHOT
        return Recorder.node_snapshots.get(vm["vmid"])

    def commit(self):
        return "abc1234"

    def node_sh(self, command, **_kwargs):
        self.calls.append(("node", command))
        return 0

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

    def sh(self, command, cwd=".", **_kwargs):
        if command == "rm -rf .facts" and cwd == "ansible":
            self.calls.append(("flush facts",))
        return 0

    def __getattr__(self, name):  # python, flag_exists, ...: no tool step
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
    assert set(pipeline.COMPONENTS) - set(EXPECTED) <= {"secrets", "init", "inventory", "show-config", "ping",
                                                        "round-reset-services", "snapshot-services"}  # fmt: skip


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


def test_a_limit_never_skips_the_internet_window(monkeypatch):
    """Internet.yml runs on the node, so `--limit <vm>` would silently skip it:
    the window would not open, and an open one would not close."""
    seen = {}

    class Args(Recorder):
        def ansible(self, playbook, *args, **kwargs):
            seen.setdefault(playbook, []).append(args)
            return super().ansible(playbook, *args, **kwargs)

    monkeypatch.setattr(pipeline, "Runner", Args)
    pipeline.Pipeline(config=None, ansible_args="--limit dashboard").provision()
    assert len(seen["Internet.yml"]) == 2
    assert not any("--limit dashboard" in args for args in seen["Internet.yml"])
    assert "--limit dashboard" in seen["SetupDbAndDashboard.yml"][0]


def test_facts_are_dropped_whenever_vms_may_have_changed(monkeypatch):
    """Ansible caches facts for a run (ansible.cfg); the cache is emptied
    before the run's first playbook and after every tofu apply or destroy, so
    no play sees the facts (MACs, addresses) of a VM that was rebuilt."""
    monkeypatch.setattr(pipeline, "Runner", Recorder)
    pipe = pipeline.Pipeline(config=None)
    pipe.rebuild_services()
    calls = [c for c in pipe.runner.calls if c[0] in ("tofu", "ansible", "flush facts")]
    first_playbook = next(i for i, c in enumerate(calls) if c[0] == "ansible")
    assert calls[first_playbook - 1] == ("flush facts",)
    for i, call in enumerate(calls):
        if call[0] == "tofu" and call[2] in ("apply", "destroy"):
            following = next((c for c in calls[i + 1:] if c[0] in ("ansible", "flush facts")), None)
            assert following in (None, ("flush facts",)), (call, following)
    assert calls.count(("flush facts",)) == 2  # the start of the run, after the applies


def test_stacks_are_destroyed_one_vm_at_a_time(monkeypatch):
    """In parallel, Proxmox's storage lock timed out on 8 of 13 VMs, with only a warning. A
    volume left behind made every later attempt to create a VM with that ID fail."""
    seen = []

    class Runner(Recorder):
        def tofu(self, stack, *args, **kwargs):
            seen.append((stack, args))
            return 0

    monkeypatch.setattr(pipeline, "Runner", Runner)
    pipeline.Pipeline(config=None).devbox_destroy()
    assert seen == [("devbox", ("destroy", "-auto-approve", "-parallelism=1"))]


# --- the internet window's messages ------------------------------------------------


def _names_a_real_command(message):
    """The command a message tells you to run, accepted by the CLI's parser."""
    import importlib.util
    import re

    spec = importlib.util.spec_from_file_location("worms_manage", REPO / "src" / "manage.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    command = re.search(r"`worms ([^`]+)`", message).group(1)
    return cli.build_parser().parse_args(command.split())


def test_a_window_left_open_names_the_command_that_closes_it(monkeypatch):
    monkeypatch.setattr(pipeline, "Runner", Recorder)
    pipe = pipeline.Pipeline(config=None)
    pipe.runner.flag_exists = lambda path: True
    with pytest.raises(BuildError) as still_open:
        pipe.offline()
    assert _names_a_real_command(str(still_open.value)).component == "internet-deny"


def test_a_failed_close_names_the_command_that_closes_it(monkeypatch):
    monkeypatch.setattr(pipeline, "Runner", Recorder)
    pipe = pipeline.Pipeline(config=None)

    def deny_fails():
        raise BuildError("no connection")

    pipe.internet_deny = deny_fails
    with pytest.raises(BuildError) as failed:
        pipe.online(lambda: None)
    assert "STILL ALLOWED" in str(failed.value)
    assert _names_a_real_command(str(failed.value)).component == "internet-deny"


# --- the service VMs: reset, snapshot, rebuild ---------------------------------------


SERVICES = [
    {"vmid": 111, "name": "scoring", "pool": "ephemeral", "status": "running", "node": "n"},
    {"vmid": 100, "name": "docker-registry", "pool": "private_internet", "status": "running", "node": "n"},
]
PERSISTENT = {"vmid": 107, "name": "database", "pool": "persistent", "status": "running", "node": "n"}
DEVBOX = {"vmid": 116, "name": "devbox-t1-a", "pool": "devbox", "status": "running", "node": "n"}
SANDBOX = {"vmid": 130, "name": "sandbox-1", "pool": "sandbox", "status": "running", "node": "n"}
A_SNAPSHOT = {"name": "clean", "snaptime": 1793900000, "description": "worms abc1234"}


@pytest.fixture
def node(monkeypatch):
    """What the node reports, set by the test, and the pipeline on a recorder."""
    monkeypatch.setattr(pipeline, "Runner", Recorder)
    monkeypatch.setattr(Recorder, "node_vms", [*SERVICES, PERSISTENT])
    monkeypatch.setattr(Recorder, "node_snapshots", {100: A_SNAPSHOT, 111: A_SNAPSHOT})
    return pipeline.Pipeline(config=None)


def steps(pipe):
    return [c for c in pipe.runner.calls if c[0] in ("tofu", "ansible", "node")]


def test_a_reset_rolls_back_the_services_and_nothing_else(node):
    """Nothing is destroyed or rebuilt, and no internet window opens."""
    node.round_reset_services()
    assert steps(node) == [
        ("node", "qm rollback 100 clean --start 1"),
        ("node", "qm rollback 111 clean --start 1"),
        ("ansible", "WaitForVMs.yml"),
    ]


def test_a_reset_waits_for_each_vm_by_its_inventory_group(monkeypatch, node):
    """`docker-registry` is the group `docker_registry`, and a wrong --limit waits for nothing."""
    seen = []
    original = Recorder.ansible

    def ansible(self, playbook, *args, **kwargs):
        seen.append(args)
        return original(self, playbook, *args, **kwargs)

    monkeypatch.setattr(Recorder, "ansible", ansible)
    node.round_reset_services()
    assert seen == [("--limit", "docker_registry,scoring")]


def test_a_reset_leaves_devboxes_running(node, monkeypatch):
    """The point of a snapshot: today's rebuild needs every devbox gone first."""
    monkeypatch.setattr(Recorder, "node_vms", [*SERVICES, DEVBOX])
    node.round_reset_services()
    assert ("node", "qm rollback 111 clean --start 1") in steps(node)
    assert not any("116" in str(step) for step in steps(node))


def test_a_reset_is_refused_during_a_round(node, monkeypatch):
    monkeypatch.setattr(Recorder, "node_vms", [*SERVICES, SANDBOX])
    with pytest.raises(BuildError, match="during a round.*sandbox-1"):
        node.round_reset_services()
    assert steps(node) == []


def test_a_reset_touches_nothing_if_any_vm_lacks_a_snapshot(node, monkeypatch):
    """All or nothing: half the services on a snapshot and half not is no clean slate."""
    monkeypatch.setattr(Recorder, "node_snapshots", {111: A_SNAPSHOT})
    with pytest.raises(BuildError, match="nothing was reset.*docker-registry") as refused:
        node.round_reset_services()
    assert steps(node) == []
    assert _names_a_real_command(str(refused.value)).component == "rebuild-services"


def test_a_reset_without_any_service_vm_says_so(node, monkeypatch):
    monkeypatch.setattr(Recorder, "node_vms", [PERSISTENT])
    with pytest.raises(BuildError, match="nothing was reset"):
        node.round_reset_services()


def test_a_snapshot_is_of_a_vm_that_was_shut_down(node, monkeypatch):
    """No guest agent runs on these VMs: a running one would be snapshotted like a
    power cut. The description says which commit it came from."""
    monkeypatch.setattr(Recorder, "node_vms", [SERVICES[0]])
    monkeypatch.setattr(Recorder, "node_snapshots", {})
    node.snapshot_services()
    assert steps(node) == [
        ("node", "qm shutdown 111 --timeout 120 --forceStop 1"),
        ("node", "qm snapshot 111 clean --description 'worms abc1234'"),
        ("node", "qm start 111"),
        ("ansible", "WaitForVMs.yml"),
    ]


def test_snapshot_services_replaces_the_old_snapshot(node, monkeypatch):
    monkeypatch.setattr(Recorder, "node_vms", [SERVICES[0]])
    node.snapshot_services()
    assert steps(node)[0] == ("node", "qm delsnapshot 111 clean")


@pytest.mark.parametrize("team_vm", [DEVBOX, SANDBOX])
def test_no_snapshot_while_a_team_vm_exists(node, monkeypatch, team_vm):
    """Team VMs are assumed malicious and can reach these services: a snapshot taken
    now could keep whatever they did, and every reset would bring it back."""
    monkeypatch.setattr(Recorder, "node_vms", [*SERVICES, team_vm])
    with pytest.raises(BuildError, match="Refusing to snapshot"):
        node.snapshot_services()
    assert steps(node) == []


@pytest.mark.parametrize("team_vm", [DEVBOX, SANDBOX])
def test_a_rebuild_refuses_before_it_destroys_anything(node, monkeypatch, team_vm):
    """The window is refused while team VMs exist. It used to be asked for only after
    the destroys, which left the services gone."""
    monkeypatch.setattr(Recorder, "node_vms", [*SERVICES, team_vm])
    with pytest.raises(BuildError, match="Refusing to destroy"):
        node.rebuild_services()
    assert steps(node) == []


def test_a_build_snapshots_only_the_vms_without_one(node, monkeypatch):
    """A new VM has seen nothing. One that has a snapshot may have been up during
    rounds since, so it keeps the snapshot it has, and the build warns."""
    warnings = []
    monkeypatch.setattr(pipeline.output, "warn", warnings.append)
    monkeypatch.setattr(Recorder, "node_snapshots", {111: A_SNAPSHOT})
    node.snapshot_new_services()
    node_steps = [c[1] for c in steps(node) if c[0] == "node"]
    assert node_steps == ["qm shutdown 100 --timeout 120 --forceStop 1",
                          "qm snapshot 100 clean --description 'worms abc1234'", "qm start 100"]  # fmt: skip
    assert len(warnings) == 1 and "rebuild-services" in warnings[0]


def test_a_first_build_does_not_warn_about_snapshots(node, monkeypatch):
    warnings = []
    monkeypatch.setattr(pipeline.output, "warn", warnings.append)
    monkeypatch.setattr(Recorder, "node_snapshots", {})
    node.snapshot_new_services()
    assert warnings == []


def test_provision_warns_that_a_reset_would_undo_it(node, monkeypatch):
    warnings = []
    monkeypatch.setattr(pipeline.output, "warn", warnings.append)
    node.provision()
    assert len(warnings) == 1 and "older snapshot" in warnings[0]
