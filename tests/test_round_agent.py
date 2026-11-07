"""The round agent (scripts/round_agent): parameter validation, the handler
command sequences (with a fake executor, so no Proxmox/SSH), and the database
claim/run/result loop against the test cluster.

The agent is pure Python (no Django); only the loop test needs a database.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import REPO, require

sys.path.insert(0, str(REPO / "scripts"))

from round_agent import handlers, validation  # noqa: E402
from round_agent.agent import Agent  # noqa: E402


@pytest.fixture(autouse=True)
def _agent_state(monkeypatch, tmp_path):
    """A test's own agent records and internet flag, never this machine's: the tests may
    run on the node itself."""
    monkeypatch.setattr(handlers, "STATE_DIR", str(tmp_path / "agent-state"))
    monkeypatch.setattr(handlers, "INTERNET_FLAG", str(tmp_path / "internet-allowed"))


def _load_varfile_text(text):
    """Parse an agent key varfile (YAML with `!unsafe` string tags)."""
    import yaml

    loader = yaml.SafeLoader
    loader.add_constructor("!unsafe", lambda ldr, node: ldr.construct_scalar(node))
    return yaml.load(text, Loader=loader)


def _load_varfile(path):
    with open(path) as fh:
        return _load_varfile_text(fh.read())


# --- validation ---------------------------------------------------------------


def test_validate_rejects_unknown_kind():
    with pytest.raises(validation.Invalid):
        validation.validate("rm-rf", {})


@pytest.mark.parametrize(
    "params",
    [
        {"start_vm": "../etc", "artifact": {}},
        {"start_vm": "ok", "artifact": {"storage_key": "/etc/passwd", "content_hash": "x"}},
        {"start_vm": "ok", "artifact": {"storage_key": "malware-artifacts/aa/bb/" + "0" * 64 + ".zip", "content_hash": "nothex"}},
        {"start_vm": "a b", "artifact": {"storage_key": "malware-artifacts/aa/bb/" + "0" * 64 + ".zip", "content_hash": "0" * 64}},
        {"start_vm": "ok", "artifact": {"storage_key": "malware-artifacts/aa/bb/" + "0" * 64 + ".zip", "content_hash": "0" * 64}, "worm_delay": -1},
    ],
)
def test_validate_deploy_rejects_bad_input(params):
    with pytest.raises(validation.Invalid):
        validation.validate("deploy", params)


def test_validate_deploy_accepts_good_input():
    key = "malware-artifacts/aa/bb/" + "c" * 64 + ".zip"
    clean = validation.validate(
        "deploy",
        {
            "start_vm": "sandbox-site-1-vm-1",
            "team_uuid": "11111111-2222-3333-4444-555555555555",
            "artifact": {"storage_key": key, "content_hash": "d" * 64},
            "initial_secrets": ["sandbox-site-1-vm-1", "sandbox-site-2-vm-2"],
            "worm_delay": 300,
        },
    )
    assert clean["artifact"]["storage_key"] == key
    assert clean["worm_delay"] == 300
    assert clean["team_uuid"] == "11111111-2222-3333-4444-555555555555"


def test_validate_deploy_rejects_bad_team_uuid():
    key = "malware-artifacts/aa/bb/" + "c" * 64 + ".zip"
    with pytest.raises(validation.Invalid):
        validation.validate(
            "deploy",
            {"start_vm": "vm", "team_uuid": "not-a-uuid", "artifact": {"storage_key": key, "content_hash": "d" * 64}},
        )


# --- devbox (docs/devbox.md) --------------------------------------------------

GOOD_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILx5kgdiAiIfKABbQ2D4tT9hgQ2Km8WiYll6Ji5CdEXD alice@team"


def test_validate_devbox_provision_accepts_good_input():
    clean = validation.validate(
        "devbox_provision",
        {"teams": [
            {"slug": "t1", "index": 0, "team_uuid": "11111111-2222-3333-4444-555555555555", "authorized_keys": [GOOD_KEY]},
            {"slug": "t2", "index": 1, "team_uuid": "22222222-2222-3333-4444-555555555555", "authorized_keys": []},
        ]},
    )
    assert [t["slug"] for t in clean["teams"]] == ["t1", "t2"]
    assert clean["teams"][0]["authorized_keys"] == [GOOD_KEY]


@pytest.mark.parametrize("params", [
    {"teams": []},                                                                   # empty
    {"teams": [{"slug": "BAD", "index": 0, "team_uuid": "1" * 8 + "-2222-3333-4444-555555555555"}]},  # bad slug
    {"teams": [{"slug": "t1", "index": 99, "team_uuid": "11111111-2222-3333-4444-555555555555"}]},     # index range
    {"teams": [{"slug": "t1", "index": 0, "team_uuid": "nope"}]},                     # bad uuid
    {"teams": [                                                                       # duplicate index
        {"slug": "t1", "index": 0, "team_uuid": "11111111-2222-3333-4444-555555555555"},
        {"slug": "t2", "index": 0, "team_uuid": "22222222-2222-3333-4444-555555555555"},
    ]},
    {"teams": [{"slug": "t1", "index": 0, "team_uuid": "11111111-2222-3333-4444-555555555555",
                "authorized_keys": ["not a key"]}]},                                  # bad ssh key
])  # fmt: skip
def test_validate_devbox_provision_rejects_bad_input(params):
    with pytest.raises(validation.Invalid):
        validation.validate("devbox_provision", params)


def test_validate_devbox_rebuild():
    clean = validation.validate(
        "devbox_rebuild",
        {"team_uuid": "11111111-2222-3333-4444-555555555555", "devbox_version": 3,
         "teams": [{"slug": "t1", "index": 0, "team_uuid": "11111111-2222-3333-4444-555555555555", "authorized_keys": []}]},
    )
    assert clean["devbox_version"] == 3


BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAILx5kgdiAiIfKABbQ2D4tT9hgQ2Km8WiYll6Ji5CdEXD"


@pytest.mark.parametrize("comment", [
    "marker={{ 7*7 }}",            # jinja expression
    "{%set x=1%}",                 # jinja statement
    "$(id)", "`id`",               # shell
    'a"b', "a'b", "a\\b",          # quotes / backslash (break out of a YAML scalar)
    "a;b", "a|b", "a<b", "a>b",    # other metacharacters
])
def test_ssh_key_rejects_template_and_shell_characters(comment):
    """A key comment that could be evaluated as a template on the Proxmox host,
    or break the varfile, is rejected (docs/security-assessment.md)."""
    with pytest.raises(validation.Invalid):
        validation.ssh_keys([f"ssh-ed25519 {BLOB} {comment}"])


def test_ssh_key_accepts_ordinary_comments():
    for comment in ("", " alice@laptop", " ci-runner_2024", " u@h.example", " key/for+ci:1"):
        assert validation.ssh_keys([f"ssh-ed25519 {BLOB}{comment}"])


def test_the_same_key_may_not_belong_to_two_teams():
    """sshd matches a key to the first authorized_keys line, so a key shared by
    two teams would hijack confinement: the agent fails the job closed."""
    shared = f"ssh-ed25519 {BLOB} shared"
    params = {"teams": [
        {"slug": "t1", "index": 0, "team_uuid": "11111111-2222-3333-4444-555555555555", "authorized_keys": [shared]},
        {"slug": "t2", "index": 1, "team_uuid": "22222222-2222-3333-4444-555555555555", "authorized_keys": [shared]},
    ]}
    with pytest.raises(validation.Invalid):
        validation.validate("devbox_provision", params)
    # The same team listing a key twice is fine (idempotent).
    ok = {"teams": [{"slug": "t1", "index": 0, "team_uuid": "11111111-2222-3333-4444-555555555555",
                     "authorized_keys": [shared, shared]}]}
    assert validation.validate("devbox_provision", ok)


def test_validate_prepare_bounds():
    with pytest.raises(validation.Invalid):
        validation.validate("prepare", {"seed": 1, "sandbox_params": {"vms": 10_000}})
    clean = validation.validate("prepare", {"seed": 7, "sandbox_params": {"vms": 8, "networks": 4}})
    assert clean["seed"] == 7 and clean["sandbox_params"]["vms"] == 8


def test_validate_release_secrets():
    with pytest.raises(validation.Invalid):
        validation.validate("release_secrets", {"vms": []})
    assert validation.validate("release_secrets", {"vms": ["vm-a"]})["vms"] == ["vm-a"]


# --- handlers with a fake executor --------------------------------------------


class FakeExecutor:
    """Records commands and returns canned output; no host, no SSH."""

    def __init__(self, vms=None, graph=None):
        self.ssh_user = "debian"
        self.calls = []
        self.ssh_calls = []
        self._vms = vms or {
            "sandbox-a": {"maintenance_ip": "10.4.0.10", "vmid": 900, "addresses": ["10.64.0.10"]},
            "sandbox-b": {"maintenance_ip": "10.4.0.11", "vmid": 901, "addresses": ["10.64.1.11"]},
        }
        self._graph = graph or {
            "nodes": [
                {"id": "network-1000", "label": "vmbr1000", "type": "network", "cidr": "10.64.0.0/24", "network_type": "users"},
                {"id": "vm:sandbox-a", "label": "sandbox-a", "type": "virtual-machine"},
                {"id": "vm:sandbox-b", "label": "sandbox-b", "type": "virtual-machine"},
            ],
            "edges": [
                {"source": "vm:sandbox-a", "target": "network-1000", "relationship": "attached-to"},
                {"source": "vm:sandbox-b", "target": "network-1000", "relationship": "attached-to"},
            ],
        }
        self.artifact_bytes = b"PK\x03\x04 worm zip"
        self.installed_hcl = None
        self.systemctl = {}
        self.qm = {}
        self.qm_config = {}
        # Proxmox: whether the sandbox stack holds VMs, the other pools' VMs, which VMs have
        # their clean snapshot.
        self.sandbox_built = True
        self.pools = {"devbox": [], "ephemeral": [], "private_internet": []}
        self.clean = set()
        self.fail_on = None

    def sandbox_pool(self):
        if not self.sandbox_built:
            return []
        hosts = self.tofu_output("sandbox")["ansible_hosts"].values()
        return [{"vmid": h["vm_id"], "name": h["name"], "node": "n", "status": "running", "pool": "sandbox"} for h in hosts]

    def sandbox_definition(self):
        return self.installed_hcl or ""

    def qm_steps(self):
        return [" ".join(c[1:]) for c in self.calls if c[:2] == ("run", "qm") and c[2] not in ("config", "status")]

    def job_log(self, job_uuid, kind):
        from contextlib import nullcontext

        from round_agent.executor import JobLog

        return nullcontext(JobLog())

    def worms(self, component, timeout=3600, args=None):
        self.calls.append(("worms", component))

    def tofu(self, stack, *args, timeout=3600):
        self.calls.append(("tofu", stack, *args))
        if self.fail_on and self.fail_on in args:
            raise RuntimeError(f"tofu {self.fail_on} failed")
        if stack == "sandbox" and args[:1] in (("apply",), ("destroy",)):
            self.sandbox_built = args[0] == "apply"
        return ""

    def tofu_output(self, stack):
        if stack == "sandbox" and not self.sandbox_built:
            return {}
        return {
            "ansible_hosts": {
                "sandbox_gw": {"name": "sandbox-gw", "vm_id": 950, "management_ip": "10.4.0.2"},
                **{name: {"name": name, "vm_id": info["vmid"], "management_ip": info["maintenance_ip"]}
                   for name, info in self._vms.items()},
            },
            "sandbox": {"hosts": {name: {"addresses": info["addresses"]} for name, info in self._vms.items()}},
        }

    def generate_sandbox(self, seed, params, network_file):
        self.calls.append(("generate", seed, params["vms"]))
        from pathlib import Path

        Path(network_file).write_text(json.dumps(self._graph))
        return "locals { seed = \"x\" }"

    def install_sandbox_definition(self, hcl):
        self.installed_hcl = hcl

    def read_json(self, path):
        from pathlib import Path

        return json.loads(Path(path).read_text())

    def run(self, argv, check=False, timeout=60):
        self.calls.append(("run", *[str(a) for a in argv]))
        if argv[:3] == ["pvesh", "get", "/cluster/resources"]:
            return json.dumps(self.sandbox_pool() + [vm for vms in self.pools.values() for vm in vms])
        if argv[:2] == ["pvesh", "get"] and argv[2].endswith("/snapshot"):
            vmid = int(argv[2].split("/")[4])
            return json.dumps([{"name": "clean"}] if vmid in self.clean else [] + [{"name": "current"}])
        if argv[:2] == ["qm", "snapshot"]:
            self.clean.add(int(argv[2]))
        if argv[0] == "systemctl":
            return self.systemctl.get(argv[2], "active")
        if argv[:2] == ["qm", "config"]:
            return self.qm_config.get(argv[2], "cores: 1\ncpu: x86-64-v3\nname: a-vm\n")
        if argv[0] == "qm":
            return self.qm.get(argv[2], "status: running")
        if argv[0] == "/usr/local/sbin/worms-mirror":
            return "tap900i0 wtsb0 mirror-all mirror-all\ntap960i2 wtmir drop drop"
        return ""

    def ssh(self, host, argv, input=None, timeout=120):
        self.ssh_calls.append({"host": host, "argv": argv, "input": input})
        if argv[:2] == ["sudo", "base64"] and input is None:
            return base64.b64encode(self.artifact_bytes).decode()
        return ""


CFG = {"dashboard_host": "10.5.0.18", "dashboard_media_root": "/opt/dashboard/media",
       "scoring_host": "10.5.0.14", "logging_host": "10.5.0.15"}


PREPARE = {"seed": 5, "sandbox_params": {"vms": 2}}
SANDBOX_VMIDS = {900, 901, 950}  # the two VMs and the gateway
SERVICE_VMS = [{"vmid": 111, "name": "scoring", "node": "n", "status": "running", "pool": "ephemeral"},
               {"vmid": 100, "name": "apt-cache", "node": "n", "status": "running", "pool": "private_internet"}]  # fmt: skip
DEVBOX_VMS = [{"vmid": 116, "name": "devbox-t1-a", "node": "n", "status": "running", "pool": "devbox"},
              {"vmid": 117, "name": "devbox-t1-b", "node": "n", "status": "stopped", "pool": "devbox"}]  # fmt: skip


def _prepare(ex, cfg=None):
    return handlers.prepare(ex, cfg or CFG, validation.validate("prepare", PREPARE))


def _first_index(steps, prefix):
    return next(i for i, step in enumerate(steps) if step.startswith(prefix))


def test_prepare_builds_sandbox_and_reports_topology():
    ex = FakeExecutor()
    ex.sandbox_built = False
    result = _prepare(ex)
    assert ("generate", 5, 2) in ex.calls
    assert any(c[:2] == ("tofu", "sandbox") and "apply" in c for c in ex.calls)
    assert ("worms", "round-configure-sandbox") in ex.calls  # inventory + install the CA
    assert ex.installed_hcl is not None
    names = {vm["name"] for vm in result["vms"]}
    assert names == {"sandbox-a", "sandbox-b"}
    assert result["vms"][0]["networks"] == [{"name": "vmbr1000", "cidr": "10.64.0.0/24"}]
    # The full generator graph and the generated OpenTofu config are returned
    # for the round's visualisation.
    assert result["topology"]["nodes"] and result["topology"]["edges"]
    assert any(n["type"] == "network" for n in result["topology"]["nodes"])
    assert result["sandbox_definition"] == ex.installed_hcl
    assert result["sandbox"] == "built and snapshotted"


def test_a_new_sandbox_is_snapshotted_before_any_team_reaches_it():
    """Every VM, its gateway too, shut down first: no guest agent, so a running one would be
    snapshotted like a power cut."""
    ex = FakeExecutor()
    ex.sandbox_built = False
    _prepare(ex)
    steps = ex.qm_steps()
    for vmid in SANDBOX_VMIDS:
        assert steps.index(f"qm shutdown {vmid} --timeout 120 --forceStop 1") < _first_index(steps, f"qm snapshot {vmid} clean")
        assert f"qm start {vmid}" in steps
    assert ex.clean == SANDBOX_VMIDS
    assert not any(handlers.FLAG_PATH in c["argv"] for c in ex.ssh_calls)  # no flag yet, nor a worm


def test_the_next_run_of_the_round_rolls_the_sandbox_back():
    """The same seed and parameters: no build, the snapshot, and nothing of the team before."""
    ex = FakeExecutor()
    ex.sandbox_built = False
    _prepare(ex)
    ex.calls.clear()
    result = _prepare(ex)
    assert not any(c[0] in ("tofu", "worms") and c[1:2] != ("output",) and "output" not in c for c in ex.calls)
    assert sorted(ex.qm_steps()) == sorted(f"qm rollback {vmid} clean --start 1" for vmid in SANDBOX_VMIDS)
    assert result["sandbox"] == "rolled back to its snapshot" and {vm["name"] for vm in result["vms"]} == {"sandbox-a", "sandbox-b"}


@pytest.mark.parametrize("change", ["another round", "a missing snapshot"])
def test_a_sandbox_that_is_not_the_rounds_or_not_whole_is_destroyed_and_built_again(change):
    ex = FakeExecutor()
    ex.installed_hcl = "locals { seed = \"another round\" }" if change == "another round" else 'locals { seed = "x" }'
    ex.clean = set() if change == "another round" else SANDBOX_VMIDS - {901}
    _prepare(ex)
    tofu = [c[2] for c in ex.calls if c[:2] == ("tofu", "sandbox") and c[2] in ("destroy", "apply")]
    assert tofu == ["destroy", "apply"]
    assert not any("rollback" in step for step in ex.qm_steps())


def test_a_new_sandbox_is_built_on_reset_services_with_the_devboxes_off():
    """What it installs comes from the private internet, which team code may have changed:
    back to its snapshot first, and no devbox running until the sandbox is snapshotted."""
    ex = FakeExecutor()
    ex.sandbox_built = False
    ex.pools.update(ephemeral=[SERVICE_VMS[0]], private_internet=[SERVICE_VMS[1]], devbox=list(DEVBOX_VMS))
    ex.clean = {100, 111}
    result = _prepare(ex)
    steps = ex.qm_steps()
    off, on = steps.index("qm shutdown 116 --timeout 120 --forceStop 1"), steps.index("qm start 116")
    rollbacks = [steps.index(f"qm rollback {vmid} clean --start 1") for vmid in (100, 111)]
    sandbox_snapshots = [_first_index(steps, f"qm snapshot {vmid} clean") for vmid in SANDBOX_VMIDS]
    assert off < min(rollbacks) and max(rollbacks + sandbox_snapshots) < on
    assert not any("117" in step for step in steps)  # it was off, it stays off
    assert result["services"].startswith("reset: 2 VMs")


def test_a_rollback_of_the_services_puts_the_devbox_map_back(tmp_path):
    """The map is newer than the services' snapshot: devbox submissions would go unattributed."""
    ex = FakeExecutor()
    ex.sandbox_built = False
    ex.pools.update(ephemeral=[SERVICE_VMS[0]])
    ex.clean = {111}
    state = tmp_path / "agent-state"
    state.mkdir()
    (state / "devbox-teams").write_text("10.2.0.33 11111111-1111-1111-1111-111111111111\n")
    _prepare(ex)
    pushed = [c for c in ex.ssh_calls if "/etc/sandbox-api/devbox-teams" in c["argv"]]
    assert {c["host"] for c in pushed} == {"10.5.0.14", "10.5.0.15"}
    assert all(c["input"] == "10.2.0.33 11111111-1111-1111-1111-111111111111\n" for c in pushed)


