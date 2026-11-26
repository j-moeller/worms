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
import tempfile
import time
from pathlib import Path

import pytest

from conftest import REPO, require

sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))  # the gameplan framework, as the agent's PYTHONPATH has it

from round_agent import handlers, host_state, probe, startup, validation  # noqa: E402
from round_agent.agent import Agent  # noqa: E402

HOST_BOOT = "6c1f0a52-2b1e-4d0e-9a57-0d6f2b9b8c11"  # the test host's boot id


@pytest.fixture(autouse=True)
def _agent_state(monkeypatch, tmp_path):
    """A test's own agent records, internet flag, /proc, /sys, QEMU pid files and task list,
    never this machine's: the tests may run on the host itself, whose VMs have the vmids the
    fakes use (a test once set a real devbox QEMU's OOM priority). The host has been up 1000 s."""
    monkeypatch.setattr(handlers, "STATE_DIR", str(tmp_path / "agent-state"))
    monkeypatch.setattr(handlers, "INTERNET_FLAG", str(tmp_path / "internet-allowed"))
    for name in ("PROC", "SYS", "QEMU_PIDS", "TASKS"):
        (tmp_path / "host" / name).mkdir(parents=True)
        monkeypatch.setattr(host_state, name, str(tmp_path / "host" / name))
    (tmp_path / "host" / "PROC" / "uptime").write_text("1000.00 3600.00\n")
    (tmp_path / "host" / "PROC" / "sys" / "kernel" / "random").mkdir(parents=True)
    (tmp_path / "host" / "PROC" / "sys" / "kernel" / "random" / "boot_id").write_text(HOST_BOOT + "\n")


def _host(tmp_path):
    return {name: tmp_path / "host" / name for name in ("PROC", "SYS", "QEMU_PIDS", "TASKS")}


def _fake_qemu(tmp_path, vmid, pid, threads=(), started=0.0):
    """A running VM's QEMU in the test's /proc: its pid file, command line, OOM priority, threads,
    and when it started (seconds after the host booted: /proc/<pid>/stat's field 22, in ticks)."""
    host = _host(tmp_path)
    (host["QEMU_PIDS"] / f"{vmid}.pid").write_text(f"{pid}\n")
    proc = host["PROC"] / str(pid)
    (proc / "task").mkdir(parents=True, exist_ok=True)
    (proc / "cmdline").write_bytes(b"\0".join([b"/usr/bin/kvm", b"-id", str(vmid).encode(), b"-name", b"vm", b""]))
    (proc / "oom_score_adj").write_text("0\n")
    ticks = round(started * os.sysconf("SC_CLK_TCK"))
    (proc / "stat").write_text(f"{pid} (kvm) S 1 {pid} {pid} 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 9 0 {ticks} 3221225472 1000 0\n")
    for tid in (pid, *threads):
        (proc / "task" / str(tid)).mkdir(exist_ok=True)
    return proc / "oom_score_adj"


def _fake_memory(tmp_path, available_mb, total_mb=32000):
    host = _host(tmp_path)
    (host["PROC"] / "meminfo").write_text(f"MemTotal: {total_mb * 1024} kB\nMemFree: 1 kB\nMemAvailable: {available_mb * 1024} kB\n")
    (host["PROC"] / "pressure").mkdir(exist_ok=True)
    (host["PROC"] / "pressure" / "memory").write_text("some avg10=1.50 avg60=0.01 avg300=0.00 total=1\n"
                                                      "full avg10=0.25 avg60=0.01 avg300=0.00 total=1\n")  # fmt: skip
    (host["SYS"] / "kernel" / "mm" / "ksm").mkdir(parents=True, exist_ok=True)
    (host["SYS"] / "kernel" / "mm" / "ksm" / "pages_sharing").write_text("25600\n")  # 100 MB of 4 KiB pages
    (host["PROC"] / "spl" / "kstat" / "zfs").mkdir(parents=True, exist_ok=True)
    (host["PROC"] / "spl" / "kstat" / "zfs" / "arcstats").write_text(f"name type data\nsize 4 {2048 * 2**20}\nc_min 4 {1024 * 2**20}\nc_max 4 1\n")


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


# --- devbox (docs/04-dev-environment.md) --------------------------------------------------

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
    or break the varfile, is rejected (docs/07-security.md)."""
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


RUN = "33333333-4444-5555-6666-777777777777"


@pytest.mark.parametrize("name", ["../x.py", "x", "/etc/passwd", "a/b.py", "X.py", ".py", "x.py\n", None, 3])
def test_a_gameplan_is_named_by_a_plain_file_name_only(name):
    with pytest.raises(validation.Invalid):
        validation.validate("prepare", {"gameplan": name, "gameplan_sha256": "0" * 64, "run_uuid": RUN})


def test_validate_prepare():
    clean = validation.validate("prepare", {"gameplan": "round_one.py", "gameplan_sha256": "a" * 64, "run_uuid": RUN})
    assert clean == {"gameplan": "round_one.py", "gameplan_sha256": "a" * 64, "run_uuid": RUN}
    with pytest.raises(validation.Invalid):
        validation.validate("prepare", {"gameplan": "round_one.py", "gameplan_sha256": "nothex", "run_uuid": RUN})
    # The round's build (once, before the attempts: no run), its rollback; an unfinished build
    # continued: the operator's yes, a boolean (worms_cli/rounds.py).
    version = {"gameplan": "round_one.py", "gameplan_sha256": "a" * 64}
    assert validation.validate("build", {**version, "round": 3}) == {**version, "resume_build": False}
    assert validation.validate("build", {**version, "resume_build": True})["resume_build"] is True
    with pytest.raises(validation.Invalid):
        validation.validate("build", {**version, "resume_build": "yes"})
    assert validation.validate("rollback", {**version, "run_uuid": RUN}) == version
    assert validation.validate("snapshot", {"round": 3}) == {} == validation.validate("delete_snapshot", {})
    # The jobs around it: preflight names the gameplan too, check the run (or nothing: the host only).
    flight = {"gameplan": "round_one.py", "gameplan_sha256": "a" * 64}
    assert validation.validate("preflight", flight) == {**flight, "hold_devboxes": False}
    assert validation.validate("preflight", {**flight, "hold_devboxes": True})["hold_devboxes"] is True
    for bad in ({**flight, "hold_devboxes": "yes"}, {**flight, "hold_devboxes": 1}, {**flight, "gameplan": "../x.py"}):
        with pytest.raises(validation.Invalid):
            validation.validate("preflight", bad)
    assert validation.validate("check", {}) == {"run_uuid": None} and validation.validate("check", {"run_uuid": RUN}) == {"run_uuid": RUN}
    with pytest.raises(validation.Invalid):
        validation.validate("check", {"run_uuid": "../etc"})
    # observe: Proxmox VM numbers, and since when (a zoned ISO time of the last days).
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(minutes=30)
    clean = validation.validate("observe", {"vmids": [901, 900, 901], "since": since.isoformat()})
    assert clean == {"vmids": [900, 901], "since": since.timestamp()}
    assert validation.validate("observe", {"vmids": [900]})["since"] is None
    for bad in ({}, {"vmids": []}, {"vmids": "900"}, {"vmids": ["900"]}, {"vmids": [True]}, {"vmids": [99]},
                {"vmids": list(range(100, 200))}, {"vmids": [900], "since": "an hour ago"},
                {"vmids": [900], "since": since.replace(tzinfo=None).isoformat()},
                {"vmids": [900], "since": (since - timedelta(days=9)).isoformat()},
                {"vmids": [900], "since": (since + timedelta(days=1)).isoformat()}):  # fmt: skip
        with pytest.raises(validation.Invalid):
            validation.validate("observe", bad)


def test_validate_flags_and_files_are_strict_unless_told_otherwise():
    with pytest.raises(validation.Invalid):
        validation.validate("place_flags", {"vms": []})
    assert validation.validate("place_flags", {"vms": ["vm-a"]}) == {"vms": ["vm-a"], "strict": True}
    assert validation.validate("remove_flags", {"vms": ["vm-a"], "strict": "no"})["strict"] is True
    assert validation.validate("remove_flags", {"vms": ["vm-a"], "strict": False})["strict"] is False
    assert validation.validate("write_file", {"run_uuid": RUN, "file": 2})["strict"] is True
    with pytest.raises(validation.Invalid):
        validation.validate("write_file", {"run_uuid": RUN, "file": "../x"})


# --- handlers with a fake executor --------------------------------------------


# The gameplan the agent's tests build: two VMs on one network (src/gameplan).
PAIR = """
from gameplan import Allow, BasicVM, FirewallRule, Gameplan, Incoming, Outgoing


class Pair(Gameplan):
    def __init__(self):
        self.a = BasicVM()
        self.b = BasicVM()
        self.b.install("jq")
        self.key = self.generate_ssh_key()
        self.login = self.generate_ssh_pwd_login("alice")
        self.add_private_ssh_key(self.a, self.key)
        self.add_public_ssh_key(self.b, self.key)
        self.add_ssh_pwd_login(self.b, self.login)
        self.note = self.write_file(self.b, "~/note", f"{self.login.user} {self.login.pwd}")
        self.later = self.write_file(self.a, "/root/later", f"{self.key.public}", owner="root", at_reset=False)
        self.b.add_service("cron")
        self.register_network([self.a, self.b])
        self.register_fw_rule(self.a, FirewallRule(Allow, 22, Outgoing, peers=[self.b]))
        self.register_fw_rule(self.b, FirewallRule(Allow, 22, Incoming))
        self.set_worm_entrypoint(self.a)

    def execute(self):
        self.deploy_worm()
