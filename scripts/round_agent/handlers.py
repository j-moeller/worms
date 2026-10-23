"""The fixed menu of privileged operations the round agent runs on the host.

One function per host-job kind. Each takes the executor, the agent config and
the already-validated parameters, and returns the JSON result the conductor
reads. They contain no string taken from the database: every command is built
from fixed literals and validated values.

See docs/rounds.md for how these fit the round lifecycle.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import tempfile
from pathlib import Path

from round_agent import validation

FLAG_PATH = "/etc/worms/flag"
WORM_DIR = "/home/debian"
WORM_ZIP = "user-worm.zip"
WORM_UNIT = "worms-start"


def _sandbox_vms(ex):
    """{name: {maintenance_ip, vmid, addresses}} from the sandbox stack state."""
    hosts = ex.tofu_output("sandbox")
    ansible_hosts = hosts.get("ansible_hosts", {}) or {}
    sandbox = (hosts.get("sandbox", {}) or {}).get("hosts", {}) or {}
    vms = {}
    for key, vm in ansible_hosts.items():
        if key == "sandbox_gw":
            continue
        vms[vm["name"]] = {
            "maintenance_ip": vm["management_ip"],
            "vmid": int(vm["vm_id"]),
            "addresses": sandbox.get(key, {}).get("addresses", []),
        }
    return vms


def _topology(network_graph):
    """The generator's graph -> {networks, edges, vm_networks}."""
    nodes = network_graph.get("nodes", [])
    edges = network_graph.get("edges", [])
    networks = [
        {"name": n["label"], "id": n["id"], "cidr": n.get("cidr"), "type": n.get("network_type")}
        for n in nodes
        if n.get("type") == "network"
    ]
    by_id = {n["id"]: n for n in nodes}
    vm_networks = {}
    topo_edges = []
    for edge in edges:
        if edge.get("relationship") == "attached-to":
            vm = by_id.get(edge["source"], {})
            net = by_id.get(edge["target"], {})
            name = (vm.get("label") or "").strip()
            if name:
                vm_networks.setdefault(name, []).append(
                    {"name": net.get("label"), "cidr": net.get("cidr")}
                )
                topo_edges.append({"vm": name, "network": net.get("label")})
    return {"networks": networks, "edges": topo_edges}, vm_networks


def _release(ex, cfg, vm_names):
    """Generate a fresh secret for each VM, write it into the VM over SSH
    (the secret goes through stdin, never the command line) and return the
    SHA-256 of each. The plaintext never leaves the host."""
    vms = _sandbox_vms(ex)
    released = []
    for name in vm_names:
        info = vms.get(name)
        if info is None:
            raise validation.Invalid(f"{name} is not a VM of the current sandbox")
        secret = f"FLAG{{{secrets.token_hex(16)}}}"
        ex.ssh(
            info["maintenance_ip"],
            ["sudo", "install", "-m", "0444", "/dev/stdin", FLAG_PATH],
            input=secret,
        )
        released.append({"vm": name, "flag_hash": hashlib.sha256(secret.encode()).hexdigest()})
    return released


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------


def prepare(ex, cfg, params):
    """Build the sandbox for this run: generate the architecture from the
    seed, apply it, refresh the inventory, and report the topology and VMs.

    Optionally (cfg.reset_services) the ephemeral and private-internet VMs are
    rebuilt first for a clean slate; that needs an internet window and so must
    happen before the sandbox exists (see docs/rounds.md)."""
    if cfg.get("reset_services"):
        ex.make("round-reset-services", timeout=cfg.get("reset_timeout", 5400))

    with tempfile.TemporaryDirectory() as tmp:
        network_file = Path(tmp) / "network.json"
        hcl = ex.generate_sandbox(params["seed"], params["sandbox_params"], network_file)
        ex.install_sandbox_definition(hcl)
        graph = ex.read_json(network_file)

    ex.tofu("sandbox", "init", "-input=false", timeout=600)
    ex.tofu("sandbox", "apply", "-auto-approve", "-input=false", "-parallelism=1", timeout=cfg.get("apply_timeout", 5400))
    ex.make("inventory", timeout=300)

    topology, vm_networks = _topology(graph)
    vms = _sandbox_vms(ex)
    return {
        "topology": topology,
        "vms": [
            {
                "name": name,
                "maintenance_ip": info["maintenance_ip"],
                "vmid": info["vmid"],
                "addresses": info["addresses"],
                "networks": vm_networks.get(name, []),
            }
            for name, info in sorted(vms.items())
        ],
    }