def test_services_without_a_snapshot_are_left_as_they_are_and_reported():
    ex = FakeExecutor()
    ex.sandbox_built = False
    ex.pools.update(ephemeral=[SERVICE_VMS[0]], private_internet=[SERVICE_VMS[1]])
    ex.clean = {111}
    result = _prepare(ex)
    assert not any("rollback" in step for step in ex.qm_steps())
    assert "not reset" in result["services"] and "apt-cache" in result["services"] and "rebuild-services" in result["services"]


def test_the_devboxes_come_back_when_the_build_fails():
    ex = FakeExecutor()
    ex.sandbox_built = False
    ex.pools.update(devbox=list(DEVBOX_VMS))
    ex.fail_on = "apply"
    with pytest.raises(RuntimeError, match="apply failed"):
        _prepare(ex)
    assert ex.qm_steps()[-1] == "qm start 116"


@pytest.mark.parametrize("built", [False, True])
def test_no_sandbox_is_started_while_internet_access_is_allowed(tmp_path, built):
    """OpenTofu refuses a build then, but a rollback starts VMs past it."""
    (tmp_path / "internet-allowed").write_text("allowed\n")
    ex = FakeExecutor()
    ex.pools.update(devbox=list(DEVBOX_VMS))
    if built:
        ex.installed_hcl, ex.clean = 'locals { seed = "x" }', set(SANDBOX_VMIDS)
    with pytest.raises(RuntimeError, match="internet access is allowed"):
        _prepare(ex)
    assert ex.qm_steps() == [] and not any(c[0] == "tofu" and "output" not in c for c in ex.calls)