"""
GAMEPLAN_ROOT = Path(tempfile.mkdtemp(prefix="agent-gameplans-"))
(GAMEPLAN_ROOT / "gameplans").mkdir()
(GAMEPLAN_ROOT / "scripts").mkdir()  # what the agent's workspace holds of scripts/: capture-vm
(GAMEPLAN_ROOT / handlers.CAPTURE_VM).write_text((REPO / handlers.CAPTURE_VM).read_text())
(GAMEPLAN_ROOT / "gameplans" / "pair.py").write_text(PAIR)
PAIR_SHA = hashlib.sha256(PAIR.encode()).hexdigest()
# The platform's addresses, as `worms step inventory` leaves them in the agent's workspace.
(GAMEPLAN_ROOT / "ansible").mkdir()
(GAMEPLAN_ROOT / "ansible" / "inventory.generated.yml").write_text(
    json.dumps({"all": {"vars": {
        "infrastructure_ips": {"dashboard": "10.10.0.18", "database": "10.11.0.16", "orchestrator": "10.11.0.19"},
        "services_ips": {"dns": "10.2.0.11", "scoring": "10.2.0.14", "logging": "10.2.0.15", "sandbox_gw": "10.2.0.2"},
        "private_internet_ips": {"apt_cache": {"network": "10.2.0.20", "maintenance": "10.5.0.20"},
                                 "devpi": {"network": "10.2.0.21", "maintenance": "10.5.0.21"}},
        "host_ips": {"mgmt_sandbox": "10.4.0.1"},
        "devbox_services_block": "10.2.0.32/27",
        "networks": {"services": "10.2.0.0/24", "mgmt_sandbox": "10.4.0.0/16", "sandbox_vms": "10.64.0.0/10"},
    }}})  # JSON is YAML
)  # fmt: skip
# sandbox-gw's cloud-init on the fake host, its snippet (the sandbox stack renders it from its template,
# whose rules and settings it has unchanged): the check reads them back from the gateway.
GATEWAY_TEMPLATE = REPO / "opentofu" / "stacks" / "sandbox" / "templates" / "sandbox-gw.tftpl"
GATEWAY_SNIPPET = GAMEPLAN_ROOT / "snippets" / "sandbox-gw.yml"
GATEWAY_SNIPPET.parent.mkdir()
GATEWAY_SNIPPET.write_text(GATEWAY_TEMPLATE.read_text())


def _mac(vmid, nic):
    """The MAC address of a VM's NIC, as the fake host's Proxmox configs give them."""
    return f"bc:24:11:{vmid >> 8:02x}:{vmid & 0xFF:02x}:{nic:02x}"


def _pair_hcl():
    """The sandbox definition prepare installs for PAIR."""
    from gameplan import build, loader, runtime

    loaded = loader.load(GAMEPLAN_ROOT / "gameplans", "pair.py")
    return build.hcl(runtime.instantiate(loaded.cls).spec(), "pair.py", loaded.sha256)


class FakeExecutor:
    """Records commands and returns canned output; no host, no SSH. A VM keeps the flag it
    was given, so that reading it back works."""

    def __init__(self, vms=None):
        self.ssh_user = "debian"
        self.repo_root = GAMEPLAN_ROOT
        self.calls = []
        self.ssh_calls = []
        self.notes = []
        self.flags = {}
        self.unreachable = set()
        self._vms = vms or {
            "sandbox-a": {"maintenance_ip": "10.4.0.10", "vmid": 900, "addresses": ["10.64.0.10"]},
            "sandbox-b": {"maintenance_ip": "10.4.0.11", "vmid": 901, "addresses": ["10.64.1.11"]},
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
        self.descriptions = {}  # vmid -> its clean snapshot's description
        self.fail_on = None
        # What the host reports now: vmid -> (status, qmpstatus), else running (or the pool's).
        self.states = {}
        self.cluster_firewall = 1
        self.journal = ""
        self.api_units = {"10.5.0.14": "active\nactive\n", "10.5.0.15": "active\nactive\n"}
        self.apt_cache = "jq_1.7.1-6_amd64.deb\ncurl_8.14.1-2_amd64.deb\n"
        # The probes: each one's parameters by VM address, and changes to the answer of a VM
        # that has everything right (probe_answer). The same of the start and initialisation
        # check that comes before them (init_answer).
        self.probes = {}
        self.probe_hooks = []
        self.inits = {}
        self.init_hooks = []
        # The cloud-init snippets on the host (`pvesm path` of the volume a VM's config names).
        self.snippets = {"local:snippets/sandbox-gw.yml": GATEWAY_SNIPPET}

    def init_answer(self, host, params):
        """What a VM (or the gateway) that came up as it should answers startup.INIT: everything as
        it was asked to find it, the gateway's settings as the cloud-init it was built with has them."""
        reply = {
            "started": time.time(),
            "cloud-init": {"code": 0, "status": "done", "extended": "done", "errors": [], "recoverable": {}, "stage": None,
                           "waited": 0.3, "late": False},
            "boot": {"state": "running", "failed": [], "jobs": 0, "waited": 0.1, "late": False},
            "addresses": {key: "" for key, _, _ in params["nics"]},
            "route": [True, f"via {params['gateway']} dev eth0" if params["gateway"] else "no default route"],
            "sshd": [True, "sshd"],
            "users": {user: True for user in params["users"]},
            "services": {unit: "active" for unit in params["services"]},
        }  # fmt: skip
        if params["chains"]:
            reply["nftables"] = {" ".join(chain[:3]): "" for chain in params["chains"]}
        if params["sysctl"]:
            _, sysctl = startup.gateway_template(self.snippets["local:snippets/sandbox-gw.yml"].read_text())
            reply["sysctl"] = {key: sysctl.get(key) for key in params["sysctl"]}
        for hook in self.init_hooks:
            hook(host, params, reply)
        return reply

    def probe_answer(self, host, params):
        """What a VM where everything is as its gameplan says answers the probe."""
        reply = {
            "started": time.time(), "flag": [True, ""], "dns": [True, ""], "scoring": [True, "HTTP 403"],
            "logging": [True, "HTTP 403"], "apt": [True, ""], "pypi": [True, "HTTP 200"], "time": [True, ""],
            "connect": {key: {"open": "open", "reachable": "refused", "blocked": "timeout"}[want] for key, _, _, want in params["connect"]},
            "forbidden": {label: "timeout" for label, _, _ in params["forbidden"]},
            "files": {key: "" for key, *_ in params["files"]},
        }  # fmt: skip
        for hook in self.probe_hooks:
            hook(host, params, reply)
        return reply

    def sandbox_pool(self):
        if not self.sandbox_built:
            return []
        hosts = self.tofu_output("sandbox")["ansible_hosts"].values()
        return [{"vmid": h["vm_id"], "name": h["name"], "node": "n", "status": "running", "pool": "sandbox"} for h in hosts]

    def sandbox_definition(self):
        return self.installed_hcl or ""

    def qm_steps(self):
        return [" ".join(c[1:]) for c in self.calls if c[:2] == ("run", "qm") and c[2] not in ("config", "status")]

    def job_log(self, job_uuid, kind, job_id=None):
        from contextlib import nullcontext

        from round_agent.executor import JobLog

        return nullcontext(JobLog())

    def worms(self, component, timeout=3600, args=None):
        self.calls.append(("worms", component))
        self.worms_args = args
        if component == "inventory" and not (self.repo_root / handlers.INVENTORY).exists():  # a workspace without one
            (self.repo_root / handlers.INVENTORY).parent.mkdir(parents=True, exist_ok=True)
            (self.repo_root / handlers.INVENTORY).write_text((GAMEPLAN_ROOT / handlers.INVENTORY).read_text())

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
            "sandbox": {
                "hosts": {name: {"addresses": info["addresses"]} for name, info in self._vms.items()},
                "gateway": {"name": "sandbox-gw", "maintenance_ip": "10.4.0.2", "services_ip": "10.2.0.2",
                            "sandbox_legs": [{"bridge": "vmbr1000", "cidr": "10.64.0.1/24"}]},
            },
        }  # fmt: skip

    def note(self, text):
        self.notes.append(text)

    def install_sandbox_definition(self, hcl):
        self.installed_hcl = hcl

    def read_json(self, path):
        from pathlib import Path

        return json.loads(Path(path).read_text())

    def run(self, argv, check=False, timeout=60):
        self.calls.append(("run", *[str(a) for a in argv]))
        if argv[0] == "ssh-keygen":
            key = Path(argv[argv.index("-f") + 1])
            key.write_text(f"-----BEGIN OPENSSH PRIVATE KEY-----\n{argv[argv.index('-t') + 1]}\n-----END OPENSSH PRIVATE KEY-----\n")
            key.with_name("key.pub").write_text("ssh-ed25519 AAAAPUBLIC worms-gameplan\n")
            return ""
        if argv[:3] == ["pvesh", "get", "/cluster/resources"]:
            return json.dumps(self.sandbox_pool() + [vm for vms in self.pools.values() for vm in vms])
        if argv[:2] == ["pvesh", "get"] and argv[2] == "/nodes/n/qemu":
            listed = self.sandbox_pool() + [vm for vms in self.pools.values() for vm in vms]
            return json.dumps([
                {"vmid": vm["vmid"], "name": vm["name"], "status": self.states.get(vm["vmid"], (vm["status"], None))[0],
                 "qmpstatus": self.states.get(vm["vmid"], (vm["status"], vm["status"] if vm["status"] == "running" else None))[1]}
                for vm in listed
            ])  # fmt: skip
        if argv[:3] == ["pvesh", "get", "/cluster/firewall/options"]:
            return json.dumps({"enable": self.cluster_firewall, "policy_in": "DROP"})
        if argv[0] == "journalctl":
            return self.journal
        if argv[:2] == ["pvesh", "get"] and argv[2].endswith("/snapshot"):
            vmid = int(argv[2].split("/")[4])
            return json.dumps([{"name": "clean", "description": self.descriptions.get(vmid, "")}] if vmid in self.clean else [] + [{"name": "current"}])
        if argv[:2] == ["qm", "snapshot"]:
            self.clean.add(int(argv[2]))
            self.descriptions[int(argv[2])] = argv[argv.index("--description") + 1] if "--description" in argv else ""
        if argv[0] == "systemctl":
            return self.systemctl.get(argv[2], "active")
        if argv[:2] == ["qm", "config"]:
            nics = "".join(f"net{i}: virtio={_mac(int(argv[2]), i).upper()},bridge=vmbr{1000 + i},firewall=1,rate=10\n" for i in range(3))
            user = "sandbox-gw" if argv[2] == "950" else "sandbox-vm"
            return self.qm_config.get(argv[2], f"cicustom: user=local:snippets/{user}.yml\ncores: 1\ncpu: x86-64-v3\nname: a-vm\n{nics}")
        if argv[:2] == ["pvesm", "path"]:
            if argv[2] not in self.snippets:
                raise RuntimeError(f"pvesm path {argv[2]}: no such volume")
            return f"{self.snippets[argv[2]]}\n"
        if argv[0] == "qm":
            return self.qm.get(argv[2], "status: running")
        if argv[0] == "/usr/local/sbin/worms-mirror":
            return "tap900i0 wtsb0 mirror-all mirror-all\ntap960i2 wtmir drop drop"
        return ""

    def ssh(self, host, argv, input=None, timeout=120):
        self.ssh_calls.append({"host": host, "argv": argv, "input": input})
        if host in self.unreachable:
            raise RuntimeError(f"ssh {host}: connection refused")
        if argv[:2] == ["sudo", "base64"] and input is None:
            return base64.b64encode(self.artifact_bytes).decode()
        if handlers.FLAG_PATH in argv and argv[:2] == ["sudo", "install"]:
            self.flags[host] = input
        if argv == ["sudo", "sha256sum", handlers.FLAG_PATH]:
            flag = self.flags.get(host)
            return f"{hashlib.sha256(flag.encode()).hexdigest()}  {handlers.FLAG_PATH}\n" if flag else ""
        if argv == ["sudo", "python3", "-I", "-B", "-"]:
            params = probe.params_of(input)
            if params.get("kind") == "init":
                self.inits[host] = params
                return probe.MARK + json.dumps(self.init_answer(host, params)) + "\n"
            self.probes[host] = params
            return "debian@vm: some login banner\n" + probe.MARK + json.dumps(self.probe_answer(host, params)) + "\n"
        if argv[:2] == ["sh", "-c"] and "sandbox-api nginx" in argv[2]:
            return self.api_units.get(host, "")
        if argv[:2] == ["sudo", "find"] and handlers.APT_CACHE_DIR in argv:
            return self.apt_cache
        if argv[:2] == ["sh", "-c"] and "is-active" in argv[2]:
            return "active\ninactive\n"
        return ""


CFG = {"dashboard_host": "10.5.0.18", "dashboard_media_root": "/opt/dashboard/media",
       "scoring_host": "10.5.0.14", "logging_host": "10.5.0.15"}


PREPARE = {"gameplan": "pair.py", "gameplan_sha256": PAIR_SHA, "run_uuid": RUN}
SANDBOX_VMIDS = {900, 901, 950}  # the two VMs and the gateway
MB = 2**20
# Their ceilings (maxmem) and what their QEMUs hold now (memhost), as Proxmox lists them.
SERVICE_VMS = [{"vmid": 111, "name": "scoring", "node": "n", "status": "running", "pool": "ephemeral",
                "maxmem": 512 * MB, "memhost": 239 * MB},
               {"vmid": 100, "name": "apt-cache", "node": "n", "status": "running", "pool": "private_internet",
                "maxmem": 1024 * MB, "memhost": 469 * MB}]  # fmt: skip
DEVBOX_VMS = [{"vmid": 116, "name": "devbox-t1-a", "node": "n", "status": "running", "pool": "devbox"},
              {"vmid": 117, "name": "devbox-t1-b", "node": "n", "status": "stopped", "pool": "devbox"}]  # fmt: skip


def _sandbox(ex, cfg=None):
    """What an attempt's sandbox gets before its prepare, as `worms round start` asks for it (the
    conductor's build_sandbox, run_attempt): built and snapshotted when this gameplan's sandbox
    with its snapshot is not there (build, snapshot), else rolled back to that snapshot. The
    build job's result."""
    version = {"gameplan": PREPARE["gameplan"], "gameplan_sha256": PREPARE["gameplan_sha256"]}
    built = handlers.build(ex, cfg or CFG, validation.validate("build", version))
    if built["snapshot_due"]:
        built["snapshot"] = handlers.snapshot(ex, cfg or CFG, validation.validate("snapshot", {}))
    else:
        built["rollback"] = handlers.rollback(ex, cfg or CFG, validation.validate("rollback", version))
    return built


def _prepare(ex, cfg=None, booted=None):
    """The run's prepare job, on its sandbox (_sandbox). With `booted` (the test's tmp_path): the
    attempt begins (its sandbox's reset: the snapshot or the rollback) when the host has been up
    1000 s, its sandbox's VMs and gateway boot 10 to 19 s later (each a QEMU of its own), and the
    check comes 300 s after it began."""
    if booted is None:
        ex.built = _sandbox(ex, cfg)
        return handlers.prepare(ex, cfg or CFG, validation.validate("prepare", PREPARE))
    (_host(booted)["PROC"] / "uptime").write_text("1000.00 3600.00\n")
    ex.built = _sandbox(ex, cfg)
    result = handlers.prepare(ex, cfg or CFG, validation.validate("prepare", PREPARE))
    for vmid in SANDBOX_VMIDS:
        _fake_qemu(booted, vmid, 4000 + vmid, started=1010 + vmid % 10)
    (_host(booted)["PROC"] / "uptime").write_text("1300.00 3600.00\n")
    return result


def _first_index(steps, prefix):
    return next(i for i, step in enumerate(steps) if step.startswith(prefix))


def test_prepare_builds_the_gameplans_sandbox_and_reports_its_topology(tmp_path):
    ex = FakeExecutor()
    ex.sandbox_built = False
    third = {"vmid": 118, "name": "devbox-t1-c", "node": "n", "status": "running", "pool": "devbox"}
    fourth = {"vmid": 119, "name": "devbox-t1-gw", "node": "n", "status": "stopped", "pool": "devbox"}
    ex.pools.update(ephemeral=[SERVICE_VMS[0]], devbox=[*DEVBOX_VMS, third, fourth])
    adjust = {vmid: _fake_qemu(tmp_path, vmid, 4000 + vmid, threads=[5000 + vmid]) for vmid in (900, 901, 950, 116, 117, 111)}
    # A pid file left behind, naming a process that is not this VM's QEMU (any more).
    other = _fake_qemu(tmp_path, 199, 4700)
    (_host(tmp_path)["QEMU_PIDS"] / "118.pid").write_text("4700\n")
    result = _prepare(ex)
    # The OOM killer's first choice is a team VM: every sandbox VM (the gateway too) and devbox VM
    # with a QEMU, not a service VM, and nothing but the QEMU its pid file names, if it is that VM's.
    # 117: the resource list (up to 10 s old) says stopped, but it has a QEMU, which decides.
    assert {vmid: path.read_text().strip() for vmid, path in adjust.items()} == {
        900: "1000", 901: "1000", 950: "1000", 116: "1000", 117: "1000", 111: "0"}  # fmt: skip
    assert other.read_text() == "0\n" and any("no QEMU of VM 118" in note for note in ex.notes)
    assert not any("VM 119" in note for note in ex.notes)  # stopped, so no QEMU is expected: nothing to say
    record = json.loads((Path(handlers.STATE_DIR) / host_state.RECORD).read_text())
    assert record["900"]["pid"] == 4900 and record["900"]["tids"] == [4900, 5900] and "111" not in record
    assert oct((Path(handlers.STATE_DIR) / host_state.RECORD).stat().st_mode & 0o777) == "0o600"
    assert result["gateway"] == {"name": "sandbox-gw", "vmid": 950}  # the conductor watches it too
    assert any(c[:2] == ("tofu", "sandbox") and "apply" in c for c in ex.calls)
    assert ("worms", "round-configure-sandbox") in ex.calls  # inventory, the CA, the gameplan's packages
    assert f'gameplan_sha256 = "{PAIR_SHA}"' in ex.installed_hcl and '"sandbox-b" = {' in ex.installed_hcl
    assert {vm["name"] for vm in result["vms"]} == {"sandbox-a", "sandbox-b"}
    assert result["vms"][0]["networks"] == [{"name": "net0", "cidr": "10.64.0.0/24"}]
    assert result["entrypoint"] == "sandbox-a"
    # The sandbox as a graph, for the replay; nothing of the gameplan's files, commands or rules.
    assert any(n["type"] == "network" for n in result["topology"]["nodes"])
    assert "sandbox_definition" not in result and "alice" not in json.dumps(result)
    assert result["sandbox"] == "as its snapshot holds it" and ex.built["sandbox"] == "built and configured" and ex.built["snapshot"]["taken"]


def test_a_failed_build_tells_the_database_nothing_of_the_gameplan():
    """Ansible prints the setup command that failed; the job's error (which the dashboard can
    read) names the job and where it failed (an exception's type, our file and line), never what
    was said: that goes to the job log on the host, and stays with the error for the host's own
    accounts (`original`: the round session's terminal and summary.log)."""
    from round_agent import failure

    class Failing(FakeExecutor):
        def worms(self, component, timeout=3600, args=None):
            raise RuntimeError('ansible-playbook exited 2: failed: [sandbox-a] => {"cmd": "echo hunter2 | chpasswd"}')

    ex = Failing()
    ex.sandbox_built = False
    with pytest.raises(RuntimeError) as caught:
        _prepare(ex)
    assert str(caught.value).startswith("the build job failed: RuntimeError raised at ") and "hunter2" not in str(caught.value)
    assert any("hunter2" in note for note in ex.notes)
    cause = failure.from_exception(caught.value)
    assert "hunter2" in cause.headline and "hunter2" not in json.dumps(failure.for_database(caught.value, cause))


def test_the_gameplans_packages_and_files_reach_ansible_unsafe(monkeypatch):
    """Ansible renders the varfile on the host as root: a gameplan's text is never templated."""
    seen = {}

    class Capture(FakeExecutor):
        def worms(self, component, timeout=3600, args=None):
            super().worms(component, timeout, args)
            if component == "round-configure-sandbox":
                seen["text"] = Path(args.split("@", 1)[1]).read_text()

    ex = Capture()
    ex.sandbox_built = False
    _prepare(ex)
    assert "!unsafe" in seen["text"]
    config = _load_varfile_text(seen["text"])["sandbox_vm_config"]
    assert config["sandbox-b"]["install"] == ["jq"] and config["sandbox-b"]["users"] == ["alice"]


