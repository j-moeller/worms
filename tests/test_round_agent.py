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

from round_agent import handlers, host_state, probe, validation  # noqa: E402
from round_agent.agent import Agent  # noqa: E402


@pytest.fixture(autouse=True)
def _agent_state(monkeypatch, tmp_path):
    """A test's own agent records, internet flag, /proc, /sys, QEMU pid files and task list,
    never this machine's: the tests may run on the host itself, whose VMs have the vmids the
    fakes use (a test once set a real devbox QEMU's OOM priority)."""
    monkeypatch.setattr(handlers, "STATE_DIR", str(tmp_path / "agent-state"))
    monkeypatch.setattr(handlers, "INTERNET_FLAG", str(tmp_path / "internet-allowed"))
    for name in ("PROC", "SYS", "QEMU_PIDS", "TASKS"):
        (tmp_path / "host" / name).mkdir(parents=True)
        monkeypatch.setattr(host_state, name, str(tmp_path / "host" / name))


def _host(tmp_path):
    return {name: tmp_path / "host" / name for name in ("PROC", "SYS", "QEMU_PIDS", "TASKS")}


def _fake_qemu(tmp_path, vmid, pid, threads=()):
    """A running VM's QEMU in the test's /proc: its pid file, command line, OOM priority, threads."""
    host = _host(tmp_path)
    (host["QEMU_PIDS"] / f"{vmid}.pid").write_text(f"{pid}\n")
    proc = host["PROC"] / str(pid)
    (proc / "task").mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"\0".join([b"/usr/bin/kvm", b"-id", str(vmid).encode(), b"-name", b"vm", b""]))
    (proc / "oom_score_adj").write_text("0\n")
    for tid in (pid, *threads):
        (proc / "task" / str(tid)).mkdir()
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
    }}})  # JSON is YAML
)  # fmt: skip


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
        # that has everything right (probe_answer).
        self.probes = {}
        self.probe_hooks = []

    def probe_answer(self, host, params):
        """What a VM where everything is as its gameplan says answers the probe."""
        reply = {
            "started": time.time(), "flag": [True, ""], "dns": [True, ""], "scoring": [True, "HTTP 403"],
            "logging": [True, "HTTP 403"], "apt": [True, ""], "pypi": [True, "HTTP 200"], "time": [True, ""],
            "connect": {key: {"open": "open", "reachable": "refused", "blocked": "timeout"}[want] for key, _, _, want in params["connect"]},
            "forbidden": {label: "timeout" for label, _, _ in params["forbidden"]},
            "files": {key: "" for key, *_ in params["files"]},
            "services": {unit: "active" for unit in params["services"]},
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
            "sandbox": {"hosts": {name: {"addresses": info["addresses"]} for name, info in self._vms.items()}},
        }

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
            return self.qm_config.get(argv[2], "cores: 1\ncpu: x86-64-v3\nname: a-vm\n")
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


def _prepare(ex, cfg=None):
    return handlers.prepare(ex, cfg or CFG, validation.validate("prepare", PREPARE))


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
    assert result["sandbox"] == "built and snapshotted"


def test_a_failed_build_tells_the_database_nothing_of_the_gameplan():
    """Ansible prints the setup command that failed; the job's error (which the dashboard can
    read) names the job only, and the reason goes to the job log on the host."""

    class Failing(FakeExecutor):
        def worms(self, component, timeout=3600, args=None):
            raise RuntimeError('ansible-playbook exited 2: failed: [sandbox-a] => {"cmd": "echo hunter2 | chpasswd"}')

    ex = Failing()
    ex.sandbox_built = False
    with pytest.raises(RuntimeError) as failure:
        _prepare(ex)
    assert str(failure.value) == "the prepare job failed: its log on the host says why"
    assert any("hunter2" in note for note in ex.notes)


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
    private = next(c for c in calls if c["argv"][-1] == "/home/debian/.ssh/id_ed25519")
    assert private["host"] == "10.4.0.10" and "PRIVATE KEY" in private["input"] and "0600" in private["argv"]
    public = next(c for c in calls if c["argv"] == ["sudo", "tee", "-a", "/home/debian/.ssh/authorized_keys"])
    assert public["host"] == "10.4.0.11" and public["input"] == "ssh-ed25519 AAAAPUBLIC worms-gameplan\n"
    note = next(c for c in calls if c["argv"][-1] == "/home/debian/note")
    assert note["input"] == f"alice {secret}" and note["argv"][note["argv"].index("-o") + 1] == "debian"
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


def test_the_node_runs_only_the_gameplan_the_orchestrator_validated():
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
    assert result["sandbox"] == "rolled back to its snapshot" and {vm["name"] for vm in result["vms"]} == {"sandbox-a", "sandbox-b"}


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
    assert "not reset" in result["services"] and "apt-cache" in result["services"] and "rebuild services" in result["services"]