def test_check_reports_ok_when_everything_is_up():
    ex = FakeExecutor()
    result = handlers.check(ex, CFG, {})
    assert result["ok"] is True and result["failures"] == []
    assert any(c["check"] == "vm:sandbox-a:running" and c["ok"] for c in result["checks"])


def test_check_flags_a_down_vm_and_dead_service():
    ex = FakeExecutor()
    ex.systemctl["worms-mirror"] = "inactive"
    ex.qm["900"] = "status: stopped"
    result = handlers.check(ex, CFG, {})
    assert result["ok"] is False
    assert "host:worms-mirror" in result["failures"]
    assert "vm:sandbox-a:running" in result["failures"]


@pytest.mark.parametrize(
    "config",
    ["cpu: host", "cpu: max", "cpu: x86-64-v3,flags=+vmx", "cpu: x86-64-v3,flags=+nested-virt", "cpu: qemu64", "name: no-cpu-line", ""],
)
def test_check_refuses_a_sandbox_vm_without_the_cpu_that_hides_kvm(config):
    """Hostile root on a VM must not get /dev/kvm. Asked of the node, and anything unknown fails."""
    ex = FakeExecutor()
    ex.qm_config["901"] = config
    result = handlers.check(ex, CFG, {})
    assert result["ok"] is False
    assert result["failures"] == ["vm:sandbox-b:no-kvm"]


def test_release_secrets_writes_flag_and_returns_hash():
    ex = FakeExecutor()
    result = handlers.release_secrets(ex, CFG, validation.validate("release_secrets", {"vms": ["sandbox-a"]}))
    assert len(result["released"]) == 1
    entry = result["released"][0]
    assert entry["vm"] == "sandbox-a" and validation.SHA256.match(entry["flag_hash"])
    # The secret went in over stdin to install into /etc/worms/flag, not argv.
    write = next(c for c in ex.ssh_calls if "install" in c["argv"])
    assert write["host"] == "10.4.0.10" and "/etc/worms/flag" in write["argv"]
    assert write["input"].startswith("FLAG{") and entry["flag_hash"] == hashlib.sha256(write["input"].encode()).hexdigest()


def test_release_unknown_vm_fails():
    ex = FakeExecutor()
    with pytest.raises(validation.Invalid):
        handlers.release_secrets(ex, CFG, {"vms": ["not-in-sandbox"]})


def test_deploy_fetches_verifies_copies_and_arms():
    ex = FakeExecutor()
    key = "malware-artifacts/aa/bb/" + "c" * 64 + ".zip"
    params = validation.validate(
        "deploy",
        {
            "start_vm": "sandbox-a",
            "team_uuid": "11111111-2222-3333-4444-555555555555",
            "artifact": {"storage_key": key, "content_hash": hashlib.sha256(ex.artifact_bytes).hexdigest()},
            "initial_secrets": ["sandbox-a"],
            "worm_delay": 120,
        },
    )
    result = handlers.deploy(ex, CFG, params)
    assert result["released"][0]["vm"] == "sandbox-a"
    # Fetched from the dashboard.
    # (as root: the media directory belongs to the dashboard's service user)
    fetch = next(c for c in ex.ssh_calls if c["host"] == "10.5.0.18")
    assert fetch["argv"] == ["sudo", "base64", "--", f"/opt/dashboard/media/{key}"]
    # Told scoring AND logging which team is running (current-team over stdin).
    team_writes = [c for c in ex.ssh_calls if "/etc/sandbox-api/current-team" in c["argv"]]
    assert {c["host"] for c in team_writes} == {"10.5.0.14", "10.5.0.15"}
    assert all(c["input"] == "11111111-2222-3333-4444-555555555555" for c in team_writes)
    # Armed a one-shot timer with the validated delay.
    arm = next(c for c in ex.ssh_calls if any("systemd-run" in str(a) for a in c["argv"]))
    assert "--on-active=120" in arm["argv"]


class DevboxExecutor(FakeExecutor):
    def tofu_output(self, stack):
        assert stack == "devbox"
        host = lambda name, ip, team: {"name": name, "vm_id": 100, "management_ip": ip, "host_vars": {"devbox_team": team}}  # noqa: E731
        return {"ansible_hosts": {
            "devbox-t1-a": host("devbox-t1-a", "10.6.0.11", "t1"),
            "devbox-t2-a": host("devbox-t2-a", "10.6.1.11", "t2"),
            "t1-gw": host("devbox-t1-gw", "10.6.0.2", "t1"),
        }}  # fmt: skip