def test_a_run_gets_its_own_keys_password_files_and_flags_before_the_worm():
    ex = FakeExecutor()
    result = _prepare(ex)
    calls = ex.ssh_calls
    password = next(c for c in calls if c["argv"] == ["sudo", "chpasswd"])
    assert password["host"] == "10.4.0.11" and password["input"].startswith("alice:")
    secret = password["input"].strip().split(":", 1)[1]
    # Root's, as the gameplan names no user: the worm starts as root.
    private = next(c for c in calls if c["argv"][-1] == "/root/.ssh/id_ed25519")
    assert private["host"] == "10.4.0.10" and "PRIVATE KEY" in private["input"] and "0600" in private["argv"]
    # ...and held by root's ssh-agent there, a service of the VM, checked to hold it.
    agent = next(c for c in calls if c["argv"] == ["sudo", "sh", "-s"])
    assert agent["host"] == "10.4.0.10" and "systemctl enable --quiet worms-ssh-agent@root" in agent["input"]
    assert "printf '%s\\n' /root/.ssh/id_ed25519 > /etc/worms/ssh-agent/root" in agent["input"]
    assert "Match localuser root\n    IdentityAgent /root/.ssh/agent.sock" in agent["input"]
    assert calls.index(agent) > calls.index(private)
    public = next(c for c in calls if c["argv"] == ["sudo", "tee", "-a", "/root/.ssh/authorized_keys"])
    assert public["host"] == "10.4.0.11" and public["input"] == "ssh-ed25519 AAAAPUBLIC worms-gameplan\n"
    note = next(c for c in calls if c["argv"][-1] == "/root/note")
    assert note["input"] == f"alice {secret}" and note["argv"][note["argv"].index("-o") + 1] == "root"
    assert not any(c["argv"][-1:] == ["/root/later"] for c in calls)  # declared for later only
    # A flag on every VM, read back.
    assert {r["vm"] for r in result["released"]} == {"sandbox-a", "sandbox-b"}
    for entry in result["released"]:
        host = {"sandbox-a": "10.4.0.10", "sandbox-b": "10.4.0.11"}[entry["vm"]]
        assert hashlib.sha256(ex.flags[host].encode()).hexdigest() == entry["flag_hash"]
    # The run's secrets stay on the host, root only, and nothing of them is in the result.
    state = Path(handlers.STATE_DIR) / "runs" / f"{RUN}.json"
    assert oct(state.stat().st_mode & 0o777) == "0o600" and secret in state.read_text()
    assert secret not in json.dumps(result)


def test_every_run_gets_new_secrets():
    ex = FakeExecutor()
    _prepare(ex)
    first = next(c for c in ex.ssh_calls if c["argv"] == ["sudo", "chpasswd"])["input"]
    ex.ssh_calls.clear()
    _prepare(ex)
    assert next(c for c in ex.ssh_calls if c["argv"] == ["sudo", "chpasswd"])["input"] != first


def test_the_host_runs_only_the_gameplan_the_orchestrator_validated():
    ex = FakeExecutor()
    with pytest.raises(Exception, match="not the version expected"):
        handlers.prepare(ex, CFG, validation.validate("prepare", {**PREPARE, "gameplan_sha256": "0" * 64}))
    with pytest.raises(Exception, match="no gameplan"):
        handlers.prepare(ex, CFG, validation.validate("prepare", {**PREPARE, "gameplan": "other.py"}))
    assert not any(c[0] == "tofu" for c in ex.calls)
    # The preflight of another version proves nothing about this one.
    with pytest.raises(handlers.Refused, match="not the version expected"):
        handlers.preflight(ex, CFG, validation.validate("preflight", {**PREPARE, "gameplan_sha256": "0" * 64, "hold_devboxes": False}))


def test_the_gameplans_job_lists_files_hashes_and_validity_but_no_reasons(tmp_path):
    root = tmp_path / "repo"
    (root / "gameplans").mkdir(parents=True)
    (root / "gameplans" / "pair.py").write_text(PAIR)
    (root / "gameplans" / "broken.py").write_text("raise SystemError('secret detail')\n")
    ex = FakeExecutor()
    ex.repo_root = root
    result = handlers.gameplans(ex, CFG, validation.validate("gameplans", {}))
    assert result == {"gameplans": [
        {"file": "broken.py", "sha256": hashlib.sha256(b"raise SystemError('secret detail')\n").hexdigest(), "valid": False},
        {"file": "pair.py", "sha256": PAIR_SHA, "valid": True},
    ]}  # fmt: skip
    assert any("secret detail" in note for note in ex.notes)  # the reason: in the job log only


def test_a_new_sandbox_is_snapshotted_before_any_team_reaches_it():
    """Every VM, its gateway too, shut down first: no guest agent, so a running one would be
    snapshotted like a power cut. No run's flag or secret is in the snapshot."""

    class Watching(FakeExecutor):
        def run(self, argv, check=False, timeout=60):
            if argv[:2] == ["qm", "snapshot"]:
                assert not self.flags and not any(c["argv"] == ["sudo", "chpasswd"] for c in self.ssh_calls)
            return super().run(argv, check, timeout)

    ex = Watching()
    ex.sandbox_built = False
    _prepare(ex)
    steps = ex.qm_steps()
    for vmid in SANDBOX_VMIDS:
        assert steps.index(f"qm shutdown {vmid} --timeout 120 --forceStop 1") < _first_index(steps, f"qm snapshot {vmid} clean")
        assert f"qm start {vmid}" in steps
    assert ex.clean == SANDBOX_VMIDS
    assert not any("systemd-run" in c["argv"] for c in ex.ssh_calls)  # nor a worm


def test_the_next_run_of_the_round_rolls_the_sandbox_back():
    """The same seed and parameters: no build, the snapshot, and nothing of the team before."""
    ex = FakeExecutor()
    ex.sandbox_built = False
    _prepare(ex)
    ex.calls.clear()
    result = _prepare(ex)
    assert not any(c[0] in ("tofu", "worms") and c[1:2] != ("output",) and "output" not in c for c in ex.calls)
    assert sorted(ex.qm_steps()) == sorted(f"qm rollback {vmid} clean --start 1" for vmid in SANDBOX_VMIDS)
    assert ex.built["sandbox"].startswith("kept") and sorted(ex.built["rollback"]["rolled_back"]) == ["sandbox-a", "sandbox-b", "sandbox-gw"]
    assert result["sandbox"] == "as its snapshot holds it" and {vm["name"] for vm in result["vms"]} == {"sandbox-a", "sandbox-b"}


@pytest.mark.parametrize("change", ["another round", "a missing snapshot"])
def test_a_sandbox_that_is_not_the_rounds_or_not_whole_is_destroyed_and_built_again(change):
    ex = FakeExecutor()
    ex.installed_hcl = "locals { seed = \"another round\" }" if change == "another round" else _pair_hcl()
    ex.clean = set() if change == "another round" else SANDBOX_VMIDS - {901}
    _prepare(ex)
    tofu = [c[2] for c in ex.calls if c[:2] == ("tofu", "sandbox") and c[2] in ("destroy", "apply")]
    assert tofu == ["destroy", "apply"]
    assert not any("rollback" in step for step in ex.qm_steps())


def test_a_new_sandbox_is_built_on_reset_services_with_the_devboxes_off():
    """What it installs comes from the private internet, which team code may have changed:
    back to its snapshot first, and no devbox running until the sandbox is snapshotted (the
    build leaves them off, the snapshot step starts them)."""
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
    assert ex.built["services"].startswith("reset: 2 VMs") and ex.built["snapshot"]["devboxes_started"] == ["devbox-t1-a"]
    assert result["sandbox"] == "as its snapshot holds it"


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
    # Pushed once onto each, from the host's copy (devbox_map.push): no host job, no database.
    assert sorted(c["host"] for c in pushed) == ["10.5.0.14", "10.5.0.15"]
    assert all(c["input"] == "10.2.0.33 11111111-1111-1111-1111-111111111111\n" for c in pushed)


def test_services_without_a_snapshot_are_left_as_they_are_and_reported():
    ex = FakeExecutor()
    ex.sandbox_built = False
    ex.pools.update(ephemeral=[SERVICE_VMS[0]], private_internet=[SERVICE_VMS[1]])
    ex.clean = {111}
    _prepare(ex)
    assert not any("rollback" in step for step in ex.qm_steps())
    services = ex.built["services"]
    assert "not reset" in services and "apt-cache" in services and "rebuild services" in services


def test_the_devboxes_come_back_when_the_build_fails():
    ex = FakeExecutor()
    ex.sandbox_built = False
    ex.pools.update(devbox=list(DEVBOX_VMS))
    ex.fail_on = "apply"
    with pytest.raises(RuntimeError, match="build job failed"):
        _prepare(ex)
    assert any("apply failed" in note for note in ex.notes)  # the reason: in the job log
    assert ex.qm_steps()[-1] == "qm start 116"


@pytest.mark.parametrize("built", [False, True])
def test_no_sandbox_is_started_while_internet_access_is_allowed(tmp_path, built):
    """OpenTofu refuses a build then, but a rollback starts VMs past it."""
    (tmp_path / "internet-allowed").write_text("allowed\n")
    ex = FakeExecutor()
    ex.pools.update(devbox=list(DEVBOX_VMS))
    if built:
        ex.installed_hcl, ex.clean = _pair_hcl(), set(SANDBOX_VMIDS)
    with pytest.raises(RuntimeError, match="internet access is allowed"):
        _prepare(ex)
    assert ex.qm_steps() == [] and not any(c[0] == "tofu" and "output" not in c for c in ex.calls)


PREFLIGHT = {"gameplan": "pair.py", "gameplan_sha256": PAIR_SHA, "hold_devboxes": True}