def test_the_devboxes_come_back_when_the_build_fails():
    ex = FakeExecutor()
    ex.sandbox_built = False
    ex.pools.update(devbox=list(DEVBOX_VMS))
    ex.fail_on = "apply"
    with pytest.raises(RuntimeError, match="prepare job failed"):
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
    """A VM's commands for the probe, on this machine: apt, the clock and systemd answer, and
    change nothing."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "apt-get": 'echo "Hit:1 http://apt-cache trixie InRelease"\n'
                   'if [ -n "$WORMS_TEST_SLOW" ]; then sleep 30 & echo $! > "$WORMS_TEST_PIDS/sleep"; wait; fi',
        # A VM whose gameplan took its time service away (WORMS_TEST_NO_NTP) has no NTP to ask.
        "timedatectl": 'printf "NTP=%s\\nNTPSynchronized=%s\\nCanNTP=yes\\n" "${WORMS_TEST_NO_NTP:-yes}" "${WORMS_TEST_NO_NTP:-yes}"',
        "systemctl": 'case "$2" in cron) echo active ;; *) echo inactive; exit 3 ;; esac',
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
    assert any(c["check"] == "vm:sandbox-a:running" and c["ok"] for c in result["checks"])
    assert ex.probes == {}  # without the run, the host alone

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

    # Right before the worm, with the run: every VM of its gameplan looked at from inside, all at once.
    ex.states = {}
    _prepare(ex)
    result = handlers.check(ex, CFG, validation.validate("check", {"run_uuid": RUN}))
    assert result["ok"] is True, result["failures"]
    names = {c["check"] for c in result["checks"]}
    for vm in ("sandbox-a", "sandbox-b"):
        assert {f"vm:{vm}:{p}" for p in ("probe", "flag", "dns", "scoring", "logging", "apt", "pypi", "time", "isolated", "firewall")} <= names
    assert {"vm:sandbox-b:files", "vm:sandbox-b:services", "host:inventory"} <= names
    assert "vm:sandbox-a:files" not in names  # its one file is written later, not at the reset
    assert next(c for c in ex.ssh_calls if c["argv"][:2] == ["sudo", "python3"])["argv"] == ["sudo", "python3", "-I", "-B", "-"]
    spec = handlers._gameplan(ex, "pair.py", PAIR_SHA)[0]
    address = {vm["name"]: vm["nics"][0]["address"].split("/")[0] for vm in spec["vms"]}
    a, b = ex.probes["10.4.0.10"], ex.probes["10.4.0.11"]
    # The gameplan's firewall: a may reach b's SSH (both sides allow it), b may not reach a's.
    assert a["connect"] == [["0", address["sandbox-b"], 22, "open"]] and b["connect"] == [["0", address["sandbox-a"], 22, "blocked"]]
    assert a["forbidden"] == [["dashboard", "10.10.0.18", 443], ["database", "10.11.0.16", 5432], ["orchestrator", "10.11.0.19", 443],
                              ["host", "10.4.0.1", 8006], ["internet", "1.1.1.1", 80], ["devbox", "10.2.0.33", 80],
                              ["maintenance", "10.4.0.11", 22]]  # fmt: skip
    assert a["dns"] == {"scoring.worms.internal": "10.2.0.14", "logging.worms.internal": "10.2.0.15",
                        "apt-cache.worms.internal": "10.2.0.20", "pypi.worms.internal": "10.2.0.21"}  # fmt: skip
    assert b["files"] == [["0", "/home/debian/note", "debian", "0644"]] and a["files"] == [] and b["services"] == ["cron"]
    assert a["pypi"] == ["pypi.worms.internal", 3141, "/root/pypi/+simple/requests/"]
    # The database (which the dashboard reads) gets none of the gameplan: no path, user, unit or rule.
    text = json.dumps(result)
    assert "/home/debian/note" not in text and "alice" not in text and "cron" not in text and address["sandbox-b"] not in text

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
        "deadline": 40, "apt_timeout": 20, "time_wait": 1, "connect_timeout": 1, "flag": str(tmp_path / "flag"), "tmp": str(tmp_path),
        "dns": {"localhost": "127.0.0.1"}, "https": {"scoring": ["127.0.0.1", closed_port], "logging": ["127.0.0.1", closed_port]},
        "pypi": ["127.0.0.1", page.server_address[1], "/root/pypi/+simple/requests/"],
        "connect": [["0", "127.0.0.1", open_port, "open"], ["1", "127.0.0.1", closed_port, "reachable"], ["2", "127.0.0.1", closed_port, "open"]],
        "forbidden": [["internet", "127.0.0.1", open_port]],
        "files": [["0", str(tmp_path / "note"), me, "0644"], ["1", str(tmp_path / "gone"), me, "0644"],
                  ["2", str(tmp_path / "note"), "nobody", "0600"]],
        "services": ["cron", "other"],
    }  # fmt: skip
    env = {"PATH": f"{_stand_ins(tmp_path)}:/usr/bin:/bin", "WORMS_TEST_PIDS": str(tmp_path / "pids")}
    try:
        ran = subprocess.run(["python3", "-I", "-B", "-"], input=probe.script(params), capture_output=True, text=True, timeout=60, env=env)
        reply = probe.answer(ran.stdout)
        assert reply is not None, ran.stderr
        assert [reply[key] for key in ("flag", "dns", "pypi", "apt", "time")] == [[True, ""], [True, ""], [True, "HTTP 200"], [True, ""], [True, ""]]
        assert reply["scoring"][0] is False and "Refused" in reply["scoring"][1]
        assert reply["connect"] == {"0": "open", "1": "refused", "2": "refused"} and reply["forbidden"] == {"internet": "open"}
        assert reply["files"] == {"0": "", "1": "missing", "2": f"owner {me}, mode 0644"} and reply["services"] == {"cron": "active", "other": "inactive"}
        assert "deadline" not in reply and not list(tmp_path.glob("worms-check-*"))  # apt's directory is gone again
        # Its own deadline: it says what it has, and leaves no command running and nothing behind. What
        # it cut off stays absent (apt's step, its command killed, would say "exit -9"). And a VM whose
        # time service the gameplan took away: its clock as the host sees it (records) decides.
        started = time.monotonic()
        ran = subprocess.run(["python3", "-I", "-B", "-"], input=probe.script({**params, "deadline": 3}), capture_output=True, text=True,
                             timeout=60, env={**env, "WORMS_TEST_SLOW": "1", "WORMS_TEST_NO_NTP": "no"})  # fmt: skip
        reply = probe.answer(ran.stdout)
        assert time.monotonic() - started < 10 and reply["deadline"] is True and "apt" not in reply and reply["flag"] == [True, ""]
        assert reply["time"] == [True, "no time service: its clock as the host sees it"]
        assert not list(tmp_path.glob("worms-check-*"))
        left = Path("/proc") / (tmp_path / "pids" / "sleep").read_text().strip() / "cmdline"  # apt's own child, killed with it
        for _ in range(20):
            if not left.exists() or not left.read_bytes():  # gone, or a zombie on its way out
                break
            time.sleep(0.1)
        assert not left.exists() or not left.read_bytes()
    finally:
        page.shutdown()
        listening.close()


def test_check_flags_a_down_vm_and_dead_service(tmp_path):
    ex = FakeExecutor()
    ex.systemctl["worms-mirror"] = "inactive"
    ex.qm["900"] = "status: stopped"
    result = handlers.check(ex, CFG, {})
    assert result["ok"] is False
    assert "host:worms-mirror" in result["failures"]
    assert "vm:sandbox-a:running" in result["failures"]

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

    # Right before the worm: a VM that is not as its gameplan says fails its probe's records.
    (tmp_path / "internet-allowed").unlink()
    ex = FakeExecutor()
    _prepare(ex)

    def broken(host, params, reply):
        if host == "10.4.0.11":  # sandbox-b
            reply["files"]["0"] = "owner root, mode 0600"
            reply["services"]["cron"] = "failed"
            reply["forbidden"]["database"] = "refused"  # an answer: it got there
            reply["apt"] = [False, "E: Failed to fetch http://apt-cache.worms.internal:3142/... 503"]
            reply["started"] -= 600  # its clock
        else:  # sandbox-a: b's SSH does not answer where the gameplan lets it in
            reply["connect"] = {key: "refused" for key in reply["connect"]}

    ex.probe_hooks.append(broken)
    result = handlers.check(ex, CFG, validation.validate("check", {"run_uuid": RUN}))
    assert set(result["failures"]) == {"vm:sandbox-a:firewall", "vm:sandbox-b:files", "vm:sandbox-b:services", "vm:sandbox-b:isolated",
                                       "vm:sandbox-b:apt", "vm:sandbox-b:time"}  # fmt: skip
    detail = {c["check"]: c["detail"] for c in result["checks"]}
    assert detail["vm:sandbox-b:isolated"] == "got to: database" and "-600 s" in detail["vm:sandbox-b:time"]
    assert detail["vm:sandbox-a:firewall"].startswith("1 of 1 connections not as the gameplan's firewall says")
    # What is wrong, in the job log; the database gets numbers and the platform's names only.
    text = json.dumps(result)
    assert "/home/debian/note" not in text and "cron" not in text
    assert any("file 0 (/home/debian/note, debian 0644): owner root, mode 0600" in note for note in ex.notes)
    assert any("service cron: failed" in note for note in ex.notes)
    assert any("sandbox-b port 22" in note and "open expected, got refused" in note for note in ex.notes)

    # A VM that does not answer at all, and one whose answer is cut off by its deadline.
    ex = FakeExecutor()
    _prepare(ex)
    ex.unreachable.add("10.4.0.10")
    ex.probe_hooks = [lambda host, params, reply: (reply.update(deadline=True), reply.pop("apt"))]
    result = handlers.check(ex, CFG, validation.validate("check", {"run_uuid": RUN}))
    detail = {c["check"]: c["detail"] for c in result["checks"]}
    assert {"vm:sandbox-a:probe", "vm:sandbox-b:probe", "vm:sandbox-b:apt"} <= set(result["failures"])
    assert not any(name.startswith("vm:sandbox-a:") and name.split(":")[2] not in ("running", "no-kvm", "probe") for name in detail)
    assert detail["vm:sandbox-a:probe"] == "no answer (the job log on the host says why)" and detail["vm:sandbox-b:apt"] == "no answer"
    assert any("connection refused" in note for note in ex.notes)
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

    names = ("a", "b", "c", "d", "e", "f", "g", "h", "i")
    ex = FakeExecutor({f"sandbox-{n}": {"maintenance_ip": f"10.4.0.{10 + i}", "vmid": 900 + i, "addresses": []} for i, n in enumerate(names)})
    ex.pools.update(devbox=list(DEVBOX_VMS))
    now = time.time()
    ex.states = {900: ("stopped", None), 901: ("stopped", None), 902: ("stopped", None), 903: ("running", "paused"),
                 904: ("stopped", None), 906: ("running", "internal-error"), 907: ("stopped", None), 908: ("stopped", None),
                 950: ("stopped", None)}  # fmt: skip
    ex.journal = "\n".join(f"{at:.6f} worms-dev {line}" for at, line in sorted([
        # a: it powered itself off; nothing of the host's then.
        (now - 1000, "systemd[1]: Started 900.scope."), (now - 100, "systemd[1]: 900.scope: Deactivated successfully."),
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
    vmids = [900, 901, 902, 903, 904, 905, 906, 907, 908, 950]
    seen = handlers.observe(ex, CFG, validation.validate("observe", {"vmids": vmids, "since": since}))
    causes = {vm["name"]: (vm["status"], vm["cause"]) for vm in seen["vms"]}
    assert causes == {
        "sandbox-a": ("stopped", "guest"), "sandbox-b": ("stopped", "oom"), "sandbox-c": ("stopped", "crash"),
        "sandbox-d": ("paused", "host"), "sandbox-e": ("stopped", "guest"), "sandbox-f": ("running", None),
        # A guest halted for good is stopped, by the guest: the worm's doing, as a power-off is.
        "sandbox-g": ("stopped", "guest"), "sandbox-h": ("stopped", "guest"), "sandbox-i": ("stopped", "crash"),
        "sandbox-gw": ("stopped", "host"),
    }  # fmt: skip
    detail = {vm["name"]: vm["detail"] for vm in seen["vms"]}
    assert detail["sandbox-d"].startswith(f"qmpause by {CONDUCTOR}") and detail["sandbox-gw"].startswith("qmstop by root@pam")
    assert detail["sandbox-g"].startswith("KVM could not run its code (QEMU internal-error)")
    assert detail["sandbox-g"].endswith("Its QEMU holds it until it is stopped")
    assert detail["sandbox-i"].startswith("its QEMU dumped core") and detail["sandbox-h"].startswith("powered off from inside")
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
         "artifact": {"storage_key": key, "content_hash": "0" * 64}, "worm_delay": 60},
    )
    with pytest.raises(validation.Invalid, match="hash mismatch"):
        handlers.deploy(ex, CFG, params)


def test_teardown():
    ex = FakeExecutor()
    _prepare(ex)
    state = Path(handlers.STATE_DIR) / "runs" / f"{RUN}.json"
    assert state.exists()
    assert handlers.teardown(ex, CFG, validation.validate("teardown", {"run_uuid": RUN}))["destroyed"] is True
    assert not state.exists()  # the run's secrets are gone
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


def test_a_reconfigure_writes_the_map_before_the_minutes_of_configuration(tmp_path):
    """After a reset of scoring and logging, the map is what is missing. Then only the keys
    (devbox-keys: no package, no internet), not the devbox's whole configuration."""
    ex = DevboxFake(tmp_path, built_before=True)
    order = []
    ex.ssh = lambda host, argv, input=None, timeout=120: order.append(("map", host)) if handlers.DEVBOX_TEAMS_FILE in argv else None
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


def test_the_agent_is_the_node_whatever_roots_worms_config_says(render, tmp_path, monkeypatch):
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
    assert state == "failed" and f"{uuid}.log" in error and "last failure: fatal: the reason" in error
    assert "step one done" not in error
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
        "worm_delay": delay,
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