def _devbox_deploy(ex, **overrides):
    key = "malware-artifacts/aa/bb/" + "c" * 64 + ".zip"
    return validation.validate("devbox_deploy", {
        "team_uuid": "11111111-2222-3333-4444-555555555555", "team_slug": "t1", "start_vm": "devbox-t1-a",
        "artifact": {"storage_key": key, "content_hash": hashlib.sha256(ex.artifact_bytes).hexdigest()},
        "worm_delay": 10, "run_duration": 3600, **overrides,
    })  # fmt: skip


def test_devbox_deploy_puts_the_worm_on_the_teams_vm_a_and_arms_it():
    ex = DevboxExecutor()

    result = handlers.devbox_deploy(ex, CFG, _devbox_deploy(ex))

    assert result["start_vm"] == "devbox-t1-a" and result["armed_at"]
    hosts = [c["host"] for c in ex.ssh_calls]
    assert hosts[0] == "10.5.0.18" and set(hosts[1:]) == {"10.6.0.11"}  # the artifact, then only this VM
    arm = next(c for c in ex.ssh_calls if "systemd-run" in c["argv"])
    assert arm["argv"] == [
        "sudo", "systemd-run", "--on-active=10", "--unit=worms-start", "--property=RuntimeMaxSec=3600",
        "--working-directory", "/home/debian/worm", "--uid", "debian", "/bin/bash", "main.sh",
    ]  # fmt: skip
    # Scoring and logging know a devbox by its gateway's address: a round's team file stays out of it.
    assert not [c for c in ex.ssh_calls if "/etc/sandbox-api/current-team" in c["argv"]]
    assert ex.calls == [("run", "qm", "config", "100")], "it only reads this VM's config, and does not touch OpenTofu"


@pytest.mark.parametrize("config", ["cpu: host", "cpu: max", "cpu: x86-64-v3,flags=+nested-virt", ""])
def test_devbox_deploy_puts_no_worm_on_a_vm_that_could_use_kvm(config):
    ex = DevboxExecutor()
    ex.qm_config["100"] = config
    with pytest.raises(validation.Invalid, match="CPU"):
        handlers.devbox_deploy(ex, CFG, _devbox_deploy(ex))
    assert ex.ssh_calls == [], "nothing was copied and nothing was armed"


def test_devbox_deploy_rejects_tampered_artifact():
    ex = DevboxExecutor()
    params = _devbox_deploy(ex, artifact={"storage_key": "malware-artifacts/aa/bb/" + "c" * 64 + ".zip", "content_hash": "0" * 64})
    with pytest.raises(validation.Invalid, match="hash mismatch"):
        handlers.devbox_deploy(ex, CFG, params)
    assert not [c for c in ex.ssh_calls if "systemd-run" in c["argv"]]


def test_devbox_deploy_only_reaches_the_teams_own_vm_a():
    ex = DevboxExecutor()
    for overrides in ({"start_vm": "devbox-t2-a"}, {"start_vm": "devbox-t1-b"}, {"start_vm": "sandbox-a"}):
        with pytest.raises(validation.Invalid, match="not the team's VM A"):
            _devbox_deploy(ex, **overrides)
    with pytest.raises(validation.Invalid, match="not a VM of team"):
        handlers.devbox_deploy(ex, CFG, _devbox_deploy(ex, team_slug="t3", start_vm="devbox-t3-a"))
    with pytest.raises(validation.Invalid):
        _devbox_deploy(ex, run_duration=0)


def test_deploy_rejects_tampered_artifact():
    ex = FakeExecutor()
    key = "malware-artifacts/aa/bb/" + "c" * 64 + ".zip"
    params = validation.validate(
        "deploy",
        {"start_vm": "sandbox-a", "team_uuid": "11111111-2222-3333-4444-555555555555",
         "artifact": {"storage_key": key, "content_hash": "0" * 64}, "initial_secrets": [], "worm_delay": 60},
    )
    with pytest.raises(validation.Invalid, match="hash mismatch"):
        handlers.deploy(ex, CFG, params)


def test_teardown():
    ex = FakeExecutor()
    assert handlers.teardown(ex, CFG, validation.validate("teardown", {}))["destroyed"] is True
    destroy = next(c for c in ex.calls if c[:2] == ("tofu", "sandbox") and "destroy" in c)
    # In parallel, VMs fight over the storage lock and leave volumes that block later VMs.
    assert "-parallelism=1" in destroy


def test_teardown_keeps_the_sandbox_for_the_rounds_next_run_powered_off():
    ex = FakeExecutor()
    result = handlers.teardown(ex, CFG, validation.validate("teardown", {"keep": True}))
    assert result == {"destroyed": False, "kept": True}
    assert not any(c[:2] == ("tofu", "sandbox") and "destroy" in c for c in ex.calls)
    assert sorted(ex.qm_steps()) == sorted(f"qm stop {vmid}" for vmid in SANDBOX_VMIDS)


@pytest.mark.parametrize("keep", ["true", 1, "yes", None, {}])
def test_only_a_real_true_keeps_a_sandbox(keep):
    """Destroying is the safe side."""
    assert validation.validate("teardown", {"keep": keep}) == {"keep": False}


def test_devbox_gateway_permitopen_is_only_the_entry_vm():
    """The SSH gateway forwards each team to its entry VM (A) only; B/C are
    reached by pivoting inside the devbox network (docs/security-assessment.md).
    Every registered key is still installed on the VMs themselves (team_keys)."""

    class Ex:
        def __init__(self):
            self.args = None

        def tofu_output(self, stack):
            return {
                "devbox": {
                    "t1": {
                        "services_source_ip": "10.2.0.33",
                        "vms": [
                            {"name": "devbox-t1-a", "ssh_fabric_ip": "10.7.0.10"},
                            {"name": "devbox-t1-b", "ssh_fabric_ip": "10.7.0.11"},
                            {"name": "devbox-t1-c", "ssh_fabric_ip": "10.7.0.12"},
                        ],
                    }
                }
            }

        def worms(self, component, timeout=3600, args=None):
            self.args = args

    ex = Ex()
    teams = [{"slug": "t1", "authorized_keys": ["ssh-ed25519 AAAA key-one", "ssh-ed25519 BBBB key-two"]}]
    handlers._configure_devbox(ex, teams)

    extra = _load_varfile(ex.args.split("@", 1)[1])
    gateway_keys = extra["ssh_gateway_authorized_keys"]
    assert len(gateway_keys) == 2  # one confined line per registered key
    for entry in gateway_keys:
        assert entry["permitopen"] == ["10.7.0.10:22"]  # only VM A, never B/C
        assert entry["comment"] == "team=t1"
    # The VMs themselves still authorize every registered key.
    assert extra["devbox_team_keys"]["t1"] == teams[0]["authorized_keys"]

    # A team without a VM A (its Devbox row exists, its VMs do not: a failed
    # provision) gets no gateway line: one without permitopen could forward anywhere.
    teams.append({"slug": "t2", "authorized_keys": ["ssh-ed25519 CCCC key-three"]})
    handlers._configure_devbox(ex, teams)
    gateway_keys = _load_varfile(ex.args.split("@", 1)[1])["ssh_gateway_authorized_keys"]
    assert [entry["comment"] for entry in gateway_keys] == ["team=t1", "team=t1"]
    assert all(entry["permitopen"] for entry in gateway_keys)


@pytest.mark.parametrize("job", ["devbox_provision", "devbox_rebuild", "devbox_reconfigure"])
def test_every_devbox_job_reports_where_the_vms_are(job, tmp_path):
    """The team's page shows these. Only the addresses on the team's own links: from a VM, the
    SSH fabric and the maintenance network lead nowhere."""

    class Ex:
        repo_root = tmp_path

        def tofu_output(self, stack):
            return {}

        def generate_devbox(self, teams, output_file):
            pass

        def tofu(self, stack, *args, timeout=3600):
            pass

        def worms(self, component, timeout=3600, args=None):
            pass

        def ssh(self, host, argv, input=None, **_):
            pass

        def run(self, argv, **_):
            return "[]"  # Proxmox: no VM, no snapshot

    params = {
        "team_uuid": "11111111-1111-1111-1111-111111111111",
        "devbox_version": 2,
        "teams": [{"slug": "t2", "index": 1, "team_uuid": "11111111-1111-1111-1111-111111111111", "authorized_keys": []}],
    }
    result = handlers.HANDLERS[job](Ex(), {"scoring_host": "scoring", "logging_host": "logging"}, params)
    assert result["addresses"] == {
        "devbox-t2-a": ["10.128.2.11"],
        "devbox-t2-b": ["10.128.2.12", "10.128.3.12"],
        "devbox-t2-c": ["10.128.3.13"],
    }


# --- devbox snapshots -------------------------------------------------------------

T1 = "11111111-1111-1111-1111-111111111111"
DEVBOX_TEAMS = [{"slug": "t1", "index": 0, "team_uuid": T1, "authorized_keys": ["ssh-ed25519 " + "A" * 68 + " t1@laptop"]}]
T1_VMIDS = {116, 117, 118}