def _stand_ins(tmp_path):
    """A VM's commands for the probe and the start and initialisation check, on this machine: apt,
    the clock, systemd, cloud-init, ip and nft answer as on a VM of the image (Debian 13, read on
    the platform's dns VM), and change nothing. WORMS_TEST_SLOW: apt and cloud-init take their
    time (cloud-init still running), and systemd is still starting (a slow unit's job left)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "apt-get": 'echo "Hit:1 http://apt-cache trixie InRelease"\n'
                   'if [ -n "$WORMS_TEST_SLOW" ]; then sleep 30 & echo $! > "$WORMS_TEST_PIDS/sleep"; wait; fi',
        # A VM whose gameplan took its time service away (WORMS_TEST_NO_NTP) has no NTP to ask.
        "timedatectl": 'printf "NTP=%s\\nNTPSynchronized=%s\\nCanNTP=yes\\n" "${WORMS_TEST_NO_NTP:-yes}" "${WORMS_TEST_NO_NTP:-yes}"',
        # Booted, but a unit nothing rests on failed (the serial getty of a VM without a serial port).
        # Slow: still starting, a timer's catch-up still running and the target waiting for it.
        "systemctl": 'case "$1" in\n'
                     '  is-active) case "$2" in cron) echo active ;; *) echo inactive; exit 3 ;; esac ;;\n'
                     '  is-system-running) if [ -n "$WORMS_TEST_SLOW" ]; then echo starting; else echo degraded; fi; exit 1 ;;\n'
                     '  show) if [ -n "$WORMS_TEST_SLOW" ]; then printf "NJobs=2\\nSystemState=starting\\n"; '
                     'else printf "NJobs=0\\nSystemState=degraded\\n"; fi ;;\n'
                     "  list-units) echo '[{\"unit\":\"serial-getty@ttyS0.service\",\"load\":\"loaded\",\"active\":\"failed\","
                     "\"sub\":\"failed\",\"description\":\"Serial Getty on ttyS0\"}]' ;;\n"
                     "  list-jobs) printf '118 multi-user.target start waiting\\n131 fstrim.service start running\\n' ;;\n"
                     'esac',
        # Done, with recoverable errors (exit 2), and a word on stderr first; slow: still running.
        "cloud-init": 'if [ -n "$WORMS_TEST_SLOW" ] && [ "$2" = "--wait" ]; then\n'
                      '  sleep 30 & echo $! > "$WORMS_TEST_PIDS/cloud-init"; wait\nfi\n'
                      'if [ -n "$WORMS_TEST_SLOW" ]; then\n'
                      "  echo '{\"status\": \"running\", \"extended_status\": \"running\", \"errors\": [], \"recoverable_errors\": {}, "
                      "\"stage\": \"modules-final\"}'; exit 0\nfi\n"
                      'echo "WARNING: the stand-in says so" >&2\n'
                      "echo '{\"boot_status_code\": \"enabled-by-generator\", \"status\": \"done\", \"extended_status\": \"degraded done\", "
                      "\"errors\": [], \"recoverable_errors\": {\"DEPRECATED\": [\"a key\", \"another\"]}, \"stage\": null}'; exit 2",
        # Two NICs, the second down, and a VLAN on the first (its MAC, no address); a default route.
        # (`ip -4` would leave each link's MAC out.)
        "ip": 'case "$*" in\n'
              "  '-j addr show') echo '[{\"ifindex\":1,\"ifname\":\"lo\",\"flags\":[\"LOOPBACK\",\"UP\",\"LOWER_UP\"],\"link_type\":\"loopback\","
              "\"address\":\"00:00:00:00:00:00\",\"addr_info\":[{\"family\":\"inet\",\"local\":\"127.0.0.1\",\"prefixlen\":8},"
              "{\"family\":\"inet6\",\"local\":\"::1\",\"prefixlen\":128}]},"
              "{\"ifindex\":2,\"ifname\":\"eth0\",\"flags\":[\"BROADCAST\",\"MULTICAST\",\"UP\",\"LOWER_UP\"],\"link_type\":\"ether\","
              "\"address\":\"bc:24:11:03:84:00\",\"altnames\":[\"enp0s18\"],\"addr_info\":[{\"family\":\"inet\",\"local\":\"10.64.0.10\","
              "\"prefixlen\":24,\"scope\":\"global\",\"label\":\"eth0\"},{\"family\":\"inet6\",\"local\":\"fe80::be24:11ff:fe03:8400\","
              "\"prefixlen\":64,\"scope\":\"link\"}]},{\"ifindex\":3,\"ifname\":\"eth1\",\"flags\":[\"BROADCAST\",\"MULTICAST\"],"
              "\"link_type\":\"ether\",\"address\":\"bc:24:11:03:84:01\",\"addr_info\":[{\"family\":\"inet\",\"local\":\"10.4.0.10\","
              "\"prefixlen\":16}]},{\"ifindex\":4,\"link\":\"eth0\",\"ifname\":\"eth0.5\",\"flags\":[\"BROADCAST\",\"MULTICAST\",\"UP\","
              "\"LOWER_UP\"],\"link_type\":\"ether\",\"address\":\"bc:24:11:03:84:00\",\"addr_info\":[]}]' ;;\n"
              "  '-j -4 route show default') echo '[{\"dst\":\"default\",\"gateway\":\"10.64.0.1\",\"dev\":\"eth0\",\"protocol\":\"static\","
              "\"flags\":[]}]' ;;\n"
              '  *) exit 1 ;;\nesac',
        # A forward chain that lets everything through.
        "nft": "echo '{\"nftables\": [{\"metainfo\": {\"version\": \"1.1.3\", \"json_schema_version\": 1}}, {\"table\": {\"family\": "
               "\"inet\", \"name\": \"sandbox_gw\", \"handle\": 1}}, {\"chain\": {\"family\": \"inet\", \"table\": \"sandbox_gw\", \"name\": "
               "\"forward\", \"handle\": 2, \"type\": \"filter\", \"hook\": \"forward\", \"prio\": 0, \"policy\": \"accept\"}}]}'",
    }.items():  # fmt: skip
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    return bin_dir


def test_check_reports_ok_when_everything_is_up(tmp_path):
    import http.server
    import pwd
    import socket
    import threading

    ex = FakeExecutor()
    result = handlers.check(ex, CFG, validation.validate("check", {}))
    assert result["ok"] is True and result["failures"] == []
    # Without the run, the host alone: each VM and the gateway in the pool and running, its QEMU too;
    # the CPU of each but the gateway. Nothing is asked of a VM.
    assert {c["check"]: c["detail"] for c in result["checks"] if c["check"].startswith("vm:")} == {
        "vm:sandbox-a:start": "running", "vm:sandbox-a:no-kvm": "its CPU must be x86-64-v3", "vm:sandbox-b:start": "running",
        "vm:sandbox-b:no-kvm": "its CPU must be x86-64-v3", "vm:sandbox-gw:start": "running"}  # fmt: skip
    assert ex.probes == {} and ex.inits == {}

    # Before an attempt: the host can take the gameplan's sandbox, and everything around it is up.
    _fake_memory(tmp_path, available_mb=20000)
    ex.pools.update(ephemeral=[SERVICE_VMS[0]], private_internet=[SERVICE_VMS[1]])
    ex.clean = {100, 111}
    ex.states = {vmid: ("stopped", None) for vmid in SANDBOX_VMIDS}  # the round's sandbox, kept powered off
    # A workspace the sync left without the platform's addresses (no sandbox was built or destroyed
    # there yet): the inventory is made first, as a build would.
    workspace = tmp_path / "workspace"
    (workspace / "gameplans").mkdir(parents=True)
    (workspace / "gameplans" / "pair.py").write_text(PAIR)
    ex.repo_root = workspace
    flight = handlers.preflight(ex, CFG, validation.validate("preflight", PREFLIGHT))
    assert ("worms", "inventory") in ex.calls and (workspace / handlers.INVENTORY).is_file()
    ex.repo_root = GAMEPLAN_ROOT
    assert flight["ok"] is True and flight["failures"] == [] and flight["warnings"] == []
    # Each VM at its ceiling, the gateway too, and what each of their QEMUs holds beyond it.
    needed = 512 + 512 + handlers.SANDBOX_GW_MEMORY_MB + 3 * handlers.QEMU_OVERHEAD_MB
    assert flight["memory"] == {"available_mb": 20000 + 1024, "needed_mb": needed, "reserve_mb": 1024, "total_mb": 32000}
    assert {c["check"] for c in flight["checks"]} == {
        "host:internet-window-closed", *(f"host:{unit}" for unit in handlers.HOST_UNITS), "host:cluster-firewall-enabled",
        "services:scoring:running", "services:apt-cache:running", "services:scoring:sandbox-api", "services:scoring:nginx",
        "services:logging:sandbox-api", "services:logging:nginx", "devboxes:held", "sandbox:not-running", "memory:admission",
        "packages:in-cache",
    }  # fmt: skip
    assert next(c["detail"] for c in flight["checks"] if c["check"] == "host:internet-window-closed") == ""
    assert next(c for c in ex.ssh_calls if c["argv"][:2] == ["sudo", "find"])["host"] == "10.5.0.20"  # apt-cache, by the inventory
    assert "jq" not in json.dumps(flight)  # what a gameplan installs is its content
    # Room for the sandbox, but not for the service VMs growing to their ceilings as well (a worm
    # reaches them): a warning, not a failure. scoring can take 512 + 40 - 239 MB more, apt-cache 595.
    _fake_memory(tmp_path, available_mb=2300)  # and 1024 MB of the ARC
    flight = handlers.preflight(ex, CFG, validation.validate("preflight", PREFLIGHT))
    assert flight["ok"] is True and len(flight["warnings"]) == 1, flight["warnings"]
    assert "grow by 908 MB" in flight["warnings"][0] and "apt-cache 595 MB, scoring 313 MB" in flight["warnings"][0]
    assert "with that, 264 MB would be missing" in flight["warnings"][0]  # 1656 + 1024 + 908 - 3324

    # Right before the worm, with the run: every VM of its gameplan and the gateway started in this
    # attempt and came up as they should, then every VM of the gameplan looked at from inside, all at once.
    ex.states = {}
    _prepare(ex, booted=tmp_path)
    assert json.loads(handlers._run_state_path({}, RUN).read_text())["began"] == {"boot_id": HOST_BOOT, "uptime": 1000.0,
                                                                                  "at": pytest.approx(time.time(), abs=60)}  # fmt: skip
    result = handlers.check(ex, CFG, validation.validate("check", {"run_uuid": RUN}))
    assert result["ok"] is True, result["failures"]
    names = {c["check"] for c in result["checks"]}
    detail = {c["check"]: c["detail"] for c in result["checks"]}
    for vm in ("sandbox-a", "sandbox-b", "sandbox-gw"):
        assert {f"vm:{vm}:{p}" for p in ("start", "init", "init:cloud-init", "init:boot", "init:addresses", "init:route", "init:sshd")} <= names
    for vm in ("sandbox-a", "sandbox-b"):
        assert {f"vm:{vm}:{p}" for p in ("probe", "flag", "dns", "scoring", "logging", "apt", "pypi", "isolated", "firewall")} <= names
    assert {"vm:sandbox-b:files", "vm:sandbox-b:init:users", "vm:sandbox-b:init:services", "host:inventory"} <= names
    assert "vm:sandbox-a:files" not in names  # its one file is written later, not at the reset
    assert "vm:sandbox-a:init:users" not in names and "vm:sandbox-a:init:services" not in names  # it has none
    assert {name.split(":", 2)[2] for name in names if name.startswith("vm:sandbox-gw:")} == {
        "start", "init", "init:cloud-init", "init:boot", "init:addresses", "init:route", "init:sshd", "init:nftables", "init:sysctl"}
    assert detail["vm:sandbox-a:start"] == "running, booted in this attempt (its QEMU started 10 s after the sandbox's reset, 290 s ago)"
    assert [detail[f"vm:sandbox-b:init:{p}"] for p in ("cloud-init", "boot", "addresses", "route", "sshd", "users", "services")] == [
        "done", "running", "2 NICs, each with its address", "via its network's gateway", "sshd listens on its SSH port", "1 there",
        "1 running"]  # fmt: skip
    assert [detail[f"vm:sandbox-gw:init:{p}"] for p in ("addresses", "route", "sshd", "nftables", "sysctl")] == [
        "3 NICs, each with its address", "none, as its stack has it", "sshd listens on 22",
        "4 chains as its cloud-init has them (forward: policy drop)", "net.ipv4.ip_forward 1, net.netfilter.nf_conntrack_max 262144"]
    assert next(c for c in ex.ssh_calls if c["argv"][:2] == ["sudo", "python3"])["argv"] == ["sudo", "python3", "-I", "-B", "-"]
    # Each VM is asked to come up first, and probed after.
    asked = [(c["host"], probe.params_of(c["input"]).get("kind")) for c in ex.ssh_calls if c["argv"][:2] == ["sudo", "python3"]]
    assert all(asked.index((host, "init")) < asked.index((host, None)) for host in ("10.4.0.10", "10.4.0.11"))
    assert ("10.4.0.2", None) not in asked  # the gateway has no probe
    spec = handlers._gameplan(ex, "pair.py", PAIR_SHA)[0]
    address = {vm["name"]: vm["nics"][0]["address"].split("/")[0] for vm in spec["vms"]}
    # Each NIC by the MAC the host's config gives it, in the stack's order: the gameplan's networks, the
    # management NIC. The gateway's: one per sandbox network, the services, the management.
    init_b, init_gw = ex.inits["10.4.0.11"], ex.inits["10.4.0.2"]
    assert init_b["nics"] == [["net0", _mac(901, 0), f"{address['sandbox-b']}/24"], ["net1", _mac(901, 1), "10.4.0.11/16"]]
    assert (init_b["gateway"], init_b["ssh_port"], init_b["users"], init_b["services"]) == ("10.64.0.1", 22, ["alice"], ["cron"])
    assert init_b["chains"] == [] and init_b["sysctl"] == [] and init_b["wait"] == startup.WAIT
    assert init_gw["nics"] == [["net0", _mac(950, 0), "10.64.0.1/24"], ["net1", _mac(950, 1), "10.2.0.2/24"],
                               ["net2", _mac(950, 2), "10.4.0.2/16"]]  # fmt: skip
    assert init_gw["gateway"] is None and init_gw["users"] == [] and init_gw["sysctl"] == list(startup.GATEWAY_SYSCTLS)
    assert init_gw["chains"] == [["inet", "sandbox_gw", "input", "input", "drop"], ["inet", "sandbox_gw", "forward", "forward", "drop"],
                                 ["inet", "sandbox_gw", "output", "output", "accept"], ["ip", "sandbox_gw_nat", "postrouting", "postrouting", None]]
    a, b = ex.probes["10.4.0.10"], ex.probes["10.4.0.11"]
    # The gameplan's firewall: a may reach b's SSH (both sides allow it), b may not reach a's.
    assert a["connect"] == [["0", address["sandbox-b"], 22, "open"]] and b["connect"] == [["0", address["sandbox-a"], 22, "blocked"]]
    assert a["forbidden"] == [["dashboard", "10.10.0.18", 443], ["database", "10.11.0.16", 5432], ["orchestrator", "10.11.0.19", 443],
                              ["host", "10.4.0.1", 8006], ["internet", "1.1.1.1", 80], ["devbox", "10.2.0.33", 80],
                              ["maintenance", "10.4.0.11", 22]]  # fmt: skip
    assert a["dns"] == {"scoring.worms.internal": "10.2.0.14", "logging.worms.internal": "10.2.0.15",
                        "apt-cache.worms.internal": "10.2.0.20", "pypi.worms.internal": "10.2.0.21"}  # fmt: skip
    assert b["files"] == [["0", "/root/note", "root", "0644"]] and a["files"] == [] and "services" not in b  # init's
    assert a["pypi"] == ["pypi.worms.internal", 3141, "/root/pypi/+simple/requests/"]
    # The database (which the dashboard reads) gets none of the gameplan: no path, user, unit or rule.
    text = json.dumps(result)
    assert "/root/note" not in text and "alice" not in text and "cron" not in text and address["sandbox-b"] not in text
    # A gateway built before a deploy changed its template (the round's sandbox is not rebuilt for that;
    # here: built before nf_conntrack_max was set): read against the cloud-init it was built with, its
    # snippet on the host, not against the template in the workspace.
    older = tmp_path / "sandbox-gw.yml"
    older.write_text("".join(line for line in GATEWAY_TEMPLATE.read_text().splitlines(True) if "nf_conntrack_max" not in line))
    ex.snippets["local:snippets/sandbox-gw.yml"] = older
    result = handlers.check(ex, CFG, validation.validate("check", {"run_uuid": RUN}))
    assert result["ok"] is True and ex.inits["10.4.0.2"]["sysctl"] == ["net.ipv4.ip_forward"]
    assert next(c["detail"] for c in result["checks"] if c["check"] == "vm:sandbox-gw:init:sysctl") == "net.ipv4.ip_forward 1"
    assert ("run", "pvesm", "path", "local:snippets/sandbox-gw.yml") in ex.calls

    # The probe itself, run here as on a VM, against stand-ins on this machine: a port that answers,
    # one that refuses, a page, and commands of its own on PATH. Nothing reaches out, nothing changes.
    listening = socket.create_server(("127.0.0.1", 0))
    closed = socket.create_server(("127.0.0.1", 0))
    open_port, closed_port = listening.getsockname()[1], closed.getsockname()[1]
    closed.close()

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/root/pypi/+simple/requests/" else 404)
            self.end_headers()

        def log_message(self, *args):
            pass

    page = http.server.HTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=page.serve_forever, daemon=True).start()
    (tmp_path / "flag").write_text("FLAG{0123}")
    (tmp_path / "note").write_text("n")
    (tmp_path / "note").chmod(0o644)
    me = pwd.getpwuid(os.getuid()).pw_name
    (tmp_path / "pids").mkdir()
    params = {
        "deadline": 40, "apt_timeout": 20, "connect_timeout": 1, "flag": str(tmp_path / "flag"), "tmp": str(tmp_path),
        "dns": {"localhost": "127.0.0.1"}, "https": {"scoring": ["127.0.0.1", closed_port], "logging": ["127.0.0.1", closed_port]},
        "pypi": ["127.0.0.1", page.server_address[1], "/root/pypi/+simple/requests/"],
        "connect": [["0", "127.0.0.1", open_port, "open"], ["1", "127.0.0.1", closed_port, "reachable"], ["2", "127.0.0.1", closed_port, "open"]],
        "forbidden": [["internet", "127.0.0.1", open_port]],
        "files": [["0", str(tmp_path / "note"), me, "0644"], ["1", str(tmp_path / "gone"), me, "0644"],
                  ["2", str(tmp_path / "note"), "nobody", "0600"]],
    }  # fmt: skip
    env = {"PATH": f"{_stand_ins(tmp_path)}:/usr/bin:/bin", "WORMS_TEST_PIDS": str(tmp_path / "pids")}
    try:
        ran = subprocess.run(["python3", "-I", "-B", "-"], input=probe.script(params), capture_output=True, text=True, timeout=60, env=env)
        reply = probe.answer(ran.stdout)
        assert reply is not None, ran.stderr
        assert [reply[key] for key in ("flag", "dns", "pypi", "apt")] == [[True, ""], [True, ""], [True, "HTTP 200"], [True, ""]]
        assert "time" not in reply  # no clock check (2026-11-24)
        assert reply["scoring"][0] is False and "Refused" in reply["scoring"][1]
        assert reply["connect"] == {"0": "open", "1": "refused", "2": "refused"} and reply["forbidden"] == {"internet": "open"}
        assert reply["files"] == {"0": "", "1": "missing", "2": f"owner {me}, mode 0644"} and "services" not in reply
        assert "deadline" not in reply and not list(tmp_path.glob("worms-check-*"))  # apt's directory is gone again
        # Its own deadline: it says what it has, and leaves no command running and nothing behind. What
        # it cut off stays absent (apt's step, its command killed, would say "exit -9").
        started = time.monotonic()
        ran = subprocess.run(["python3", "-I", "-B", "-"], input=probe.script({**params, "deadline": 3}), capture_output=True, text=True,
                             timeout=60, env={**env, "WORMS_TEST_SLOW": "1"})  # fmt: skip
        reply = probe.answer(ran.stdout)
        assert time.monotonic() - started < 10 and reply["deadline"] is True and "apt" not in reply and reply["flag"] == [True, ""]
        assert not list(tmp_path.glob("worms-check-*"))
        left = Path("/proc") / (tmp_path / "pids" / "sleep").read_text().strip() / "cmdline"  # apt's own child, killed with it
        for _ in range(20):
            if not left.exists() or not left.read_bytes():  # gone, or a zombie on its way out
                break
            time.sleep(0.1)
        assert not left.exists() or not left.read_bytes()

        # The start and initialisation check, run here the same way: cloud-init done with a recoverable
        # error, systemd degraded by a unit nothing rests on, a NIC down, one missing, a route as asked,
        # its SSH port held by this test (not sshd), a user that is not there, a forward chain that lets
        # everything through. It reads /proc and /proc/sys, and changes nothing.
        init = {
            "kind": "init", "deadline": 30, "wait": 5, "gateway": "10.64.0.1", "ssh_port": open_port, "users": [me, "nobody-here"],
            "nics": [["net0", _mac(900, 0).upper(), "10.64.0.10/24"], ["net1", _mac(900, 1), "10.4.0.10/16"], ["net2", _mac(900, 2), "10.2.0.2"]],
            "services": ["cron", "other"], "chains": [["inet", "sandbox_gw", "input", "input", "drop"], ["inet", "sandbox_gw", "forward", "forward", "drop"]],
            "sysctl": ["net.ipv4.ip_forward", "net.no.such"],
        }  # fmt: skip
        ran = subprocess.run(["python3", "-I", "-B", "-"], input=probe.script(init, startup.INIT), capture_output=True, text=True, timeout=60, env=env)
        reply = probe.answer(ran.stdout)
        assert reply is not None, ran.stderr
        assert {**reply["cloud-init"], "waited": 0} == {"code": 2, "status": "done", "extended": "degraded done", "errors": [],
                                                        "recoverable": {"DEPRECATED": 2}, "stage": None, "waited": 0, "late": False}
        assert {**reply["boot"], "waited": 0} == {"state": "degraded", "failed": ["serial-getty@ttyS0.service"], "pending": None,
                                                  "jobs": 0, "waited": 0, "late": False}  # fmt: skip
        # net0 is found though a VLAN on it carries its MAC too (without its address).
        assert reply["addresses"] == {"net0": "", "net1": "eth1 is down", "net2": f"no interface has its MAC address ({_mac(900, 2)})"}
        assert reply["route"] == [True, "via 10.64.0.1 dev eth0"] and reply["sshd"][0] is False
        assert reply["sshd"][1] == Path("/proc/self/comm").read_text().strip()  # this test's process: python, or pytest
        assert reply["users"] == {me: True, "nobody-here": False} and reply["services"] == {"cron": "active", "other": "inactive"}
        assert reply["nftables"] == {"inet sandbox_gw input": "missing", "inet sandbox_gw forward": "policy accept, not drop"}
        assert reply["sysctl"]["net.ipv4.ip_forward"] in ("0", "1") and reply["sysctl"]["net.no.such"] is None
        # What the host makes of it: a recoverable error and a failed getty are not failures.
        notes = {"router": False, "critical": set(startup.PLATFORM_UNITS) | {"cron.service"}, "theirs": {"cron.service"},
                 "sysctl": {"net.ipv4.ip_forward": "1"}}  # fmt: skip
        logged = []
        rows = {row[0]: row[1:] for row in startup.records("vm-a", init, notes, reply, logged.append)}
        assert rows["vm:vm-a:init"] == (True, "") and rows["vm:vm-a:init:cloud-init"] == (True, "done, with recoverable errors (2 DEPRECATED)")
        assert rows["vm:vm-a:init:boot"] == (True, "degraded: 1 unit failed, none that its start rests on (the job log on the host names them)")
        assert rows["vm:vm-a:init:addresses"][0] is False and rows["vm:vm-a:init:route"] == (True, "via its network's gateway")
        assert rows["vm:vm-a:init:sshd"] == (False, "sshd does not listen on its SSH port (the job log on the host says what does)")
        assert rows["vm:vm-a:init:users"] == (False, "1 of 2 missing (the job log on the host names them)")
        assert rows["vm:vm-a:init:services"] == (False, "1 of 2 not running (the job log on the host names them)")
        assert rows["vm:vm-a:init:nftables"] == (False, "inet sandbox_gw input: missing; inet sandbox_gw forward: policy accept, not drop")
        assert "check vm-a: serial-getty@ttyS0.service failed" in logged and "check vm-a: user nobody-here: missing" in logged
        # A boot still under way: cloud-init is waited for up to `wait`, then said to be running. systemd
        # still starting after the wait: the jobs it has left, by unit; a slow unit nothing rests on
        # passes, one the start rests on fails. Its own deadline cuts the rest off: what it has, no
        # command left running.
        slow = {**env, "WORMS_TEST_SLOW": "1"}
        ran = subprocess.run(["python3", "-I", "-B", "-"], input=probe.script({**init, "wait": 1, "ssh_port": closed_port}, startup.INIT),
                             capture_output=True, text=True, timeout=60, env=slow)  # fmt: skip
        reply = probe.answer(ran.stdout)
        assert reply["cloud-init"]["late"] is True and reply["cloud-init"]["status"] == "running" and reply["sshd"] == [False, "nothing listens"]
        assert startup._cloud_init("vm-a", {"wait": 1}, reply, logged.append) == (False, "running after 1 s (stage modules-final)")
        assert {**reply["boot"], "waited": 0} == {"state": "starting", "failed": ["serial-getty@ttyS0.service"], "jobs": 2, "waited": 0,
                                                  "pending": ["fstrim.service", "multi-user.target"], "late": False}  # fmt: skip
        assert startup._boot("vm-a", {"wait": 1}, notes, reply, logged.append) == (True, (
            "still starting after 1 s: 1 unit failed, none that its start rests on; 2 jobs to go, none for a unit its start rests on "
            "(the job log on the host names them)"))  # fmt: skip
        assert "check vm-a: fstrim.service still starting after 1 s" in logged
        waiting = {**reply, "boot": {**reply["boot"], "pending": ["cron.service", "multi-user.target", "ssh.service"]}}
        assert startup._boot("vm-a", {"wait": 1}, notes, waiting, logged.append) == (False, (
            "still starting after 1 s: ssh.service not started yet; 1 of the gameplan's services not started yet; 1 other unit failed; "
            "1 other job to go (the job log on the host names them)"))  # fmt: skip
        started = time.monotonic()
        ran = subprocess.run(["python3", "-I", "-B", "-"], input=probe.script({**init, "deadline": 2}, startup.INIT), capture_output=True,
                             text=True, timeout=60, env=slow)  # fmt: skip
        reply = probe.answer(ran.stdout)
        assert time.monotonic() - started < 10 and reply["deadline"] is True and "cloud-init" not in reply and "addresses" not in reply
        assert reply["boot"]["state"] == "starting"
        left = Path("/proc") / (tmp_path / "pids" / "cloud-init").read_text().strip() / "cmdline"
        for _ in range(20):
            if not left.exists() or not left.read_bytes():
                break
            time.sleep(0.1)
        assert not left.exists() or not left.read_bytes()
    finally:
        page.shutdown()
        listening.close()


def test_check_flags_a_down_vm_and_dead_service(tmp_path, monkeypatch):
    ex = FakeExecutor()
    ex.systemctl["worms-mirror"] = "inactive"
    # A VM stopped, one whose guest halted it (its QEMU is still there), and a gateway paused.
    ex.states = {900: ("stopped", None), 901: ("running", "internal-error"), 950: ("running", "paused")}
    result = handlers.check(ex, CFG, {})
    assert result["ok"] is False
    assert set(result["failures"]) == {"vm:sandbox-a:start", "vm:sandbox-b:start", "vm:sandbox-gw:start"}
    assert "host:worms-mirror" in result["warnings"]  # said, but not what decides (handlers.GATES)
    detail = {c["check"]: c["detail"] for c in result["checks"]}
    assert detail["vm:sandbox-a:start"] == "stopped"
    assert detail["vm:sandbox-b:start"] == "Proxmox says running, but its QEMU is internal-error (halted)"
    assert detail["vm:sandbox-gw:start"] == "Proxmox says running, but its QEMU is paused (not running)"
    # Not in the sandbox's pool, or another VM by now under its number.
    ex.states = {}
    ex.sandbox_pool = lambda: [{"vmid": 900, "name": "sandbox-a", "node": "n", "status": "running", "pool": "devbox"},
                               {"vmid": 901, "name": "sandbox-c", "node": "n", "status": "running", "pool": "sandbox"}]  # fmt: skip
    detail = {c["check"]: c["detail"] for c in handlers.check(ex, CFG, {})["checks"]}
    assert detail["vm:sandbox-a:start"] == "VM 900 is sandbox-a in the pool devbox, not sandbox-a in sandbox"
    assert detail["vm:sandbox-b:start"] == "VM 901 is sandbox-c in the pool sandbox, not sandbox-b in sandbox"
    assert detail["vm:sandbox-gw:start"] == "Proxmox has no VM 950"
    del ex.sandbox_pool

    # Before an attempt: each thing the host lacks for it fails the preflight, and says so.
    (tmp_path / "internet-allowed").write_text("allowed\n")
    _fake_memory(tmp_path, available_mb=600)  # and 1024 MB of the ARC: the sandbox takes 1656 MB (its ceilings, its QEMUs), 1024 more kept free
    stopped_service = {**SERVICE_VMS[1], "status": "stopped"}
    halted = {"vmid": 118, "name": "devbox-t1-c", "node": "n", "status": "running", "pool": "devbox"}
    ex.pools.update(ephemeral=[SERVICE_VMS[0]], private_internet=[stopped_service], devbox=[*DEVBOX_VMS, halted])
    ex.states = {118: ("running", "internal-error")}  # its guest halted it: its QEMU still holds its memory, so it is not off
    ex.cluster_firewall = 0
    ex.api_units["10.5.0.14"] = "inactive\nactive\n"
    ex.apt_cache = "curl_8.14.1-2_amd64.deb\n"  # no jq
    flight = handlers.preflight(ex, CFG, validation.validate("preflight", PREFLIGHT))
    assert flight["ok"] is False and set(flight["failures"]) == {
        "host:internet-window-closed", "host:worms-mirror", "host:cluster-firewall-enabled", "services:apt-cache:running",
        "services:scoring:sandbox-api", "devboxes:held", "sandbox:not-running", "memory:admission", "packages:in-cache",
    }  # fmt: skip
    detail = {c["check"]: c["detail"] for c in flight["checks"]}
    assert detail["devboxes:held"] == "not off: devbox-t1-a, devbox-t1-c"
    assert detail["sandbox:not-running"].startswith("not off: sandbox-a")
    assert detail["host:internet-window-closed"].endswith("exists: internet access is allowed (`worms internet off`)")
    assert flight["memory"]["available_mb"] == 1624 and flight["memory"]["needed_mb"] == 1656
    assert detail["memory:admission"] == ("1624 MB available (600 MB free and 1024 MB the ZFS ARC gives back); 1656 MB for the "
                                          "sandbox (the ceilings of its 2 VMs and the gateway, and 120 MB for their QEMUs) and "
                                          "1024 MB to spare")  # fmt: skip
    assert "jq" not in json.dumps(flight) and any("no .deb of jq" in note for note in ex.notes)  # names: in the job log only
    assert len(flight["warnings"]) == 2 and all("no clean snapshot" in w for w in flight["warnings"])
    # Without the hold, devboxes may run; a later state (the sandbox powered off) passes.
    ex.states = {vmid: ("stopped", None) for vmid in SANDBOX_VMIDS}
    flight = handlers.preflight(ex, CFG, validation.validate("preflight", {**PREFLIGHT, "hold_devboxes": False}))
    assert "devboxes:held" not in {c["check"] for c in flight["checks"]} and "sandbox:not-running" not in flight["failures"]

    # Right before the worm: a VM that is not as its gameplan says fails its probe's records, and its
    # service that failed at boot its start and initialisation records.
    (tmp_path / "internet-allowed").unlink()
    ex = FakeExecutor()
    _prepare(ex, booted=tmp_path)

    def broken(host, params, reply):
        if host == "10.4.0.11":  # sandbox-b
            reply["files"]["0"] = "owner debian, mode 0600"
            reply["forbidden"]["database"] = "refused"  # an answer: it got there
            reply["apt"] = [False, "E: Failed to fetch http://apt-cache.worms.internal:3142/... 503"]
        else:  # sandbox-a: b's SSH does not answer where the gameplan lets it in
            reply["connect"] = {key: "refused" for key in reply["connect"]}

    def failed_service(host, params, reply):
        if host == "10.4.0.11":
            reply["services"]["cron"] = "failed"
            reply["boot"].update(state="degraded", failed=["cron.service"])

    ex.probe_hooks.append(broken)
    ex.init_hooks.append(failed_service)
    result = handlers.check(ex, CFG, validation.validate("check", {"run_uuid": RUN}))
    assert set(result["failures"]) == {"vm:sandbox-b:isolated", "vm:sandbox-b:apt"}
    assert set(result["warnings"]) == {"vm:sandbox-a:firewall", "vm:sandbox-b:files", "vm:sandbox-b:init:services",
                                       "vm:sandbox-b:init:boot"}  # fmt: skip
    detail = {c["check"]: c["detail"] for c in result["checks"]}
    assert detail["vm:sandbox-b:isolated"] == "got to: database" and "vm:sandbox-b:time" not in detail
    assert detail["vm:sandbox-a:firewall"].startswith("1 of 1 connections not as the gameplan's firewall says")
    assert detail["vm:sandbox-b:init:boot"] == "degraded: 1 of the gameplan's services failed (the job log on the host names them)"
    # What is wrong, in the job log; the database gets numbers and the platform's names only.
    text = json.dumps(result)
    assert "/root/note" not in text and "cron" not in text
    assert any("file 0 (/root/note, root 0644): owner debian, mode 0600" in note for note in ex.notes)
    assert any("service cron: failed" in note for note in ex.notes) and "check sandbox-b: cron.service failed" in ex.notes
    assert any("sandbox-b port 22" in note and "open expected, got refused" in note for note in ex.notes)

    # A VM that did not boot in this attempt: its QEMU is older than the attempt's prepare (it ran on
    # from the last run). Nothing is asked of it. A VM and a gateway that did not come up as they should.
    ex = FakeExecutor()
    _prepare(ex, booted=tmp_path)
    _fake_qemu(tmp_path, 900, 4900, started=400)

    def not_up(host, params, reply):
        if host == "10.4.0.11":  # sandbox-b
            reply["cloud-init"].update(code=1, status="error", extended="error - done",
                                       errors=["('scripts_user', RuntimeError('Runparts: 1 failures (alice-setup) in 1 attempted commands'))"])
            reply["boot"].update(state="degraded", failed=["serial-getty@ttyS0.service", "ssh.service"])
            reply["addresses"]["net0"] = "eth0 has 10.64.0.9/24, not 10.64.0.232/24"
            reply["route"] = [False, "via 10.64.0.9 dev eth0"]
            reply["sshd"] = [False, "nothing listens"]
            reply["users"]["alice"] = False
        elif host == "10.4.0.2":  # sandbox-gw: degraded by a unit nothing rests on, which passes
            reply["boot"].update(state="degraded", failed=["serial-getty@ttyS0.service"], waited=12.4)
            reply["nftables"]["inet sandbox_gw forward"] = "missing"
            reply["sysctl"]["net.netfilter.nf_conntrack_max"] = "4096"
            reply["route"] = [False, "via 10.4.0.1 dev eth2"]

    ex.init_hooks.append(not_up)
    result = handlers.check(ex, CFG, validation.validate("check", {"run_uuid": RUN}))
    assert set(result["failures"]) == {"vm:sandbox-a:start", "vm:sandbox-a:probe"}
    assert set(result["warnings"]) == {
        "vm:sandbox-a:init", "vm:sandbox-b:init:cloud-init", "vm:sandbox-b:init:boot",
        "vm:sandbox-b:init:addresses", "vm:sandbox-b:init:route", "vm:sandbox-b:init:sshd", "vm:sandbox-b:init:users",
        "vm:sandbox-gw:init:route", "vm:sandbox-gw:init:nftables"}  # fmt: skip
    detail = {c["check"]: c["detail"] for c in result["checks"]}
    assert detail["vm:sandbox-a:start"] == ("running since 600 s before this attempt's reset (the rollback, or the snapshot): it did not boot in this attempt "
                                            "and may hold the last run's state")  # fmt: skip
    assert detail["vm:sandbox-a:init"] == "not checked: it did not start in this attempt (start)"
    assert "10.4.0.10" not in ex.inits and "10.4.0.10" not in ex.probes
    assert detail["vm:sandbox-b:init:cloud-init"] == "error: scripts_user failed (the job log on the host says what)"
    assert any(note.startswith("check sandbox-b: cloud-init: ('scripts_user', ") and "alice-setup" in note for note in ex.notes)
    assert detail["vm:sandbox-b:init:boot"] == "degraded: ssh.service failed; 1 other unit (the job log on the host names them)"
    assert detail["vm:sandbox-b:init:addresses"] == "net0 not with the address its stack gives (the job log on the host says what it has)"
    assert detail["vm:sandbox-gw:init:boot"] == ("degraded: 1 unit failed, none that its start rests on (the job log on the host names "
                                                 "them), after 12 s")  # fmt: skip
    assert detail["vm:sandbox-gw:init:route"] == "not as its stack has it: none (the job log on the host says what it has)"
    assert detail["vm:sandbox-gw:init:nftables"] == "inet sandbox_gw forward: missing"
    # The conntrack limit is capacity: said, not a failure (startup.SOFT_SYSCTLS); IP forwarding would fail.
    assert detail["vm:sandbox-gw:init:sysctl"] == ("net.netfilter.nf_conntrack_max is 4096, not 262144 (a warning: the gateway "
                                                   "still routes; under a busy worm its connections may run out)")  # fmt: skip
    text = json.dumps(result)
    assert "alice" not in text and "10.64.0.9" not in text and "10.64.0.232" not in text
    assert {"check sandbox-b: user alice: missing", "check sandbox-gw: serial-getty@ttyS0.service failed"} <= set(ex.notes)
    assert any(note.startswith("check sandbox-b: net0 (") and "eth0 has 10.64.0.9/24" in note for note in ex.notes)
    assert "check sandbox-gw: default route: via 10.4.0.1 dev eth2; expected none" in ex.notes

    # A run an older agent prepared, with no note of when prepare began: whether its VMs booted in this
    # attempt is not known, so each fails its start (that attempt runs again).
    ex = FakeExecutor()
    _prepare(ex, booted=tmp_path)
    saved = handlers._run_state_path({}, RUN)
    state = json.loads(saved.read_text())
    saved.write_text(json.dumps({key: value for key, value in state.items() if key != "began"}))
    result = handlers.check(ex, CFG, validation.validate("check", {"run_uuid": RUN}))
    assert {"vm:sandbox-a:start", "vm:sandbox-b:start", "vm:sandbox-gw:start"} <= set(result["failures"])
    assert {c["detail"] for c in result["checks"] if c["check"].endswith(":start")} == {
        "running, but its attempt has no note of its reset: whether it booted in this attempt is not known"}
    saved.write_text(json.dumps(state))
    # A VM whose check cannot be planned (nor probed then), a gateway whose cloud-init snippet cannot be
    # read, and a VM still starting after the wait, ssh's job and one of the gameplan's services' left.
    ex.inits, ex.probes, ex.snippets = {}, {}, {}
    plan_vm = startup.plan_vm
    monkeypatch.setattr(startup, "plan_vm", lambda vm, macs: plan_vm({} if vm["name"] == "sandbox-a" else vm, macs))
    ex.init_hooks.append(lambda host, params, reply: host == "10.4.0.11" and reply["boot"].update(
        state="starting", jobs=3, pending=["cron.service", "multi-user.target", "ssh.service"], waited=90.2, late=True))
    result = handlers.check(ex, CFG, validation.validate("check", {"run_uuid": RUN}))
    assert set(result["failures"]) == {"vm:sandbox-a:probe"}
    assert set(result["warnings"]) == {"vm:sandbox-a:init", "vm:sandbox-gw:init", "vm:sandbox-b:init:boot"}
    detail = {c["check"]: c["detail"] for c in result["checks"]}
    assert detail["vm:sandbox-a:init"] == detail["vm:sandbox-gw:init"] == "not checked: it could not be planned (the job log on the host says why)"
    assert detail["vm:sandbox-a:probe"] == "not run: its start and initialisation check could not be planned (init)"
    assert "10.4.0.10" not in ex.inits and "10.4.0.10" not in ex.probes and "10.4.0.2" not in ex.inits
    assert ("check sandbox-gw: its start and initialisation check: RuntimeError: pvesm path local:snippets/sandbox-gw.yml: no such "
            "volume") in ex.notes  # fmt: skip
    assert detail["vm:sandbox-b:init:boot"] == ("still starting after 90 s: ssh.service not started yet; 1 of the gameplan's services "
                                                "not started yet; 1 other job to go (the job log on the host names them)")  # fmt: skip
    assert "cron" not in json.dumps(result) and "check sandbox-b: cron.service still starting after 90 s" in ex.notes
    # A gateway whose Proxmox config names no snippet.
    monkeypatch.setattr(startup, "plan_vm", plan_vm)
    ex.snippets = FakeExecutor().snippets
    ex.qm_config["950"] = "cores: 1\nname: sandbox-gw\n"
    result = handlers.check(ex, CFG, validation.validate("check", {"run_uuid": RUN}))
    assert "vm:sandbox-gw:init" in result["warnings"] and "10.4.0.2" not in ex.inits
    assert ("check sandbox-gw: its start and initialisation check: RuntimeError: its Proxmox config names no cloud-init snippet "
            "(cicustom user=)") in ex.notes  # fmt: skip

    # A VM that does not answer at all (it is not probed then), one whose probe's answer is cut off by
    # its deadline, and a gateway Proxmox says runs but whose QEMU the host does not find.
    ex = FakeExecutor()
    _prepare(ex, booted=tmp_path)
    (_host(tmp_path)["QEMU_PIDS"] / "950.pid").unlink()
    ex.unreachable.add("10.4.0.10")
    ex.probe_hooks = [lambda host, params, reply: (reply.update(deadline=True), reply.pop("apt"))]
    result = handlers.check(ex, CFG, validation.validate("check", {"run_uuid": RUN}))
    detail = {c["check"]: c["detail"] for c in result["checks"]}
    assert {"vm:sandbox-a:probe", "vm:sandbox-b:probe", "vm:sandbox-b:apt", "vm:sandbox-gw:start"} <= set(result["failures"])
    assert "vm:sandbox-a:init" in result["warnings"]
    assert {name.split(":", 2)[2] for name in detail if name.startswith("vm:sandbox-a:")} == {"start", "no-kvm", "init", "probe"}
    assert detail["vm:sandbox-a:init"] == "no answer (the job log on the host says why)" and detail["vm:sandbox-b:apt"] == "no answer"
    assert detail["vm:sandbox-a:probe"] == "not run: the VM did not answer its start and initialisation check (init)"
    assert detail["vm:sandbox-gw:start"] == "Proxmox says running, but the host finds no QEMU process of it"
    assert any("connection refused" in note for note in ex.notes)
    # The host itself restarted after prepare began: every QEMU on it is younger than that.
    (_host(tmp_path)["PROC"] / "sys" / "kernel" / "random" / "boot_id").write_text("another boot\n")
    (_host(tmp_path)["PROC"] / "uptime").write_text("300.00 600.00\n")
    _fake_qemu(tmp_path, 950, 4950, started=20)
    detail = {c["check"]: c["detail"] for c in handlers.check(ex, CFG, validation.validate("check", {"run_uuid": RUN}))["checks"]}
    assert detail["vm:sandbox-gw:start"] == "running, booted in this attempt (the host itself restarted after the sandbox's reset)"
    # No run on the host: the job fails, it does not pass without its probes.
    with pytest.raises(validation.Invalid, match="no state for run"):
        handlers.check(ex, CFG, validation.validate("check", {"run_uuid": "4" * 8 + RUN[8:]}))


@pytest.mark.parametrize(
    "config",
    ["cpu: host", "cpu: max", "cpu: x86-64-v3,flags=+vmx", "cpu: x86-64-v3,flags=+nested-virt", "cpu: qemu64", "name: no-cpu-line", ""],
)
def test_check_refuses_a_sandbox_vm_without_the_cpu_that_hides_kvm(config):
    """Hostile root on a VM must not get /dev/kvm. Asked of the host, and anything unknown fails."""
    ex = FakeExecutor()
    ex.qm_config["901"] = config
    result = handlers.check(ex, CFG, {})
    assert result["ok"] is False
    assert result["failures"] == ["vm:sandbox-b:no-kvm"]


def test_place_flags_writes_flag_and_returns_hash():
    ex = FakeExecutor()
    result = handlers.place_flags(ex, CFG, validation.validate("place_flags", {"vms": ["sandbox-a"]}))
    assert result["failed"] == [] and len(result["released"]) == 1
    entry = result["released"][0]
    assert entry["vm"] == "sandbox-a" and validation.SHA256.match(entry["flag_hash"])
    # The secret went in over stdin to install into /etc/worms/flag, not argv.
    write = next(c for c in ex.ssh_calls if "install" in c["argv"])
    assert write["host"] == "10.4.0.10" and "/etc/worms/flag" in write["argv"]
    assert write["input"].startswith("FLAG{") and entry["flag_hash"] == hashlib.sha256(write["input"].encode()).hexdigest()


def test_a_flag_that_does_not_read_back_is_not_released():
    class Lying(FakeExecutor):
        def ssh(self, host, argv, input=None, timeout=120):
            out = super().ssh(host, argv, input, timeout)
            return "0" * 64 + "  /etc/worms/flag\n" if "sha256sum" in argv else out

    with pytest.raises(RuntimeError, match="reads back differently"):
        handlers.place_flags(Lying(), CFG, validation.validate("place_flags", {"vms": ["sandbox-a"]}))
    result = handlers.place_flags(Lying(), CFG, validation.validate("place_flags", {"vms": ["sandbox-a"], "strict": False}))
    assert result["released"] == [] and "reads back differently" in result["failed"][0]["error"]


CONDUCTOR = "orchestrator@pam!orchestrator-prov"  # the orchestrator's API token


def _upid(start, kind, vmid, user="root@pam"):
    return f"UPID:n:0001E240:0A1B2C3D:{int(start):08X}:{kind}:{vmid}:{user}:"


def test_after_the_worm_a_vm_it_broke_is_reported_not_failed(tmp_path, monkeypatch):
    ex = FakeExecutor()
    ex.unreachable.add("10.4.0.11")
    with pytest.raises(RuntimeError):
        handlers.remove_flags(ex, CFG, validation.validate("remove_flags", {"vms": ["sandbox-a", "sandbox-b"]}))
    result = handlers.remove_flags(ex, CFG, validation.validate("remove_flags", {"vms": ["sandbox-a", "sandbox-b"], "strict": False}))
    assert result["removed"] == ["sandbox-a"] and result["failed"][0]["vm"] == "sandbox-b"
    remove = next(c for c in ex.ssh_calls if c["host"] == "10.4.0.10")
    assert remove["argv"] == ["sudo", "sh", "-c", "rm -f /etc/worms/flag && test ! -e /etc/worms/flag"]

    # After the worm a VM is asked nothing: the host's own records say why one is not running.
    from datetime import datetime, timedelta, timezone

    names = ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j")
    ex = FakeExecutor({f"sandbox-{n}": {"maintenance_ip": f"10.4.0.{10 + i}", "vmid": 900 + i, "addresses": []} for i, n in enumerate(names)})
    ex.pools.update(devbox=list(DEVBOX_VMS))
    now = time.time()
    ex.states = {900: ("stopped", None), 901: ("stopped", None), 902: ("stopped", None), 903: ("running", "paused"),
                 904: ("stopped", None), 906: ("running", "internal-error"), 907: ("stopped", None), 908: ("stopped", None),
                 909: ("stopped", None), 950: ("stopped", None)}  # fmt: skip
    ex.journal = "\n".join(f"{at:.6f} worms-dev {line}" for at, line in sorted([
        # a: it powered itself off; nothing of the host's then (QEMU exits: its taps close first).
        (now - 1000, "systemd[1]: Started 900.scope."), (now - 100.2, "kernel: tap900i0: left allmulticast mode"),
        (now - 100, "systemd[1]: 900.scope: Deactivated successfully."),
        (now - 97, "qmeventd[4]: Starting cleanup for 900"),
        # b: the OOM killer.
        (now - 50.2, "kernel: oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=qemu.slice,mems_allowed=0,global_oom,"
                     "task_memcg=/qemu.slice/901.scope,task=kvm,pid=7901,uid=0"),
        (now - 50, "systemd[1]: 901.scope: Failed with result 'oom-kill'."),
        # c: one of its QEMU's threads crashed (by the threads the agent recorded).
        (now - 20, "kernel: CPU 0/KVM[7910]: segfault at 0 ip 00005555 sp 00007ffd error 4 in qemu-system-x86_64[5555,1000+2000]"),
        (now - 19, "systemd[1]: 902.scope: Deactivated successfully."),
        # d: paused by the conductor; that task is in the journal alone (the task list rotated).
        (now - 10, f"pvedaemon[9]: <{CONDUCTOR}> starting task {_upid(now - 10, 'qmpause', 903, CONDUCTOR)}"),
        # e: shut down by the conductor, started again, then off from inside: the earlier stop is not why.
        (now - 900, f"qm[5]: <root@pam> starting task {_upid(now - 900, 'qmshutdown', 904)}"),
        (now - 891, "systemd[1]: 904.scope: Deactivated successfully."),
        (now - 890, f"qm[5]: <root@pam> end task {_upid(now - 900, 'qmshutdown', 904)} OK"),
        (now - 800, "systemd[1]: Started 904.scope."), (now - 60, "systemd[1]: 904.scope: Deactivated successfully."),
        # the gateway: stopped by a task of the host (in the task list).
        (now - 29, "systemd[1]: 950.scope: Deactivated successfully."),
        # Not a QEMU, not a VM of the run.
        (now - 21, "kernel: soffice.bin[1489511]: segfault at 34 ip 00007feb21b01e55 sp 00007ffe661c29c0 error 4"),
        # h: this run began with the conductor's rollback (and its start, in the same second); the
        # journal no longer has systemd's line of the start. Off from inside much later: the
        # rollback had ended long before, it is not why.
        (now - 700, f"pvedaemon[9]: <{CONDUCTOR}> starting task {_upid(now - 700, 'qmrollback', 907, CONDUCTOR)}"),
        (now - 699.5, f"pvedaemon[9]: <{CONDUCTOR}> starting task {_upid(now - 700, 'qmstart', 907, CONDUCTOR)}"),
        (now - 698, "pvedaemon[9]: VM 907 started with PID 6907 using pve-qemu-kvm_10.1.2-4."),
        (now - 697, f"pvedaemon[9]: <{CONDUCTOR}> end task {_upid(now - 700, 'qmstart', 907, CONDUCTOR)} OK"),
        (now - 696, f"pvedaemon[9]: <{CONDUCTOR}> end task {_upid(now - 700, 'qmrollback', 907, CONDUCTOR)} OK"),
        (now - 40, "systemd[1]: 907.scope: Deactivated successfully."),
        # i: its QEMU dumped core (an abort, which the kernel does not log); its pid from Proxmox's line.
        (now - 500, "systemd[1]: Started 908.scope."), (now - 500, "qm[5]: VM 908 started with PID 6908 using pve-qemu-kvm_10.1.2-4."),
        (now - 15, "systemd-coredump[77]: Process 6908 (kvm) of user 0 dumped core."),
        (now - 14, "systemd[1]: 908.scope: Deactivated successfully."),
        # j: its QEMU killed by a signal (no core dump, no kernel crash line): the kernel took its taps down.
        (now - 300, "systemd[1]: Started 909.scope."), (now - 8.1, "kernel: tap909i0 (unregistering): left allmulticast mode"),
        (now - 8, "systemd[1]: 909.scope: Deactivated successfully."),
    ]))  # fmt: skip
    (_host(tmp_path)["TASKS"] / "index").write_text(f"{_upid(now - 801, 'qmstart', 904)} {int(now - 799):08X} OK\n"
                                                    f"{_upid(now - 30, 'qmstop', 950)} {int(now - 28):08X} OK\n")  # fmt: skip
    state = Path(handlers.STATE_DIR)
    state.mkdir(parents=True)
    (state / host_state.RECORD).write_text(json.dumps({"902": {"pid": 7902, "tids": [7902, 7910], "at": now - 100}}))
    _fake_memory(tmp_path, available_mb=9000)
    devbox_qemu, running_qemu = _fake_qemu(tmp_path, 116, 6116), _fake_qemu(tmp_path, 905, 6905)
    halted_qemu = _fake_qemu(tmp_path, 906, 6906)  # g: its guest made KVM fail; QEMU holds it, and its memory
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    vmids = [900, 901, 902, 903, 904, 905, 906, 907, 908, 909, 950]
    seen = handlers.observe(ex, CFG, validation.validate("observe", {"vmids": vmids, "since": since}))
    causes = {vm["name"]: (vm["status"], vm["cause"]) for vm in seen["vms"]}
    assert causes == {
        "sandbox-a": ("stopped", "guest"), "sandbox-b": ("stopped", "oom"), "sandbox-c": ("stopped", "crash"),
        "sandbox-d": ("paused", "host"), "sandbox-e": ("stopped", "guest"), "sandbox-f": ("running", None),
        # A guest halted for good is stopped, by the guest: the worm's doing, as a power-off is.
        "sandbox-g": ("stopped", "guest"), "sandbox-h": ("stopped", "guest"), "sandbox-i": ("stopped", "crash"),
        "sandbox-j": ("stopped", "crash"),
        "sandbox-gw": ("stopped", "host"),
    }  # fmt: skip
    detail = {vm["name"]: vm["detail"] for vm in seen["vms"]}
    assert detail["sandbox-d"].startswith(f"qmpause by {CONDUCTOR}") and detail["sandbox-gw"].startswith("qmstop by root@pam")
    assert detail["sandbox-g"].startswith("KVM could not run its code (QEMU internal-error)")
    assert detail["sandbox-g"].endswith("Its QEMU holds it until it is stopped")
    assert detail["sandbox-i"].startswith("its QEMU dumped core") and detail["sandbox-h"].startswith("powered off from inside")
    assert detail["sandbox-j"].startswith("its QEMU ended without a clean exit")
    assert seen["memory"] == {"available_mb": 9000, "total_mb": 32000, "psi_some_avg10": 1.5, "psi_full_avg10": 0.25, "ksm_sharing_mb": 100,
                              "arc_mb": 2048, "arc_reclaimable_mb": 1024}  # fmt: skip
    assert seen["devboxes_running"] == ["devbox-t1-a"] and seen["oom_adjusted"] == [116, 905, 906]
    assert devbox_qemu.read_text().strip() == running_qemu.read_text().strip() == halted_qemu.read_text().strip() == "1000"
    # The journal read for the VMs not running: systemd's lines of their scopes alone (PID 1 writes some
    # 26000 a day), the kernel's, qmeventd's, Proxmox's task loggers' and systemd-coredump's.
    read = next(c for c in ex.calls if c[:2] == ("run", "journalctl"))
    assert {f"UNIT={vmid}.scope" for vmid in (900, 901, 902, 903, 904, 906, 907, 908, 950)} < set(read)
    assert "UNIT=905.scope" not in read
    assert "SYSLOG_IDENTIFIER=systemd-coredump" in read
    # The worm's own power-off is all a guest can cause: no record of the host's is the guest's to write.
    ex.journal = ex.journal.replace("Failed with result 'oom-kill'", "Deactivated successfully")
    assert {vm["name"]: vm["cause"] for vm in handlers.observe(ex, CFG, validation.validate("observe", {"vmids": [901]}))["vms"]} == {
        "sandbox-b": "oom"}  # the kernel's line alone says it  # fmt: skip
    # Records it cannot read: unknown (the conductor fails the attempt, our side), never guest.

    class Blind(type(ex)):
        def run(self, argv, check=False, timeout=60):
            if argv[0] == "journalctl":
                raise RuntimeError("journalctl exited 1")
            return super().run(argv, check, timeout)

    blind = Blind(ex._vms)
    blind.states = ex.states
    assert [vm["cause"] for vm in handlers.observe(blind, CFG, validation.validate("observe", {"vmids": [900]}))["vms"]] == ["unknown"]

    # Proxmox can report a QEMU gone before systemd's line of it can be read (qmeventd's trails by
    # seconds): no record of a stop yet is read once more, a moment later, before it is unknown.
    class Late(type(ex)):
        def run(self, argv, check=False, timeout=60):
            if argv[0] == "journalctl":
                self.reads.append(argv)
                if len(self.reads) == 1 or self.never:  # without 900's end
                    ends = ("900.scope: Deactivated successfully.", "Starting cleanup for 900")
                    return "\n".join(line for line in self.journal.splitlines() if not line.endswith(ends))
            return super().run(argv, check, timeout)

    monkeypatch.setattr(handlers, "OBSERVE_REREAD", 0)
    for never, cause in ((False, "guest"), (True, "unknown")):
        late = Late(ex._vms)
        late.states, late.journal, late.reads, late.never = ex.states, ex.journal, [], never
        seen = handlers.observe(late, CFG, validation.validate("observe", {"vmids": [900, 904, 905]}))
        assert [vm["cause"] for vm in seen["vms"]] == [cause, "guest", None] and len(late.reads) == 2
        assert "UNIT=900.scope" in late.reads[1] and "UNIT=904.scope" not in late.reads[1]  # the VM without a record alone


def test_a_declared_file_is_written_later_from_the_hosts_copy():
    ex = FakeExecutor()
    _prepare(ex)
    public = next(c for c in ex.ssh_calls if c["argv"][:3] == ["sudo", "tee", "-a"])["input"].strip()
    ex.ssh_calls.clear()
    result = handlers.write_file(ex, CFG, validation.validate("write_file", {"run_uuid": RUN, "file": 1}))
    assert result == {"written": True, "vm": "sandbox-a", "error": None}
    write = ex.ssh_calls[-1]
    assert write["argv"][-1] == "/root/later" and write["input"] == public
    with pytest.raises(validation.Invalid, match="no state for run"):
        handlers.write_file(ex, CFG, validation.validate("write_file", {"run_uuid": "4" * 8 + RUN[8:], "file": 1}))


def test_release_unknown_vm_fails():
    ex = FakeExecutor()
    with pytest.raises(validation.Invalid):
        handlers.place_flags(ex, CFG, {"vms": ["not-in-sandbox"], "strict": True})
    # observe looks at the sandbox pool's VMs alone: not the database's, not a devbox's.
    ex.pools.update(ephemeral=[SERVICE_VMS[0]], devbox=list(DEVBOX_VMS))
    for vmid in (111, 116, 4242):
        with pytest.raises(validation.Invalid, match="not VMs of the sandbox pool"):
            handlers.observe(ex, CFG, validation.validate("observe", {"vmids": [900, vmid]}))
    assert not any(c[0] == "run" and c[1] == "journalctl" for c in ex.calls)


def test_deploy_fetches_verifies_copies_and_arms():
    ex = FakeExecutor()
    key = "malware-artifacts/aa/bb/" + "c" * 64 + ".zip"
    params = validation.validate(
        "deploy",
        {
            "start_vm": "sandbox-a",
            "team_uuid": "11111111-2222-3333-4444-555555555555",
            "artifact": {"storage_key": key, "content_hash": hashlib.sha256(ex.artifact_bytes).hexdigest()},
            "worm_delay": 120,
        },
    )
    result = handlers.deploy(ex, CFG, params)
    assert result["start_vm"] == "sandbox-a" and "released" not in result
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
    # An earlier upload's worm stopped on this VM (no reset before a dev upload since 2026-11-24),
    # then the artifact, then only this VM.
    assert hosts[:2] == ["10.6.0.11", "10.5.0.18"] and set(hosts[2:]) == {"10.6.0.11"}
    assert ex.ssh_calls[0]["argv"] == ["sudo", "/bin/sh", "-c", handlers.STOP_WORM] and "worms-start.timer" in handlers.STOP_WORM
    arm = next(c for c in ex.ssh_calls if "systemd-run" in c["argv"])
    assert arm["argv"] == [
        "sudo", "systemd-run", "--on-active=10", "--unit=worms-start", "--property=RuntimeMaxSec=3600",
        "--working-directory", "/root/worm", "--uid", "root", "/bin/bash", "main.sh",
    ]  # fmt: skip
    # Scoring and logging know a devbox by its gateway's address: a round's team file stays out of it.
    assert not [c for c in ex.ssh_calls if "/etc/sandbox-api/current-team" in c["argv"]]
    assert ex.calls == [("run", "qm", "config", "100"), ("run", "qm", "status", "100")], \
        "it only reads this VM's config and state, and does not touch OpenTofu"
    # Armed, checked as a round's deploy checks it.
    assert ex.ssh_calls[-1]["argv"] == ["sh", "-c", "systemctl is-active worms-start.timer worms-start.service || true"]
    # VM A powered off: said, before anything is copied (not a bare SSH error).
    ex = DevboxExecutor()
    ex.qm["100"] = "status: stopped"
    with pytest.raises(RuntimeError, match="devbox-t1-a is stopped, not running"):
        handlers.devbox_deploy(ex, CFG, _devbox_deploy(ex))
    assert ex.ssh_calls == []


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
         "artifact": {"storage_key": key, "content_hash": "0" * 64}, "worm_delay": 60},
    )
    with pytest.raises(validation.Invalid, match="hash mismatch"):
        handlers.deploy(ex, CFG, params)


def test_teardown():
    ex = FakeExecutor()
    _prepare(ex)
    state = Path(handlers.STATE_DIR) / "runs" / f"{RUN}.json"
    assert state.exists()
    other, reset = Path(handlers.STATE_DIR) / "runs" / "an-earlier-run.json", Path(handlers.STATE_DIR) / handlers.SANDBOX_RESET
    other.write_text("{}")
    reset.write_text("{}")
    assert handlers.teardown(ex, CFG, validation.validate("teardown", {"run_uuid": RUN}))["destroyed"] is True
    assert not state.exists()  # the run's secrets are gone
    # Nothing of the sandbox left (T12): no run's secrets, no reset record, and its definition empty,
    # so that a `tofu apply` there builds nothing (not the last round's sandbox).
    assert not other.exists() and not reset.exists()
    assert "No sandbox: written by teardown" in ex.installed_hcl and 'sandbox_vms     = {}' in ex.installed_hcl
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
    assert validation.validate("teardown", {"keep": keep}) == {"keep": False, "run_uuid": None}


def test_devbox_gateway_permitopen_is_only_the_entry_vm():
    """The SSH gateway forwards each team to its entry VM (A) only; B/C are
    reached by pivoting inside the devbox network (docs/07-security.md).
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
# A well-formed ed25519 key: its blob names its type, as `worms_cli.keys.canonical` checks.
T1_KEY = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"\x01" * 32).decode() + " t1@laptop"
DEVBOX_TEAMS = [{"slug": "t1", "index": 0, "team_uuid": T1, "authorized_keys": [T1_KEY]}]
T1_VMIDS = {116, 117, 118}