def check(ex, cfg, params):
    """Sanity checks on the host and the sandbox. Returns {ok, checks}."""
    results = []

    def record(name, ok, detail=""):
        results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:200]})

    for unit in ("proxmox-firewall", "worms-host-firewall", "worms-firewall-watchdog.timer", "worms-mirror"):
        out = ex.run(["systemctl", "is-active", unit], check=False).strip()
        record(f"host:{unit}", out == "active", out)

    mirror = ex.run(["/usr/local/sbin/worms-mirror", "status"], check=False, timeout=60)
    record("host:mirror-receive-only", "drop" in mirror, "")

    vms = _sandbox_vms(ex)
    record("sandbox:vms-registered", bool(vms), f"{len(vms)} VM(s)")
    for name, info in sorted(vms.items()):
        status = ex.run(["qm", "status", str(info["vmid"])], check=False, timeout=60).strip()
        record(f"vm:{name}:running", status.endswith("running"), status)

    failures = [r["check"] for r in results if not r["ok"]]
    return {"ok": not failures, "failures": failures, "checks": results}


def deploy(ex, cfg, params):
    """Put the team's worm on the start VM, release the initial secrets, and
    arm the fuse that starts the worm after worm_delay seconds."""
    vms = _sandbox_vms(ex)
    start = params["start_vm"]
    if start not in vms:
        raise validation.Invalid(f"start_vm {start} is not in the sandbox")
    start_ip = vms[start]["maintenance_ip"]

    artifact = _fetch_artifact(ex, cfg, params["artifact"])
    # Copy the verified zip onto the start VM, base64 over stdin so the binary
    # survives the text channel and never appears on a command line.
    ex.ssh(
        start_ip,
        ["/bin/sh", "-c", f'base64 -d > {WORM_DIR}/{WORM_ZIP}'],
        input=base64.b64encode(artifact).decode("ascii"),
    )

    released = _release(ex, cfg, params["initial_secrets"])

    # Arm a one-shot systemd timer on the VM: after worm_delay it unzips and
    # runs the worm as the sandbox user. The command is a fixed literal.
    delay = params["worm_delay"]
    ex.ssh(
        start_ip,
        [
            "sudo", "systemd-run", f"--on-active={delay}", f"--unit={WORM_UNIT}",
            "--working-directory", WORM_DIR, "--uid", ex.ssh_user,
            "/bin/sh", "-lc", f"unzip -o {WORM_ZIP} && python3 main.py",
        ],
    )
    return {"released": released, "start_vm": start, "worm_delay": delay}


def release_secrets(ex, cfg, params):
    return {"released": _release(ex, cfg, params["vms"])}


def teardown(ex, cfg, params):
    ex.tofu("sandbox", "destroy", "-auto-approve", "-input=false", timeout=cfg.get("apply_timeout", 5400))
    ex.make("inventory", timeout=300)
    return {"destroyed": True}


def _fetch_artifact(ex, cfg, artifact):
    """Read the artifact from the dashboard's media directory over SSH and
    verify its content hash before it is used. A mismatch fails the job."""
    media_root = cfg["dashboard_media_root"].rstrip("/")
    path = f"{media_root}/{artifact['storage_key']}"
    # The key is validated against a strict pattern (no traversal); still, pass
    # it as a bare argument, never through a shell. base64 keeps the binary
    # intact over the text channel.
    encoded = ex.ssh(cfg["dashboard_host"], ["base64", path], timeout=120)
    raw = base64.b64decode(encoded)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != artifact["content_hash"]:
        raise validation.Invalid(
            f"artifact hash mismatch: expected {artifact['content_hash']}, got {digest}"
        )
    return raw


HANDLERS = {
    "prepare": prepare,
    "check": check,
    "deploy": deploy,
    "release_secrets": release_secrets,
    "teardown": teardown,
}