class DevboxFake(FakeExecutor):
    """One team's devbox: three VMs, their gateway and the SSH gateway, once applied."""

    def __init__(self, tmp_path, built_before=False):
        super().__init__()
        self.repo_root = tmp_path
        self.devbox_built = built_before
        self._sync()

    def _sync(self):
        self.pools["devbox"] = [
            {"vmid": vmid, "name": f"devbox-t1-{role}", "node": "n", "status": "running", "pool": "devbox"}
            for vmid, role in zip(sorted(T1_VMIDS), "abc")
        ] + [{"vmid": 119, "name": "devbox-t1-gw", "node": "n", "status": "running", "pool": "devbox"}] if self.devbox_built else []

    def generate_devbox(self, teams, output_file):
        self.calls.append(("generate-devbox",))

    def worms(self, component, timeout=3600, args=None):
        super().worms(component, timeout, args)
        if component == "devbox-apply":
            self.devbox_built = True
            self._sync()

    def tofu(self, stack, *args, timeout=3600):
        if stack == "devbox" and "apply" in args:
            self.devbox_built = True
            self._sync()
        return super().tofu(stack, *args, timeout=timeout)

    def tofu_output(self, stack):
        if stack != "devbox":
            return super().tofu_output(stack)
        if not self.devbox_built:
            return {}
        hosts = {
            f"devbox-t1-{role}": {"name": f"devbox-t1-{role}", "vm_id": vmid, "management_ip": f"10.6.0.{vmid}",
                                  "host_vars": {"devbox_team": "t1", "devbox_ssh_ip": f"10.7.0.{vmid}"}}
            for vmid, role in zip(sorted(T1_VMIDS), "abc")
        }  # fmt: skip
        hosts["devbox-t1-gw"] = {"name": "devbox-t1-gw", "vm_id": 119, "management_ip": "10.6.0.119", "host_vars": {}}
        vms = [{"name": f"devbox-t1-{role}", "ssh_fabric_ip": f"10.7.0.{vmid}"} for vmid, role in zip(sorted(T1_VMIDS), "abc")]
        return {"ansible_hosts": hosts, "devbox": {"t1": {"services_source_ip": "10.2.0.33", "vms": vms}}}

    def steps(self):
        """qm steps, configuration and SSH in the order they ran."""
        return [" ".join(c[1:]) for c in self.calls if (c[:2] == ("run", "qm") and c[2] not in ("config", "status")) or c[0] == "worms"]


def test_a_new_devbox_is_snapshotted_after_its_first_boot_and_before_any_team_key(tmp_path):
    ex = DevboxFake(tmp_path)
    handlers.devbox_provision(ex, CFG, validation.validate("devbox_provision", {"teams": DEVBOX_TEAMS}))
    steps = ex.steps()
    configure = steps.index("devbox-configure")
    for vmid in T1_VMIDS:
        assert steps.index(f"qm shutdown {vmid} --timeout 120 --forceStop 1") < _first_index(steps, f"qm snapshot {vmid} clean") < configure
    assert ex.clean == T1_VMIDS  # not the gateways: a reset rolls back the team's own three
    first_boot = [c["host"] for c in ex.ssh_calls if c["argv"] == ["cloud-init", "status", "--wait"]]
    assert sorted(first_boot) == sorted(f"10.6.0.{vmid}" for vmid in T1_VMIDS)


def test_a_devbox_that_was_there_before_gets_no_snapshot(tmp_path):
    """Its team may have been on it: a snapshot now would bring that back at every reset."""
    ex = DevboxFake(tmp_path, built_before=True)
    handlers.devbox_provision(ex, CFG, validation.validate("devbox_provision", {"teams": DEVBOX_TEAMS}))
    assert ex.clean == set() and not any("snapshot" in step for step in ex.steps())


def _rebuild(ex):
    params = {"team_uuid": T1, "devbox_version": 3, "teams": DEVBOX_TEAMS}
    return handlers.devbox_rebuild(ex, CFG, validation.validate("devbox_rebuild", params))


def test_a_devbox_reset_rolls_back_to_the_snapshot(tmp_path):
    """Minutes of OpenTofu and first boot saved; the keys and new tokens still follow."""
    ex = DevboxFake(tmp_path, built_before=True)
    ex.clean = set(T1_VMIDS)
    result = _rebuild(ex)
    steps = ex.steps()
    assert not any(c[:2] == ("tofu", "devbox") and "apply" in c for c in ex.calls)
    assert [s for s in steps if "rollback" in s] == [f"qm rollback {vmid} clean --start 1" for vmid in sorted(T1_VMIDS)]
    assert steps[-1] == "devbox-configure"
    tokens = [c["host"] for c in ex.ssh_calls if handlers.FLAG_PATH in c["argv"]]
    assert sorted(tokens) == sorted(f"10.6.0.{vmid}" for vmid in T1_VMIDS)
    assert result["devbox"] == "rolled back to its snapshot" and len(result["vms"]) == 3


def test_a_devbox_without_a_snapshot_is_replaced_and_snapshotted(tmp_path):
    ex = DevboxFake(tmp_path, built_before=True)
    ex.clean = {116, 117}
    result = _rebuild(ex)
    replace = next(c for c in ex.calls if c[:2] == ("tofu", "devbox") and "apply" in c)
    assert sum(arg.startswith("-replace=") for arg in replace) == 3
    assert ex.clean == T1_VMIDS and result["devbox"] == "replaced and snapshotted"
    assert not any("rollback" in step for step in ex.steps())


def test_no_devbox_is_rolled_back_while_internet_access_is_allowed(tmp_path):
    (tmp_path / "internet-allowed").write_text("allowed\n")
    ex = DevboxFake(tmp_path, built_before=True)
    ex.clean = set(T1_VMIDS)
    with pytest.raises(RuntimeError, match="internet access is allowed"):
        _rebuild(ex)
    assert not any("rollback" in step for step in ex.steps())


def test_a_reconfigure_writes_the_map_before_the_minutes_of_configuration(tmp_path):
    """After a reset of scoring and logging, the map is what is missing."""
    ex = DevboxFake(tmp_path, built_before=True)
    order = []
    ex.ssh = lambda host, argv, input=None, timeout=120: order.append(("map", host)) if handlers.DEVBOX_TEAMS_FILE in argv else None
    ex.worms = lambda component, timeout=3600, args=None: order.append(("configure", component))
    handlers.devbox_reconfigure(ex, CFG, validation.validate("devbox_reconfigure", {"team_uuid": T1, "teams": DEVBOX_TEAMS}))
    assert order == [("map", "10.5.0.14"), ("map", "10.5.0.15"), ("configure", "devbox-configure")]


def test_the_agents_copy_of_the_map_is_root_only(tmp_path):
    ex = DevboxFake(tmp_path, built_before=True)
    handlers.devbox_reconfigure(ex, CFG, validation.validate("devbox_reconfigure", {"team_uuid": T1, "teams": DEVBOX_TEAMS}))
    copy = tmp_path / "agent-state" / "devbox-teams"
    assert copy.read_text() == f"10.2.0.33 {T1}\n"
    assert oct(copy.stat().st_mode & 0o777) == "0o600" and oct(copy.parent.stat().st_mode & 0o777) == "0o700"


def test_devbox_key_varfile_tags_keys_unsafe():
    """The keys are written with the YAML `!unsafe` tag so Ansible never
    templates attacker-controlled key text (template injection defence in
    depth; docs/security-assessment.md)."""
    raw = handlers._unsafe_yaml(
        {"devbox_team_keys": {"t1": [handlers._Unsafe("ssh-ed25519 AAAA marker={{ 7*7 }}")]},
         "ssh_gateway_authorized_keys": [{"public_key": handlers._Unsafe("ssh-ed25519 AAAA x"), "comment": "team=t1", "permitopen": []}]}
    )
    assert "!unsafe" in raw and "{{ 7*7 }}" in raw and "49" not in raw
    # It round-trips to the literal strings (the !unsafe tag is stripped on load).
    loaded = _load_varfile_text(raw)
    assert loaded["devbox_team_keys"]["t1"] == ["ssh-ed25519 AAAA marker={{ 7*7 }}"]


# --- the database loop --------------------------------------------------------


@pytest.fixture
def agent_db(database):
    """An Agent wired to the test cluster with a fake executor."""
    env = {**database.env("postgres", ""), "DB_PASSWORD": "x"}
    ex = FakeExecutor()
    agent = Agent(env=env, executor=ex)
    conn = database.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE host_jobs RESTART IDENTITY CASCADE")
    agent._conn = conn
    return agent, ex, conn