class DevboxFake(FakeExecutor):
    """One team's devbox: three VMs, their gateway and the SSH gateway, once applied."""

    def __init__(self, tmp_path, built_before=False):
        super().__init__()
        self.repo_root = tmp_path
        (tmp_path / handlers.CA_CERT).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / handlers.CA_CERT).write_text("-----BEGIN CERTIFICATE-----\nworms CA\n-----END CERTIFICATE-----\n")
        (tmp_path / handlers.CAPTURE_VM).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / handlers.CAPTURE_VM).write_text((REPO / handlers.CAPTURE_VM).read_text())
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


def _key_writes(ex):
    """The hosts the team's key file was written on, directly (worms_cli/keys.py), and what."""
    return sorted((c["host"], c["input"]) for c in ex.ssh_calls if c["argv"][:3] == ["sudo", "sh", "-c"] and "authorized_keys_worms_team" in c["argv"][3])


def test_a_devbox_reset_rolls_back_to_the_snapshot(tmp_path):
    """A first-boot snapshot (before 2026-11-19): the team's next reset sets its three VMs up once,
    without the network (no apt refresh, no package, no team key) and only them, and snapshots
    that. Every reset after it: the rollback, then the internal CA, the team's keys and new tokens
    over SSH, seconds: no Ansible, no package, no new snapshot."""
    import yaml

    ex = DevboxFake(tmp_path, built_before=True)
    ex.clean = set(T1_VMIDS)  # first-boot snapshots
    result = _rebuild(ex)
    steps = ex.steps()
    assert steps.count("devbox-configure") == 1 and "--limit devbox-t1-a,devbox-t1-b,devbox-t1-c" in ex.worms_args
    varfile = yaml.safe_load(open(ex.worms_args.split()[1].lstrip("@")).read().replace("!unsafe ", ""))
    assert varfile["apt_client_refresh_offline"] is False and varfile["devbox_packages"] == []
    assert varfile["devbox_team_keys"] == {"t1": []}  # the snapshot holds no team key
    configure = steps.index("devbox-configure")
    for vmid in T1_VMIDS:
        assert steps.index(f"qm rollback {vmid} clean --start 1") < configure < steps.index(f"qm snapshot {vmid} clean --description {handlers.DEVBOX_CONFIGURED}")
    assert result["devbox"].startswith("rolled back, set up once")

    ex.calls.clear()
    ex.ssh_calls.clear()
    result = _rebuild(ex)
    steps = ex.steps()
    assert not any(c[:2] == ("tofu", "devbox") and "apply" in c for c in ex.calls)
    assert [s for s in steps if "rollback" in s] == [f"qm rollback {vmid} clean --start 1" for vmid in sorted(T1_VMIDS)]
    assert not any(c[0] == "worms" for c in ex.calls) and not any("snapshot" in s for s in steps)
    hosts = sorted(f"10.6.0.{vmid}" for vmid in T1_VMIDS)
    ca = sorted(c["host"] for c in ex.ssh_calls if c["argv"] == ["sudo", "sh", "-c", handlers.CA_INSTALL] and "worms CA" in c["input"])
    assert ca == hosts
    key = " ".join(DEVBOX_TEAMS[0]["authorized_keys"][0].split()[:2]) + "\n"
    assert _key_writes(ex) == [(host, key) for host in hosts]
    assert sorted(c["host"] for c in ex.ssh_calls if handlers.FLAG_PATH in c["argv"]) == hosts
    assert result["devbox"] == "rolled back to its snapshot" and len(result["vms"]) == 3


