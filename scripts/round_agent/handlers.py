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
CURRENT_TEAM_FILE = "/etc/sandbox-api/current-team"
DEVBOX_TEAMS_FILE = "/etc/sandbox-api/devbox-teams"
DEVBOX_GENERATED = "opentofu/stacks/devbox/generated_devboxes.tf"


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


def _vm_networks(network_graph):
    """From the generator's graph, the networks each VM is attached to
    (keyed by VM name), for the VM rows."""
    nodes = network_graph.get("nodes", [])
    by_id = {n["id"]: n for n in nodes}
    vm_networks = {}
    for edge in network_graph.get("edges", []):
        if edge.get("relationship") == "attached-to":
            vm = by_id.get(edge["source"], {})
            net = by_id.get(edge["target"], {})
            name = (vm.get("label") or "").strip()
            if name:
                vm_networks.setdefault(name, []).append(
                    {"name": net.get("label"), "cidr": net.get("cidr")}
                )
    return vm_networks


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
    seed, apply it, refresh the inventory, and report the topology and VMs."""
    with tempfile.TemporaryDirectory() as tmp:
        network_file = Path(tmp) / "network.json"
        hcl = ex.generate_sandbox(params["seed"], params["sandbox_params"], network_file)
        ex.install_sandbox_definition(hcl)
        graph = ex.read_json(network_file)

    ex.tofu("sandbox", "init", "-input=false", timeout=600)
    ex.tofu("sandbox", "apply", "-auto-approve", "-input=false", "-parallelism=1", timeout=cfg.get("apply_timeout", 5400))
    # Refresh the inventory and configure the sandbox guests (install the
    # internal CA, so a worm can reach scoring/logging over verified HTTPS).
    ex.worms("round-configure-sandbox", timeout=cfg.get("configure_timeout", 1800))

    vm_networks = _vm_networks(graph)
    vms = _sandbox_vms(ex)
    # The full generator graph (every layer: internet, edge/core/distribution/
    # access switches, networks and VMs, with all edges) and the generated
    # OpenTofu definition are saved on the run for later visualisation.
    return {
        "topology": graph,
        "sandbox_definition": hcl,
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

    # Tell the scoring and logging servers which team is running now, so they
    # record its uuid on every submission (no access token is used anymore).
    _set_current_team(ex, cfg, params["team_uuid"])

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
    return {"released": released, "start_vm": start, "worm_delay": delay, "team_uuid": params["team_uuid"]}


def _set_current_team(ex, cfg, uuid):
    """Write the running team's uuid into the current-team file on the scoring
    and logging servers (root:www-data 0640; uuid over stdin, not argv)."""
    for host in (cfg.get("scoring_host"), cfg.get("logging_host")):
        if not host:
            raise validation.Invalid("scoring_host/logging_host not configured for the round agent")
        ex.ssh(
            host,
            ["sudo", "install", "-m", "0640", "-o", "root", "-g", "www-data", "/dev/stdin", CURRENT_TEAM_FILE],
            input=uuid,
        )


def release_secrets(ex, cfg, params):
    return {"released": _release(ex, cfg, params["vms"])}


def teardown(ex, cfg, params):
    ex.tofu("sandbox", "destroy", "-auto-approve", "-input=false", timeout=cfg.get("apply_timeout", 5400))
    ex.worms("inventory", timeout=300)
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


# ---------------------------------------------------------------------------
# Devbox (docs/devbox.md)
# ---------------------------------------------------------------------------


def _devbox_vms(ex):
    """{name: {maintenance_ip, vmid, team, ssh_ip}} from the devbox stack state."""
    hosts = ex.tofu_output("devbox").get("ansible_hosts", {}) or {}
    vms = {}
    for key, host in hosts.items():
        if key == "ssh_gateway" or key.endswith("-gw"):
            continue
        hv = host.get("host_vars", {}) or {}
        vms[host["name"]] = {
            "maintenance_ip": host["management_ip"],
            "vmid": int(host["vm_id"]),
            "team": hv.get("devbox_team"),
            "ssh_ip": hv.get("devbox_ssh_ip"),
        }
    return vms


def _write_devbox_token(ex, maintenance_ip):
    """Generate a token, write it into a devbox VM, return its SHA-256."""
    secret = f"FLAG{{{secrets.token_hex(16)}}}"
    ex.ssh(maintenance_ip, ["sudo", "install", "-m", "0444", "/dev/stdin", FLAG_PATH], input=secret)
    return hashlib.sha256(secret.encode()).hexdigest()


def _write_devbox_map(ex, cfg, teams):
    """Write the source-address -> team-uuid map onto scoring and logging, so a
    devbox submission is attributed to its team (docs/devbox.md). The services
    source of a team is its devbox-gw address from the stack output."""
    devbox = ex.tofu_output("devbox").get("devbox", {}) or {}
    lines = []
    for team in teams:
        info = devbox.get(team["slug"])
        if info and info.get("services_source_ip"):
            lines.append(f"{info['services_source_ip']} {team['team_uuid']}")
    body = "".join(f"{line}\n" for line in lines)
    for host in (cfg.get("scoring_host"), cfg.get("logging_host")):
        if not host:
            raise validation.Invalid("scoring_host/logging_host not configured for the round agent")
        ex.ssh(host, ["sudo", "install", "-m", "0640", "-o", "root", "-g", "www-data", "/dev/stdin", DEVBOX_TEAMS_FILE], input=body)


def _devbox_report(ex, teams):
    """Per-team devbox info (VMs + tokens + source ip) for the orchestrator to
    store in the database (devboxes, devbox_vms)."""
    vms = _devbox_vms(ex)
    devbox = ex.tofu_output("devbox").get("devbox", {}) or {}
    report = []
    by_team = {t["slug"]: t for t in teams}
    for name, info in sorted(vms.items()):
        if info["team"] not in by_team:
            continue
        report.append(
            {
                "team_uuid": by_team[info["team"]]["team_uuid"],
                "team_slug": info["team"],
                "name": name,
                "maintenance_ip": info["maintenance_ip"],
                "vmid": info["vmid"],
                "ssh_ip": info["ssh_ip"],
                "flag_hash": _write_devbox_token(ex, info["maintenance_ip"]),
            }
        )
    sources = {slug: d.get("services_source_ip") for slug, d in devbox.items()}
    return report, sources


def devbox_provision(ex, cfg, params):
    """Build (or update) every team's devbox and the SSH gateway: generate the
    stack definition for all teams, apply it, configure the VMs and the gateway
    (CA, team keys), write fresh tokens, and publish the source->team map.
    Returns the devbox records for the orchestrator to register."""
    teams = params["teams"]
    ex.generate_devbox(teams, ex.repo_root / DEVBOX_GENERATED)
    ex.worms("devbox-apply", timeout=cfg.get("apply_timeout", 5400))
    _configure_devbox(ex, teams)
    report, sources = _devbox_report(ex, teams)
    _write_devbox_map(ex, cfg, teams)
    return {"vms": report, "services_sources": sources}


def devbox_rebuild(ex, cfg, params):
    """Rebuild one team's devbox VMs (fresh disks) and rewrite their tokens,
    without touching other teams. The team's version has already been bumped by
    the orchestrator (which clears the dashboard score); here the VMs are
    replaced so the environment is truly fresh."""
    teams = params["teams"]
    slug = next((t["slug"] for t in teams if t["team_uuid"] == params["team_uuid"]), None)
    if slug is None:
        raise validation.Invalid("team_uuid is not in the teams list")

    ex.generate_devbox(teams, ex.repo_root / DEVBOX_GENERATED)
    # Replace just this team's three VMs (fresh disks); the rest is a no-op.
    replaces = " ".join(
        f'-replace=module.devbox_vm[\\"devbox-{slug}-{role}\\"].proxmox_virtual_environment_vm.vm'
        for role in ("a", "b", "c")
    )
    ex.tofu("devbox", "apply", "-auto-approve", "-input=false", "-parallelism=1", *replaces.split(), timeout=cfg.get("apply_timeout", 5400))
    ex.worms("inventory", timeout=300)
    _configure_devbox(ex, teams)

    # Rewrite this team's tokens only.
    vms = _devbox_vms(ex)
    rebuilt = []
    for name, info in sorted(vms.items()):
        if info["team"] == slug:
            rebuilt.append({"name": name, "flag_hash": _write_devbox_token(ex, info["maintenance_ip"])})
    _write_devbox_map(ex, cfg, teams)
    return {"team_uuid": params["team_uuid"], "devbox_version": params["devbox_version"], "vms": rebuilt}


def _configure_devbox(ex, teams):
    """Run the devbox + SSH-gateway Ansible with the teams' keys and VM targets
    as extra vars (CA trust, team authorized_keys, confined gateway keys)."""
    import json
    import tempfile

    devbox = ex.tofu_output("devbox").get("devbox", {}) or {}
    team_keys = {t["slug"]: t["authorized_keys"] for t in teams}
    gateway_keys = []
    for t in teams:
        vms = (devbox.get(t["slug"], {}) or {}).get("vms", [])
        # The gateway forwards each team to its ENTRY VM (A) only; the worm
        # pivots A->B->C inside the team's own devbox network, as in the sandbox.
        # permitopen is thus a single host, so a team key cannot open a forward
        # to B/C directly (docs/devbox.md; docs/security-assessment.md).
        entry = f"devbox-{t['slug']}-a"
        targets = [f"{vm['ssh_fabric_ip']}:22" for vm in vms if vm["name"] == entry]
        for key in t["authorized_keys"]:
            gateway_keys.append({"public_key": key, "comment": f"team={t['slug']}", "permitopen": targets})
    extra = {"devbox_team_keys": team_keys, "ssh_gateway_authorized_keys": gateway_keys}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(extra, fh)
        varfile = fh.name
    ex.worms("devbox-configure", timeout=1800, args=f"-e @{varfile}")


HANDLERS = {
    "prepare": prepare,
    "check": check,
    "deploy": deploy,
    "release_secrets": release_secrets,
    "teardown": teardown,
    "devbox_provision": devbox_provision,
    "devbox_rebuild": devbox_rebuild,
}