def _queue(conn, kind, params):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO host_jobs (uuid, kind, params, state, result, error) "
            "VALUES (gen_random_uuid(), %s, %s, 'queued', '{}', '') RETURNING id",
            (kind, json.dumps(params)),
        )
        return cur.fetchone()[0]


def test_loop_runs_a_job_and_writes_the_result(agent_db):
    agent, ex, conn = agent_db
    job_id = _queue(conn, "check", {})
    assert agent.tick() is True
    with conn.cursor() as cur:
        cur.execute("SELECT state, result FROM host_jobs WHERE id = %s", (job_id,))
        state, result = cur.fetchone()
    assert state == "done" and result["ok"] is True


def test_loop_fails_an_invalid_job_without_crashing(agent_db):
    agent, ex, conn = agent_db
    job_id = _queue(conn, "deploy", {"start_vm": "../bad"})
    agent.tick()
    with conn.cursor() as cur:
        cur.execute("SELECT state, error FROM host_jobs WHERE id = %s", (job_id,))
        state, error = cur.fetchone()
    assert state == "failed" and "invalid" in error


def test_loop_claims_each_job_once(agent_db):
    agent, ex, conn = agent_db
    _queue(conn, "check", {})
    _queue(conn, "teardown", {})
    assert agent.tick() and agent.tick()
    assert agent.tick() is False  # queue drained
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM host_jobs WHERE state = 'done'")
        assert cur.fetchone()[0] == 2


def test_a_rounds_jobs_go_before_a_devboxs(agent_db):
    agent, ex, conn = agent_db
    _queue(conn, "devbox_deploy", {})
    _queue(conn, "devbox_rebuild", {})
    round_job = _queue(conn, "check", {})

    assert agent.claim()[0] == round_job
    # Among themselves, oldest first.
    assert agent.claim()[2] == "devbox_deploy"
    assert agent.claim()[2] == "devbox_rebuild"
    assert agent.claim() is None


def test_a_devbox_rebuild_gives_opentofu_addresses_it_accepts(tmp_path):
    """No reset could ever work: the handler escaped the quotes for a shell, no
    shell runs it, and OpenTofu rejected the address. Found by the live run."""
    require("tofu")
    (tmp_path / "main.tf").write_text("")

    def refused(*args):
        proc = subprocess.run(["tofu", f"-chdir={tmp_path}", "plan", "-input=false", *args], capture_output=True, text=True)
        return "Invalid force-replace address" in proc.stdout + proc.stderr

    assert refused('-replace=module.devbox_vm[\\"devbox-t4-a\\"].proxmox_virtual_environment_vm.vm')  # the old form
    replaces = handlers._devbox_replace_args("t4")
    assert len(replaces) == 3 and not refused(*replaces)
    assert [r.split('"')[1] for r in replaces] == ["devbox-t4-a", "devbox-t4-b", "devbox-t4-c"]


def test_a_reconfigure_with_a_newer_one_behind_it_is_skipped(agent_db):
    """Each carries every team's keys, so a burst of key changes runs once, with
    the newest keys."""
    agent, ex, conn = agent_db
    params = {"team_uuid": "6a1f2c3d-0000-4000-8000-000000000001", "teams": []}
    _queue(conn, "devbox_reconfigure", params)
    _queue(conn, "devbox_reconfigure", params)
    assert agent.tick() and agent.tick()
    with conn.cursor() as cur:
        cur.execute("SELECT state, result FROM host_jobs ORDER BY id")
        first, second = cur.fetchall()
    assert first == ("done", {"superseded": True})
    assert second[0] != "queued" and second[1] != {"superseded": True}


def test_loop_is_safe_against_a_second_worker(agent_db, database):
    agent, ex, conn = agent_db
    _queue(conn, "check", {})
    other = Agent(env=agent.env, executor=FakeExecutor())
    other._conn = database.connect()
    other._conn.autocommit = True
    # Two agents, one job: exactly one runs it.
    ran = [agent.tick(), other.tick()]
    assert ran.count(True) == 1


# --- the role template --------------------------------------------------------


# What the agent's environment file is rendered from: all of it is required (a missing
# value stops SetupRoundAgent.yml before anything changes: tasks/workspace.yml).
AGENT_ENV_VARS = dict(
    infrastructure_ips={"database": "10.11.0.16"},
    management_ips={"dashboard": "10.5.0.18", "scoring": "10.5.0.14", "logging": "10.5.0.15"},
    postgres_db="mydb", pki_ca_cert_path="/etc/worms/ca.crt", vault_round_agent_db_password="secret",
    dashboard_media_root="/opt/dashboard/media", vault_tofu_proxmox_api_token="root@pam!worms=0000-1111",
    proxmox={"host": "10.11.0.1", "api_port": 8006, "node": "pve-lab"}, ssh_gateway_ips={"maintenance": "10.5.0.30"},
    worms_environment={"name": "development", "operator_ssh_port": 22}, llm_access_ip="192.0.2.34",
)  # fmt: skip


def test_round_agent_env_renders(render):
    env = render("ansible/roles/host/round_agent", "round-agent.env.j2", **AGENT_ENV_VARS)
    assert "DB_USER=round_agent" in env and "DB_SSLMODE=verify-full" in env
    assert "DASHBOARD_MEDIA_ROOT=/opt/dashboard/media" in env
    assert "PYTHONPATH=" in env and "/scripts" in env
    assert "SCORING_HOST=10.5.0.14" in env and "LOGGING_HOST=10.5.0.15" in env
    assert "DASHBOARD_HOST=10.5.0.18" in env
    # OpenTofu: the provisioning token from the vault.
    assert "TF_VAR_proxmox_api_url=https://127.0.0.1:8006/" in env
    assert "TF_VAR_proxmox_api_token=root@pam!worms=0000-1111" in env
    assert "TF_VAR_ssh_public_key=/etc/worms/ssh/worms.pub" in env  # the WORMS key, from the vault
    assert "REPO_ROOT=/opt/worms" in env and "DB_SSLROOTCERT=/opt/worms/ansible/pki/worms/ca.crt" in env


def test_the_agent_is_the_node_whatever_roots_worms_config_says(render, tmp_path, monkeypatch):
    """A /root/.worms/config with WORMS_SSH on the node (laptop mode) made the
    agent's `worms build devbox-apply` fail with "no ansible/inventory.generated.yml"
    (2026-11-06, production). The agent's environment empties it."""
    env = render("ansible/roles/host/round_agent", "round-agent.env.j2", **AGENT_ENV_VARS)
    sys.path.insert(0, str(REPO / "src"))
    from worms_cli import config

    roots_config = tmp_path / "config"
    roots_config.write_text("WORMS_SSH=root@worms.example\n")
    monkeypatch.setattr(config, "DEFAULT_CONFIG_FILE", roots_config)
    assert config.Config.load().ssh == "root@worms.example"  # the file alone: laptop mode
    for line in env.splitlines():
        if line.startswith("WORMS_"):
            key, _, value = line.partition("=")
            monkeypatch.setenv(key, value)
    assert config.Config.load().ssh == ""  # with the agent's environment: the node


def test_round_agent_env_names_every_service_vm_it_waits_for(render):
    """After rolling them back for a round's sandbox, which installs from the private internet."""
    env = render(
        "ansible/roles/host/round_agent", "round-agent.env.j2", **{**AGENT_ENV_VARS,
        "management_ips": {"dashboard": "10.5.0.18", "scoring": "10.5.0.14", "logging": "10.5.0.15",
                           "monitor": "10.5.0.100", "dns": "10.5.0.11", "database": "10.3.0.16"},
        "private_internet_ips": {"apt_cache": {"maintenance": "10.5.0.20", "network": "10.2.0.20"}}},
    )  # fmt: skip
    assert "SERVICE_HOSTS=10.5.0.14,10.5.0.15,10.5.0.100,10.5.0.11,10.5.0.20\n" in env
    from round_agent.agent import config

    assert config({"SERVICE_HOSTS": "10.5.0.14,10.5.0.20"})["service_hosts"] == ["10.5.0.14", "10.5.0.20"]
    assert config({})["service_hosts"] == []


def test_round_agent_env_carries_the_opentofu_inputs(render):
    """The agent runs `tofu apply` for the sandbox and devbox stacks from a
    systemd service: it needs the same inputs as the operator's `make`."""
    env = render("ansible/roles/host/round_agent", "round-agent.env.j2", **AGENT_ENV_VARS)
    assert "TF_VAR_proxmox_api_token=root@pam!worms=0000-1111" in env
    assert "TF_VAR_proxmox_instance=pve-lab" in env