def test_a_devbox_without_a_snapshot_is_replaced_and_snapshotted(tmp_path):
    ex = DevboxFake(tmp_path, built_before=True)
    ex.clean = {116, 117}
    result = _rebuild(ex)
    replace = next(c for c in ex.calls if c[:2] == ("tofu", "devbox") and "apply" in c)
    assert sum(arg.startswith("-replace=") for arg in replace) == 3
    assert ex.clean == T1_VMIDS and result["devbox"] == "replaced, configured and snapshotted"
    assert ex.descriptions == {vmid: handlers.DEVBOX_CONFIGURED for vmid in T1_VMIDS}
    steps = ex.steps()
    assert steps.index("devbox-configure") < min(steps.index(f"qm snapshot {vmid} clean --description {handlers.DEVBOX_CONFIGURED}") for vmid in T1_VMIDS)
    assert not any("rollback" in step for step in steps)
    assert len(_key_writes(ex)) == 3


def test_no_devbox_is_rolled_back_while_internet_access_is_allowed(tmp_path):
    (tmp_path / "internet-allowed").write_text("allowed\n")
    ex = DevboxFake(tmp_path, built_before=True)
    ex.clean = set(T1_VMIDS)
    with pytest.raises(RuntimeError, match="internet access is allowed"):
        _rebuild(ex)
    assert not any("rollback" in step for step in ex.steps())


