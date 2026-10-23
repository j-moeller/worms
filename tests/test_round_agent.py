"""The round agent (scripts/round_agent): parameter validation, the handler
command sequences (with a fake executor, so no Proxmox/SSH), and the database
claim/run/result loop against the test cluster.

The agent is pure Python (no Django); only the loop test needs a database.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys

import pytest

from conftest import REPO, require

sys.path.insert(0, str(REPO / "scripts"))

from round_agent import handlers, validation  # noqa: E402
from round_agent.agent import Agent  # noqa: E402


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
            "artifact": {"storage_key": key, "content_hash": "d" * 64},
            "initial_secrets": ["sandbox-site-1-vm-1", "sandbox-site-2-vm-2"],
            "worm_delay": 300,
        },
    )
    assert clean["artifact"]["storage_key"] == key
    assert clean["worm_delay"] == 300


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

    def make(self, target, timeout=3600):
        self.calls.append(("make", target))

    def tofu(self, stack, *args, timeout=3600):
        self.calls.append(("tofu", stack, *args))
        return ""

    def tofu_output(self, stack):
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
        if argv[0] == "systemctl":
            return self.systemctl.get(argv[2], "active")
        if argv[0] == "qm":
            return self.qm.get(argv[2], "status: running")
        if argv[0] == "/usr/local/sbin/worms-mirror":
            return "tap900i0 wtsb0 mirror-all mirror-all\ntap960i2 wtmir drop drop"
        return ""

    def ssh(self, host, argv, input=None, timeout=120):
        self.ssh_calls.append({"host": host, "argv": argv, "input": input})
        if argv and argv[0] == "base64" and input is None:
            return base64.b64encode(self.artifact_bytes).decode()
        return ""


CFG = {"dashboard_host": "10.5.0.18", "dashboard_media_root": "/opt/dashboard/media", "reset_services": False}


def test_prepare_builds_sandbox_and_reports_topology():
    ex = FakeExecutor()
    result = handlers.prepare(ex, CFG, validation.validate("prepare", {"seed": 5, "sandbox_params": {"vms": 2}}))
    assert ("generate", 5, 2) in ex.calls
    assert any(c[:2] == ("tofu", "sandbox") and "apply" in c for c in ex.calls)
    assert ("make", "inventory") in ex.calls
    assert ex.installed_hcl is not None
    names = {vm["name"] for vm in result["vms"]}
    assert names == {"sandbox-a", "sandbox-b"}
    assert result["vms"][0]["networks"] == [{"name": "vmbr1000", "cidr": "10.64.0.0/24"}]
    assert result["topology"]["networks"][0]["name"] == "vmbr1000"


def test_prepare_reset_services_runs_first():
    ex = FakeExecutor()
    handlers.prepare(ex, {**CFG, "reset_services": True}, validation.validate("prepare", {"seed": 1}))
    assert ex.calls[0] == ("make", "round-reset-services")


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
            "artifact": {"storage_key": key, "content_hash": hashlib.sha256(ex.artifact_bytes).hexdigest()},
            "initial_secrets": ["sandbox-a"],
            "worm_delay": 120,
        },
    )
    result = handlers.deploy(ex, CFG, params)
    assert result["released"][0]["vm"] == "sandbox-a"
    # Fetched from the dashboard.
    assert any(c["host"] == "10.5.0.18" and c["argv"][0] == "base64" for c in ex.ssh_calls)
    # Armed a one-shot timer with the validated delay.
    arm = next(c for c in ex.ssh_calls if any("systemd-run" in str(a) for a in c["argv"]))
    assert "--on-active=120" in arm["argv"]


def test_deploy_rejects_tampered_artifact():
    ex = FakeExecutor()
    key = "malware-artifacts/aa/bb/" + "c" * 64 + ".zip"
    params = validation.validate(
        "deploy",
        {"start_vm": "sandbox-a", "artifact": {"storage_key": key, "content_hash": "0" * 64}, "initial_secrets": [], "worm_delay": 60},
    )
    with pytest.raises(validation.Invalid, match="hash mismatch"):
        handlers.deploy(ex, CFG, params)


def test_teardown():
    ex = FakeExecutor()
    assert handlers.teardown(ex, CFG, {})["destroyed"] is True
    assert any(c[:2] == ("tofu", "sandbox") and "destroy" in c for c in ex.calls)


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


def test_round_agent_env_renders(render):
    env = render(
        "ansible/roles/host/round_agent", "round-agent.env.j2",
        infrastructure_ips={"database": "10.11.0.16"},
        management_ips={"dashboard": "10.5.0.18"},
        postgres_db="mydb",
        pki_ca_cert_path="/etc/worms/ca.crt",
        vault_round_agent_db_password="secret",
    )
    assert "DB_USER=round_agent" in env and "DB_SSLMODE=verify-full" in env
    assert "PYTHONPATH=" in env and "/scripts" in env
    assert "DASHBOARD_HOST=10.5.0.18" in env