def test_round_agent_unit_wraps_an_ssh_agent(render, tmp_path):
    """The bpg provider uploads snippets over SSH through an agent (agent =
    true); systemd kills anything ExecStartPre forks, so ssh-agent wraps the
    service command instead: worms-node-run (tests/test_laptop.py runs it)."""
    import shutil
    import subprocess

    unit = render("ansible/roles/host/round_agent", "worms-round-agent.service.j2")
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert exec_start == 'ExecStart=/usr/local/sbin/worms-node-run . "exec /usr/bin/python3 -m round_agent"'
    wrapper = render("ansible/roles/host/round_agent", "worms-node-run.j2")
    assert "exec ssh-agent sh -c 'ssh-add -q" in wrapper and "/etc/worms/ssh/worms" in wrapper
    assert "ExecStartPre" not in unit
    path = tmp_path / "worms-round-agent.service"
    path.write_text(unit)
    if shutil.which("systemd-analyze"):
        proc = subprocess.run(["systemd-analyze", "verify", "--man=no", str(path)], capture_output=True, text=True)
        assert "ExecStart" not in proc.stderr, proc.stderr


# --- the remote side: what ssh really runs on a VM ---------------------------------
#
# ssh does not pass an argument list: it joins its arguments into one command
# line and the VM's login shell parses it again (`sh -c "<line>"`). The fake
# executor above records argv lists and so hid that the deploy's copy and fuse
# commands were re-split on the VM. These tests run the very line ssh would
# send through a real shell.


def _remote_shell(line, input=None, cwd=None):
    """What sshd does with the command line it receives."""
    import subprocess

    return subprocess.run(["/bin/sh", "-c", line], input=input, cwd=cwd, capture_output=True, text=True)


def _zip(entries):
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


# --- the job's output log -----------------------------------------------------


JOB_UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def logged(tmp_path):
    """A real Executor inside a job, writing to a log under tmp_path."""
    from round_agent.executor import Executor

    ex = Executor(tmp_path, log_dir=tmp_path / "jobs")
    with ex.job_log(JOB_UUID, "devbox_provision") as job_log:
        yield ex, job_log
    assert job_log._handle is None


def _log_text(job_log):
    return job_log.path.read_text()


def test_a_build_goes_to_a_root_only_log_and_stdout_stays_the_callers(logged, tmp_path):
    ex, job_log = logged
    out = ex.run(["sh", "-c", "echo to-stdout; echo to-stderr >&2"], stream=True)
    assert out == "to-stdout\n"  # handlers parse this: stderr must not get mixed in
    text = _log_text(job_log)
    assert f"job {JOB_UUID} (devbox_provision)" in text
    assert "$ sh -c 'echo to-stdout; echo to-stderr >&2'" in text
    assert "to-stdout\n" in text and "to-stderr\n" in text
    assert "exit 0 after" in text
    assert (job_log.path.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "jobs").stat().st_mode & 0o777) == 0o700
    assert (tmp_path / "jobs" / "latest.log").resolve() == job_log.path.resolve()


def test_the_log_can_be_followed_while_the_command_runs(logged, tmp_path):
    """The command waits for a file that appears only once its first line is in the log."""
    import threading
    import time

    ex, job_log = logged
    seen = tmp_path / "seen"

    def watch():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if "first line" in _log_text(job_log):
                seen.touch()
                return
            time.sleep(0.05)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    script = f'echo "first line"; i=0; until [ -e {seen} ] || [ $i -ge 200 ]; do sleep 0.1; i=$((i+1)); done; [ -e {seen} ]'
    ex.run(["sh", "-c", script], stream=True, timeout=30)
    watcher.join()
    assert seen.exists()


def test_values_the_caller_reads_stay_out_of_the_log(logged, tmp_path, monkeypatch):
    """A secret written to a VM goes through stdin, and `tofu output` can hold sensitive
    values: neither may reach the log. `tofu apply` is the build's progress, so it does."""
    ex, job_log = logged
    assert ex.run(["cat"], input="FLAG{from-stdin}") == "FLAG{from-stdin}"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_tofu = bin_dir / "tofu"
    fake_tofu.write_text(
        '#!/bin/sh\ncase "$2" in\n'
        '  output) echo \'{"token": {"value": "sensitive-output"}}\' ;;\n'
        "  *) echo apply-progress ;;\n"
        "esac\n"
    )
    fake_tofu.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    assert ex.tofu_output("devbox") == {"token": "sensitive-output"}
    ex.tofu("devbox", "apply", "-auto-approve")

    text = _log_text(job_log)
    assert "$ cat" in text and "FLAG{from-stdin}" not in text
    assert "output -json" in text and "sensitive-output" not in text
    assert "apply-progress" in text


def test_a_failed_build_keeps_its_real_reason_in_the_log(logged):
    """Ansible reports a failed task on stdout. The job's `error` keeps only stderr, so
    it has to say where the rest is."""
    from round_agent.executor import CommandError

    ex, job_log = logged
    with pytest.raises(CommandError) as failure:
        ex.run(["sh", "-c", "echo 'fatal: [vm]: FAILED! => the real reason'; echo warning >&2; exit 3"], stream=True)
    assert "exited 3: warning" in str(failure.value)
    assert str(job_log.path) in str(failure.value)
    # The failed task from stdout, too: the error is what the dashboard shows.
    assert "last failure: fatal: [vm]: FAILED! => the real reason" in str(failure.value)
    text = _log_text(job_log)
    assert "the real reason" in text and "exit 3 after" in text


def test_a_failed_commands_error_keeps_the_end_of_stderr():
    """Ansible starts stderr with inventory-plugin warnings; its reason comes last
    (2026-11-07: the job's error showed only the warnings)."""
    from round_agent.executor import CommandError

    stderr = "[WARNING]: Failed to parse inventory with 'auto' plugin\n" * 40 + "[ERROR]: the real reason\n"
    message = str(CommandError(["python3"], 1, stderr))
    assert message.endswith("[ERROR]: the real reason") and "exited 1: ..." in message


def test_a_timed_out_build_is_killed_and_noted(logged):
    ex, job_log = logged
    with pytest.raises(subprocess.TimeoutExpired):
        ex.run(["sleep", "30"], stream=True, timeout=0.3)
    assert "timed out after 0.3 s" in _log_text(job_log)


def test_a_background_child_holding_the_pipe_does_not_hold_up_the_job(logged, monkeypatch):
    import time

    from round_agent import executor

    monkeypatch.setattr(executor, "PIPE_GRACE", 0.3)
    ex, job_log = logged
    started = time.monotonic()
    assert ex.run(["sh", "-c", "sleep 5 & echo done"], stream=True) == "done\n"
    assert time.monotonic() - started < 3


def test_a_log_that_cannot_be_opened_does_not_fail_the_job(tmp_path):
    from round_agent.executor import Executor

    blocker = tmp_path / "a-file"
    blocker.write_text("")
    ex = Executor(tmp_path, log_dir=blocker / "jobs")
    with ex.job_log(JOB_UUID, "check") as job_log:
        assert job_log.path is None
        assert ex.run(["echo", "still runs"], stream=True) == "still runs\n"


def test_a_log_that_stops_working_does_not_fail_the_job(logged):
    ex, job_log = logged
    job_log._handle.close()  # as good as a full disk: every further write raises
    assert ex.run(["echo", "still runs"], stream=True) == "still runs\n"


def test_outside_a_job_nothing_is_logged(tmp_path):
    from round_agent.executor import Executor

    ex = Executor(tmp_path, log_dir=tmp_path / "jobs")
    assert ex.run(["echo", "hello"], stream=True) == "hello\n"
    assert not (tmp_path / "jobs").exists()


def test_old_logs_go_and_new_ones_stay(tmp_path):
    from round_agent.executor import JOB_LOG_KEEP_DAYS, Executor

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    old, recent = jobs / "old.log", jobs / "recent.log"
    old.write_text("old")
    recent.write_text("recent")
    long_ago = time.time() - (JOB_LOG_KEEP_DAYS + 1) * 86400
    os.utime(old, (long_ago, long_ago))
    ex = Executor(tmp_path, log_dir=jobs)
    for _ in range(2):  # the second time, latest.log already exists
        with ex.job_log(JOB_UUID, "check"):
            pass
    assert not old.exists() and recent.exists()
    assert (jobs / "latest.log").is_symlink() and (jobs / f"{JOB_UUID}.log").exists()