from round_agent import devbox_map  # noqa: E402


def test_a_reconfigure_writes_the_map_before_the_minutes_of_configuration(tmp_path):
    """After a reset of scoring and logging, the map is what is missing. Then only the keys
    (devbox-keys: no package, no internet), not the devbox's whole configuration."""
    ex = DevboxFake(tmp_path, built_before=True)
    order = []
    ex.ssh = lambda host, argv, input=None, timeout=120: order.append(("map", host)) if devbox_map.TARGET in argv else None
    ex.worms = lambda component, timeout=3600, args=None: order.append(("configure", component))
    handlers.devbox_reconfigure(ex, CFG, validation.validate("devbox_reconfigure", {"team_uuid": T1, "teams": DEVBOX_TEAMS}))
    assert order == [("map", "10.5.0.14"), ("map", "10.5.0.15"), ("configure", "devbox-keys")]


def test_the_agents_copy_of_the_map_is_root_only(tmp_path):
    ex = DevboxFake(tmp_path, built_before=True)
    handlers.devbox_reconfigure(ex, CFG, validation.validate("devbox_reconfigure", {"team_uuid": T1, "teams": DEVBOX_TEAMS}))
    copy = tmp_path / "agent-state" / "devbox-teams"
    assert copy.read_text() == f"10.2.0.33 {T1}\n"
    assert oct(copy.stat().st_mode & 0o777) == "0o600" and oct(copy.parent.stat().st_mode & 0o777) == "0o700"


def test_devbox_key_varfile_tags_keys_unsafe():
    """The keys are written with the YAML `!unsafe` tag so Ansible never
    templates attacker-controlled key text (template injection defence in
    depth; docs/07-security.md)."""
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
    job_id = _queue(conn, "observe", {"vmids": [950, 900]})
    assert agent.tick() is True
    with conn.cursor() as cur:
        cur.execute("SELECT state, result FROM host_jobs WHERE id = %s", (job_id,))
        state, result = cur.fetchone()
    assert state == "done" and [(vm["vmid"], vm["status"]) for vm in result["vms"]] == [(900, "running"), (950, "running")]
    assert set(result) == {"vms", "memory", "devboxes_running", "oom_adjusted"} and agent.cfg["memory_reserve_mb"] == 1024
    # Observe jobs have a worker of their own (run_forever starts it), with its own connection and
    # executor: each keeps its job's log, and a job's VM history names that job.
    observer = agent.observer()
    assert observer.observe_only and not agent.observe_only and observer._conn is None
    assert observer.executor is not agent.executor and observer.executor.latest is False  # latest.log stays the loop's
    agent.serve(stop=lambda: "the test's stop")  # asked between jobs, before the first: it ends at once


def test_loop_fails_an_invalid_job_without_crashing(agent_db):
    agent, ex, conn = agent_db
    job_id = _queue(conn, "deploy", {"start_vm": "../bad"})
    agent.tick()
    with conn.cursor() as cur:
        cur.execute("SELECT state, error FROM host_jobs WHERE id = %s", (job_id,))
        state, error = cur.fetchone()
    assert state == "failed" and "invalid" in error
    job_id = _queue(conn, "observe", {"vmids": [111]})  # not a VM of the sandbox pool
    agent.tick()
    with conn.cursor() as cur:
        cur.execute("SELECT state, error FROM host_jobs WHERE id = %s", (job_id,))
        state, error = cur.fetchone()
    assert state == "failed" and error.startswith("invalid: not VMs of the sandbox pool")


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
    # An observe first of all: seconds, and the conductor waits for it with a short timeout.
    _queue(conn, "check", {})
    _queue(conn, "devbox_rebuild", {})
    observe = _queue(conn, "observe", {"vmids": [900]})
    assert [agent.claim()[0], agent.claim()[2], agent.claim()[2]] == [observe, "check", "devbox_rebuild"]
    # The observer takes observe jobs alone, while the loop runs a build or a team's reset; the
    # operator's pause holds it too.
    observer = Agent(env=agent.env, executor=FakeExecutor(), connection=conn, observe_only=True)
    build = _queue(conn, "check", {})
    observe = _queue(conn, "observe", {"vmids": [900]})
    handlers.paused_path(agent.cfg).parent.mkdir(parents=True, exist_ok=True)
    handlers.paused_path(agent.cfg).write_text("paused by the test")
    assert observer.claim() is None
    handlers.paused_path(agent.cfg).unlink()
    assert observer.claim()[0] == observe and observer.claim() is None and agent.claim()[0] == build


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
    assert "PYTHONPATH=/opt/worms/scripts:/opt/worms/src\n" in env  # the gameplan framework is in src
    assert "SCORING_HOST=10.5.0.14" in env and "LOGGING_HOST=10.5.0.15" in env
    assert "DASHBOARD_HOST=10.5.0.18" in env
    # OpenTofu: the provisioning token from the vault.
    assert "TF_VAR_proxmox_api_url=https://127.0.0.1:8006/" in env
    assert "TF_VAR_proxmox_api_token=root@pam!worms=0000-1111" in env
    assert "TF_VAR_ssh_public_key=/etc/worms/ssh/worms.pub" in env  # the WORMS key, from the vault
    assert "REPO_ROOT=/opt/worms" in env and "DB_SSLROOTCERT=/opt/worms/ansible/pki/worms/ca.crt" in env


def test_the_agent_is_the_host_whatever_roots_worms_config_says(render, tmp_path, monkeypatch):
    """A /root/.worms/config with WORMS_SSH on the host (laptop mode) made the
    agent's `worms step devbox-apply` fail with "no ansible/inventory.generated.yml"
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
    assert config.Config.load().ssh == ""  # with the agent's environment: the host


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
    service command instead: worms-host-run (tests/test_laptop.py runs it)."""
    import shutil
    import subprocess

    unit = render("ansible/roles/host/round_agent", "worms-round-agent.service.j2")
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert exec_start == 'ExecStart=/usr/local/sbin/worms-host-run . "exec /usr/bin/python3 -m round_agent"'
    wrapper = render("ansible/roles/host/round_agent", "worms-host-run.j2")
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
    # The VM history's name for what did it goes with each command, the agent's own environment
    # stays as it is: the agent's other worker runs an observe job at the same time (agent.py).
    assert ex.run(["sh", "-c", 'echo "$WORMS_HISTORY_BY"']) == "round agent: devbox_provision\n"
    assert ex.run(["sh", "-c", 'echo "$WORMS_HISTORY_BY $PYTHONUNBUFFERED"'], stream=True) == "round agent: devbox_provision 1\n"
    assert "WORMS_HISTORY_BY" not in os.environ
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
    published = [tmp_path / "platform.json", tmp_path / "firewall.json"]  # what a host stack's apply needs (T2)
    for path in published:
        path.write_text("{}")
    monkeypatch.setattr(type(ex), "OUTPUTS", tuple(map(str, published)))
    assert ex.tofu_output("devbox") == {"token": "sensitive-output"}
    ex.tofu("devbox", "apply", "-auto-approve")

    text = _log_text(job_log)
    assert "$ cat" in text and "FLAG{from-stdin}" not in text
    assert "output -json" in text and "sensitive-output" not in text
    assert "apply-progress" in text


def test_a_failed_build_keeps_its_real_reason_in_the_log(logged):
    """Ansible reports a failed task on stdout, not stderr: the job's `error` (what the dashboard
    shows) is that cause, found in the whole output (round_agent/failure.py), and says where the
    rest is."""
    from round_agent.executor import CommandError

    ex, job_log = logged
    with pytest.raises(CommandError) as failure:
        ex.run(["sh", "-c", "echo 'fatal: [vm]: FAILED! => the real reason'; echo warning >&2; exit 3"], stream=True)
    assert "exited 3: Ansible: fatal: [vm]: FAILED! => the real reason" in str(failure.value)
    assert str(job_log.path) in str(failure.value) and "warning" not in str(failure.value).split(" exited 3: ")[1]
    assert "the real reason" in failure.value.detail
    text = _log_text(job_log)
    assert "the real reason" in text and "exit 3 after" in text


def test_a_failed_commands_error_is_its_cause_not_the_warnings():
    """Ansible fills stderr with warnings (inventory plugins, deprecations); its reason may come
    anywhere (2026-11-07 and 2026-11-23: the job's error showed only warnings). The callback's
    failed task (its VM, file:line and msg) comes first, the CLI's `error:` line after it."""
    from round_agent.executor import CommandError

    stderr = "[WARNING]: Failed to parse inventory with 'auto' plugin\n" * 40 + "[ERROR]: the real reason\n"
    message = str(CommandError(["python3", "src/manage.py", "step", "sandbox-configure"], 1, stderr))
    assert message == "worms step sandbox-configure exited 1: Ansible: the real reason"
    stdout = ("✗ sandbox-a · role : Create users · roles/x/tasks/main.yml:37\n    msg: One or more items failed\n"
              "    item bob:\n      msg: 'append is True but groups is missing'\n    its whole log: /r/round/sandbox-a.log\n"
              "\n✗ sandbox-b · role : Create users · roles/x/tasks/main.yml:37\n    msg: One or more items failed\n"
              "    item bob:\n      msg: 'append is True but groups is missing'\n")  # fmt: skip
    stderr = "[DEPRECATION WARNING]: INJECT_FACTS_AS_VARS\nOrigin: x.yml:5:9\n\nerror: step stopped: command failed (exit 2)\n"
    error = CommandError(["python3", "src/manage.py", "step", "sandbox-configure"], 1, stderr, "/r/004-prepare.log", stdout)
    assert str(error) == ("worms step sandbox-configure exited 1: Ansible: role : Create users failed on sandbox-a and 1 "
                          "more VM(s) (roles/x/tasks/main.yml:37): append is True but groups is missing "
                          "(full output: /r/004-prepare.log)")  # fmt: skip
    assert "the same failure on: sandbox-b" in error.detail and "error: step stopped" in error.detail
    assert error.cause.logs == ["/r/round/sandbox-a.log"] and "DEPRECATION" not in error.detail


# --- children in a session of their own (child.py) ---------------------------------------------


def _interrupt_when_ready(marks):
    """The terminal's Ctrl-C, once the child runs: it reaches this process alone (the child has a
    session of its own), as SIGINT to the process."""
    import os
    import signal
    import threading
    import time

    def interrupt():
        for _ in range(200):
            if marks.exists() and "READY" in marks.read_text():
                break
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt, daemon=True).start()


def test_an_interrupt_stops_the_child_with_one_sigint_and_waits_for_it(tmp_path):
    """OpenTofu and Ansible finish what they do on one SIGINT: the CLI sends it once and waits."""
    import os
    import sys

    from round_agent import child

    marks = tmp_path / "marks"
    script = (
        "import os, signal, sys, time\n"
        f"marks = open({str(marks)!r}, 'a', buffering=1)\n"
        "def on_int(signum, frame):\n"
        "    marks.write('INT\\n'); time.sleep(0.5); marks.write('EXIT\\n'); sys.exit(0)\n"
        "signal.signal(signal.SIGINT, on_int)\n"
        "marks.write(f'READY {os.getsid(0)}\\n')\n"
        "time.sleep(30)\n"
    )
    _interrupt_when_ready(marks)
    with pytest.raises(KeyboardInterrupt):
        child.run([sys.executable, "-c", script], grace=10, say=lambda text: None)
    lines = marks.read_text().splitlines()
    assert lines.count("INT") == 1 and lines[-1] == "EXIT"  # one SIGINT; it finished before the parent went on
    assert int(lines[0].split()[1]) != os.getsid(0)  # its own session: the terminal's Ctrl-C is not its


def test_a_child_that_ignores_sigint_gets_sigterm_after_the_grace(tmp_path):
    import sys

    from round_agent import child

    marks = tmp_path / "marks"
    script = (
        "import signal, sys, time\n"
        f"marks = open({str(marks)!r}, 'a', buffering=1)\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "def on_term(signum, frame):\n"
        "    marks.write('TERM\\n'); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, on_term)\n"
        "marks.write('READY\\n')\n"
        "time.sleep(30)\n"
    )
    said = []
    _interrupt_when_ready(marks)
    with pytest.raises(KeyboardInterrupt):
        child.run([sys.executable, "-c", script], grace=0.5, say=said.append)
    assert marks.read_text().splitlines()[-1] == "TERM"
    assert said and said[0].startswith("stopping ") and "Ctrl-C again" in said[0]


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
    # The observer's jobs (one a minute in a round) have their logs, and leave latest.log to the
    # build someone follows.
    with Executor(tmp_path, log_dir=jobs, latest=False).job_log("6" * 8 + JOB_UUID[8:], "observe"):
        pass
    assert (jobs / ("6" * 8 + JOB_UUID[8:] + ".log")).exists() and os.readlink(jobs / "latest.log") == f"{JOB_UUID}.log"


def test_a_job_logs_its_commands_and_how_it_ended(agent_db, tmp_path, monkeypatch):
    from round_agent.executor import CommandError, Executor

    agent, _fake, conn = agent_db
    agent.executor = Executor(tmp_path, log_dir=tmp_path / "jobs")

    def build(ex, cfg, params):
        ex.run(["sh", "-c", "echo building"], stream=True)
        return {"built": True}

    def broken(ex, cfg, params):
        ex.run(["sh", "-c", "echo 'step one done'; echo 'fatal: the reason'; echo warning >&2; exit 1"], stream=True)

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
    # The job's error points to the log and names the failure (CommandError); the rest of its
    # stdout stays out of the database.
    assert state == "failed" and f"{uuid}.log" in error and "exited 1: fatal: the reason" in error
    assert "step one done" not in error.split(" exited 1: ")[1]  # (the command line before it has it)
    assert isinstance(CommandError(["x"], 1, ""), RuntimeError)


def test_ssh_sends_one_line_the_remote_shell_splits_back_into_argv(monkeypatch):
    import shlex

    from round_agent.executor import SSH_OPTS, Executor

    ex = Executor("/nonexistent")
    sent = {}
    monkeypatch.setattr(ex, "run", lambda argv, input=None, timeout=None: sent.setdefault("argv", argv))
    argv = ["sudo", "/bin/sh", "-c", "base64 -d > /root/user-worm.zip"]
    ex.ssh("10.4.0.10", argv)
    assert sent["argv"][: len(SSH_OPTS) + 1] == ["ssh", *SSH_OPTS]
    assert sent["argv"][-2:] == ["--", "sudo /bin/sh -c 'base64 -d > /root/user-worm.zip'"]
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
        "worm_delay": delay,
    })  # fmt: skip


def _replay_on_the_vm(ex, home, start_ip="10.4.0.10"):
    """Run the deploy's copy and unpack commands as the start VM would, with
    root's home directory at `home`: as root there (sudo), as this test's user here. Returns the
    unpack result."""
    from round_agent.executor import Executor

    calls = [c for c in ex.ssh_calls if c["host"] == start_ip and c["argv"][:2] == ["sudo", "/bin/sh"]]
    copy, unpack = calls
    result = _remote_shell(Executor.remote_command(copy["argv"][1:]), input=copy["input"])
    assert result.returncode == 0, result.stderr
    return _remote_shell(Executor.remote_command(unpack["argv"][1:]))


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
    # The fuse runs `bash main.sh` in that directory as root (the worm always starts as root),
    # after the delay -- no shell, nothing run at deploy time.
    arm = next(c for c in ex.ssh_calls if "systemd-run" in c["argv"])
    assert arm["argv"][:2] == ["sudo", "systemd-run"] and "--on-active=120" in arm["argv"]
    assert arm["argv"][arm["argv"].index("--working-directory") + 1] == f"{tmp_path}/worm"
    assert arm["argv"][arm["argv"].index("--uid") + 1] == "root"
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
    handlers.place_flags(ex, CFG, validation.validate("place_flags", {"vms": ["sandbox-a"]}))
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