def test_a_job_logs_its_commands_and_how_it_ended(agent_db, tmp_path, monkeypatch):
    from round_agent.executor import CommandError, Executor

    agent, _fake, conn = agent_db
    agent.executor = Executor(tmp_path, log_dir=tmp_path / "jobs")

    def build(ex, cfg, params):
        ex.run(["sh", "-c", "echo building"], stream=True)
        return {"built": True}

    def broken(ex, cfg, params):
        ex.run(["sh", "-c", "echo 'fatal: the reason'; echo warning >&2; exit 1"], stream=True)

    monkeypatch.setitem(handlers.HANDLERS, "check", build)
    _queue(conn, "check", {})
    assert agent.tick() is True
    text = (tmp_path / "jobs" / "latest.log").read_text()
    assert "building" in text and "job done" in text

    monkeypatch.setitem(handlers.HANDLERS, "check", broken)
    job_id = _queue(conn, "check", {})
    assert agent.tick() is True
    latest = tmp_path / "jobs" / "latest.log"
    text = latest.read_text()
    assert "fatal: the reason" in text and "job failed: CommandError" in text
    with conn.cursor() as cur:
        cur.execute("SELECT state, error, uuid FROM host_jobs WHERE id = %s", (job_id,))
        state, error, uuid = cur.fetchone()
    # The job's error points to the log, and its stdout stays out of the database.
    assert state == "failed" and f"{uuid}.log" in error and "fatal: the reason" not in error
    assert isinstance(CommandError(["x"], 1, ""), RuntimeError)


def test_ssh_sends_one_line_the_remote_shell_splits_back_into_argv(monkeypatch):
    import shlex

    from round_agent.executor import SSH_OPTS, Executor

    ex = Executor("/nonexistent")
    sent = {}
    monkeypatch.setattr(ex, "run", lambda argv, input=None, timeout=None: sent.setdefault("argv", argv))
    argv = ["/bin/sh", "-c", "base64 -d > /home/debian/user-worm.zip"]
    ex.ssh("10.4.0.10", argv)
    assert sent["argv"][: len(SSH_OPTS) + 1] == ["ssh", *SSH_OPTS]
    assert sent["argv"][-2:] == ["--", "/bin/sh -c 'base64 -d > /home/debian/user-worm.zip'"]
    assert shlex.split(sent["argv"][-1]) == argv


def test_every_deploy_command_reaches_the_vm_unchanged():
    import shlex

    from round_agent.executor import Executor

    ex = FakeExecutor()
    ex.artifact_bytes = _zip({"main.sh": "echo hi\n"})
    handlers.deploy(ex, CFG, _deploy_params(ex))
    for call in ex.ssh_calls:
        assert shlex.split(Executor.remote_command(call["argv"])) == [str(a) for a in call["argv"]]


def _deploy_params(ex, delay=120):
    return validation.validate("deploy", {
        "start_vm": "sandbox-a", "team_uuid": "11111111-2222-3333-4444-555555555555",
        "artifact": {"storage_key": "malware-artifacts/aa/bb/" + "c" * 64 + ".zip",
                     "content_hash": hashlib.sha256(ex.artifact_bytes).hexdigest()},
        "initial_secrets": ["sandbox-a"], "worm_delay": delay,
    })  # fmt: skip


def _replay_on_the_vm(ex, home, start_ip="10.4.0.10"):
    """Run the deploy's copy and unpack commands as the start VM would, with
    the VM's home directory at `home`. Returns the unpack result."""
    from round_agent.executor import Executor

    calls = [c for c in ex.ssh_calls if c["host"] == start_ip and c["argv"][0] == "/bin/sh"]
    copy, unpack = calls
    result = _remote_shell(Executor.remote_command(copy["argv"]), input=copy["input"])
    assert result.returncode == 0, result.stderr
    return _remote_shell(Executor.remote_command(unpack["argv"]))


def test_the_zip_arrives_intact_and_is_unpacked(tmp_path, monkeypatch):
    monkeypatch.setattr(handlers, "WORM_DIR", str(tmp_path))
    ex = FakeExecutor()
    ex.artifact_bytes = _zip({"main.sh": "echo hi\n", "lib/util.py": "x = 1\n", "../../escape": "no"})
    result = handlers.deploy(ex, CFG, _deploy_params(ex))
    unpack = _replay_on_the_vm(ex, tmp_path)
    assert unpack.returncode == 0, unpack.stderr
    assert (tmp_path / handlers.WORM_ZIP).read_bytes() == ex.artifact_bytes  # not base64 text
    assert (tmp_path / "worm" / "main.sh").read_text() == "echo hi\n"
    assert (tmp_path / "worm" / "lib" / "util.py").exists()
    # Nothing outside the worm's directory.
    assert not (tmp_path.parent / "escape").exists() and not (tmp_path / "escape").exists()
    # The fuse runs `bash main.sh` in that directory as the sandbox user,
    # after the delay -- no shell, nothing run at deploy time.
    arm = next(c for c in ex.ssh_calls if "systemd-run" in c["argv"])
    assert arm["argv"][:2] == ["sudo", "systemd-run"] and "--on-active=120" in arm["argv"]
    assert arm["argv"][arm["argv"].index("--working-directory") + 1] == f"{tmp_path}/worm"
    assert arm["argv"][arm["argv"].index("--uid") + 1] == "debian"
    assert arm["argv"][-2:] == ["/bin/bash", "main.sh"]
    # The conductor dates the worm's start from when the fuse was lit.
    from datetime import datetime, timezone

    armed = datetime.fromisoformat(result["armed_at"])
    assert armed.tzinfo is not None and abs((datetime.now(timezone.utc) - armed).total_seconds()) < 60


@pytest.mark.parametrize("entries, message", [
    ({"worm/main.sh": "x"}, "no main.sh at its top level"),
    ({"README": "x"}, "no main.sh at its top level"),
])  # fmt: skip
def test_an_upload_without_main_sh_fails_the_deploy_with_a_reason(tmp_path, monkeypatch, entries, message):
    monkeypatch.setattr(handlers, "WORM_DIR", str(tmp_path))
    ex = FakeExecutor()
    ex.artifact_bytes = _zip(entries)
    handlers.deploy(ex, CFG, _deploy_params(ex))
    unpack = _replay_on_the_vm(ex, tmp_path)
    assert unpack.returncode == 3 and message in unpack.stderr


def test_a_file_that_is_not_a_zip_fails_the_unpack(tmp_path, monkeypatch):
    monkeypatch.setattr(handlers, "WORM_DIR", str(tmp_path))
    ex = FakeExecutor()
    ex.artifact_bytes = b"not a zip"
    handlers.deploy(ex, CFG, _deploy_params(ex))
    assert _replay_on_the_vm(ex, tmp_path).returncode != 0


def test_flags_are_written_where_no_directory_exists_yet():
    """A fresh VM has no /etc/worms: install -D creates it (sandbox and devbox)."""
    ex = FakeExecutor()
    handlers.release_secrets(ex, CFG, validation.validate("release_secrets", {"vms": ["sandbox-a"]}))
    write = next(c for c in ex.ssh_calls if "install" in c["argv"])
    assert write["argv"][:4] == ["sudo", "install", "-D", "-m"]
    ex = FakeExecutor()
    handlers._write_devbox_token(ex, "10.6.0.10")
    assert ex.ssh_calls[0]["argv"][:3] == ["sudo", "install", "-D"]


def test_install_d_creates_the_flag_directory(tmp_path):
    """The flag write itself, through a shell, into a path whose directory is missing."""
    from round_agent.executor import Executor

    target = tmp_path / "etc" / "worms" / "flag"
    argv = ["install", "-D", "-m", "0444", "/dev/stdin", str(target)]
    result = _remote_shell(Executor.remote_command(argv), input="FLAG{x}")
    assert result.returncode == 0, result.stderr
    assert target.read_text() == "FLAG{x}" and oct(target.stat().st_mode & 0o777) == "0o444"


@pytest.mark.parametrize("keep", [False, True])
def test_teardown_clears_the_running_team(keep):
    """After a run, scoring and logging refuse round submissions (no current team)."""
    ex = FakeExecutor()
    handlers.teardown(ex, CFG, {"keep": keep})
    cleared = [c for c in ex.ssh_calls if c["argv"] == ["sudo", "rm", "-f", "/etc/sandbox-api/current-team"]]
    assert {c["host"] for c in cleared} == {"10.5.0.14", "10.5.0.15"}


def test_base64_aliases_cannot_map_one_ssh_key_to_two_teams():
    import base64

    canonical = base64.b64encode(b"x" * 40).decode()
    alternate = canonical[:-3] + "B=="
    assert base64.b64decode(canonical) == base64.b64decode(alternate)
    params = {"teams": [
        {"slug": "t1", "index": 0, "team_uuid": "11111111-2222-3333-4444-555555555555",
         "authorized_keys": [f"ssh-ed25519 {canonical} first"]},
        {"slug": "t2", "index": 1, "team_uuid": "22222222-2222-3333-4444-555555555555",
         "authorized_keys": [f"ssh-ed25519 {alternate} second"]},
    ]}
    with pytest.raises(validation.Invalid, match="more than one team"):
        validation.validate("devbox_provision", params)
