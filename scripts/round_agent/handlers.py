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
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from generate_devbox import team_addressing
from round_agent import snapshots, validation

FLAG_PATH = "/etc/worms/flag"
WORM_DIR = "/home/debian"
WORM_ZIP = "user-worm.zip"
# The zip is unpacked here; the worm is `bash main.sh` run in this
# directory (docs/worm-contract.md).
WORM_CODE = "worm"
WORM_ENTRY = "main.sh"
WORM_UNIT = "worms-start"
# Unpack with Python's zipfile (python3 is in every Debian cloud image -- cloud-
# init and Ansible need it; unzip is not installed). It keeps every entry
# inside the target directory (absolute paths and `..` are stripped) and
# writes symlinks as plain files.
UNPACK = (
    "set -e; cd {dir}; rm -rf {code}; mkdir {code}; "
    "python3 -m zipfile -e {zip} {code}; "
    "test -f {code}/{entry} || {{ echo 'the upload has no {entry} at its top level' >&2; exit 3; }}"
)
CURRENT_TEAM_FILE = "/etc/sandbox-api/current-team"
DEVBOX_TEAMS_FILE = "/etc/sandbox-api/devbox-teams"
DEVBOX_GENERATED = "opentofu/stacks/devbox/generated_devboxes.tf"
DEVBOX_ROLES = ("a", "b", "c")
# While it exists, internet access is allowed (ansible Internet.yml).
INTERNET_FLAG = "/run/worms/internet-allowed"
# The VMs a sandbox reaches, which a new round's sandbox is built on after a rollback; their
# snapshot is `worms build`'s (src/worms_cli/pipeline.py).
SERVICE_POOLS = ("ephemeral", "private_internet")
# The agent's own records on the node (root only).
STATE_DIR = "/var/lib/worms"
# Team code is assumed hostile and root on its VM. This CPU offers it no hardware virtualisation,
# so no /dev/kvm and no way to drive the node's nested-KVM code (docs/security-assessment.md). The
# sandbox and devbox stacks set it. These checks refuse anything else, such as a VM changed by hand.
TEAM_VM_CPU = "x86-64-v3"


def _has_team_vm_cpu(ex, vmid):
    """Read from the node, because what a hostile VM says about itself proves nothing. Anything
    but exactly this CPU fails, including added flags and a config that could not be read."""
    config = ex.run(["qm", "config", str(vmid)], check=False, timeout=60)
    return any(line.strip() == f"cpu: {TEAM_VM_CPU}" for line in config.splitlines())


def _ansible_hosts(stack, outputs):
    """A stack's `ansible_hosts` output: {} when nothing is applied (no outputs), an error
    when its last apply did not finish (outputs, but not this one): never "no VMs"."""
    if outputs and "ansible_hosts" not in outputs:
        raise RuntimeError(
            f"the {stack} stack has outputs ({', '.join(sorted(outputs))}) but no ansible_hosts: its last apply "
            f"did not finish. Find its error in the job log, fix it, and enqueue the job again"
        )
    return outputs.get("ansible_hosts") or {}


def _sandbox_vms(ex):
    """{name: {maintenance_ip, vmid, addresses}} from the sandbox stack state."""
    hosts = ex.tofu_output("sandbox")
    ansible_hosts = _ansible_hosts("sandbox", hosts)
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
            # -D: /etc/worms does not exist in a fresh VM.
            ["sudo", "install", "-D", "-m", "0444", "/dev/stdin", FLAG_PATH],
            input=secret,
        )
        released.append({"vm": name, "flag_hash": hashlib.sha256(secret.encode()).hexdigest()})
    return released


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------


def _refuse_while_internet(cfg, what):
    """Team VMs never run while the internet window is open (Internet.yml): the services they
    reach could then reach out. OpenTofu refuses to build them then; a rollback or a start
    goes past it, so these check it themselves."""
    if Path(cfg.get("internet_flag", INTERNET_FLAG)).exists():
        raise RuntimeError(f"internet access is allowed ({INTERNET_FLAG}): refusing to {what}")


def _sandbox_hosts(ex):
    """Every sandbox VM's management address, its gateway's too."""
    hosts = _ansible_hosts("sandbox", ex.tofu_output("sandbox"))
    return [host["management_ip"] for host in hosts.values()]


def _reusable_sandbox(ex, hcl):
    """The VMs of the sandbox the stack holds, if it is the one `hcl` defines and every VM
    of it has its clean snapshot; else None."""
    if ex.sandbox_definition() != hcl:
        return None
    built = {int(host["vm_id"]) for host in _ansible_hosts("sandbox", ex.tofu_output("sandbox")).values()}
    pool = snapshots.pool_vms(ex, "sandbox")
    if not built or {vm["vmid"] for vm in pool} != built:
        return None
    return pool if all(snapshots.has_clean(ex, vm) for vm in pool) else None


def _destroy_sandbox(ex, cfg):
    # One at a time: VMs destroyed in parallel fight over the storage lock, and a volume
    # Proxmox gives up on then blocks whichever new VM gets that ID.
    ex.tofu("sandbox", "destroy", "-auto-approve", "-input=false", "-parallelism=1", timeout=cfg.get("apply_timeout", 5400))
    ex.worms("inventory", timeout=300)


def _reset_services(ex, cfg):
    """The service VMs back to the snapshot `worms build` took of them, all or none. Returns
    what happened, for the run's record: without a snapshot on each, they stay as they are."""
    vms = [vm for pool in SERVICE_POOLS for vm in snapshots.pool_vms(ex, pool)]
    missing = sorted(vm["name"] for vm in vms if not snapshots.has_clean(ex, vm))
    if not vms or missing:
        return f"not reset: no clean snapshot on {', '.join(missing) or 'any service VM'} (`worms build rebuild-services`)"
    snapshots.roll_back(ex, vms)
    snapshots.wait_for_ssh(ex, cfg.get("service_hosts") or [cfg["scoring_host"], cfg["logging_host"]])
    _restore_devbox_map(ex, cfg)
    return f"reset: {len(vms)} VMs back to their clean snapshot"


def _build_sandbox(ex, cfg, hcl):
    """A new sandbox, snapshotted before any team's code reaches it. While it is built the
    devbox VMs are shut down and the service VMs it installs from are back to their snapshot,
    so nothing in it comes from team code: every later run of the round rolls back to it."""
    _refuse_while_internet(cfg, "build a sandbox")
    if ex.tofu_output("sandbox"):
        _destroy_sandbox(ex, cfg)  # another round's, or one left half built: it may hold team code
    devboxes = snapshots.shut_down(ex, snapshots.pool_vms(ex, "devbox"))
    try:
        services = _reset_services(ex, cfg)
        ex.install_sandbox_definition(hcl)
        ex.tofu("sandbox", "init", "-input=false", timeout=600)
        ex.tofu("sandbox", "apply", "-auto-approve", "-input=false", "-parallelism=1", timeout=cfg.get("apply_timeout", 5400))
        # Refresh the inventory and configure the sandbox guests (install the
        # internal CA, so a worm can reach scoring/logging over verified HTTPS).
        ex.worms("round-configure-sandbox", timeout=cfg.get("configure_timeout", 1800))
        snapshots.take(ex, snapshots.pool_vms(ex, "sandbox"), f"worms round sandbox {hashlib.sha256(hcl.encode()).hexdigest()[:12]}")
    finally:
        if devboxes:
            _refuse_while_internet(cfg, "start the devbox VMs again")
            snapshots.start(ex, devboxes)
    return services


def prepare(ex, cfg, params):
    """The run's sandbox. The runs of a round share one, generated from the round's seed and
    parameters: the first run builds and snapshots it, every later one rolls it back to that
    snapshot, which takes a boot instead of a build and leaves nothing of the team before.
    Reports the topology and VMs."""
    with tempfile.TemporaryDirectory() as tmp:
        network_file = Path(tmp) / "network.json"
        hcl = ex.generate_sandbox(params["seed"], params["sandbox_params"], network_file)
        graph = ex.read_json(network_file)

    built = _reusable_sandbox(ex, hcl)
    if built:
        _refuse_while_internet(cfg, "start a sandbox")
        snapshots.roll_back(ex, built)
        services = "not reset: a later run of the round (the first one reset them)"
    else:
        services = _build_sandbox(ex, cfg, hcl)
    snapshots.wait_for_ssh(ex, _sandbox_hosts(ex))

    vm_networks = _vm_networks(graph)
    vms = _sandbox_vms(ex)
    # The full generator graph (every layer: internet, edge/core/distribution/
    # access switches, networks and VMs, with all edges) and the generated
    # OpenTofu definition are saved on the run for later visualisation.
    return {
        "sandbox": "rolled back to its snapshot" if built else "built and snapshotted",
        "services": services,
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
        record(f"vm:{name}:no-kvm", _has_team_vm_cpu(ex, info["vmid"]), f"its CPU must be {TEAM_VM_CPU}")

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

    _place_worm(ex, cfg, start_ip, params["artifact"])
    released = _release(ex, cfg, params["initial_secrets"])
    delay = params["worm_delay"]
    armed_at = _arm_worm(ex, start_ip, delay)
    return {
        "released": released, "start_vm": start, "worm_delay": delay,
        "team_uuid": params["team_uuid"], "armed_at": armed_at,
    }  # fmt: skip


def _place_worm(ex, cfg, ip, artifact):
    """The verified zip onto a VM, unpacked."""
    raw = _fetch_artifact(ex, cfg, artifact)
    # base64 over stdin so the binary survives the text channel and never appears on a command line.
    ex.ssh(
        ip,
        ["/bin/sh", "-c", f"base64 -d > {WORM_DIR}/{WORM_ZIP}"],
        input=base64.b64encode(raw).decode("ascii"),
    )
    # Unpack it now, so a broken upload fails the deploy with a reason
    # instead of a worm that never starts.
    ex.ssh(
        ip,
        ["/bin/sh", "-c", UNPACK.format(dir=WORM_DIR, code=WORM_CODE, zip=WORM_ZIP, entry=WORM_ENTRY)],
    )


def _arm_worm(ex, ip, delay, *properties):
    """Arm a one-shot systemd timer on the VM: after `delay` it runs the worm as the login user,
    in its directory. No shell: fixed literals. Returns when the fuse was lit (the host's clock):
    the worm's start is dated from this, not from when anyone noticed the job was done."""
    ex.ssh(
        ip,
        [
            "sudo", "systemd-run", f"--on-active={delay}", f"--unit={WORM_UNIT}", *properties,
            "--working-directory", f"{WORM_DIR}/{WORM_CODE}", "--uid", ex.ssh_user,
            "/bin/bash", WORM_ENTRY,
        ],
    )
    return datetime.now(timezone.utc).isoformat()


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


def _clear_current_team(ex, cfg):
    """No run is active: scoring and logging refuse round submissions again
    (no file = no active round) instead of recording the last team's uuid."""
    for host in (cfg.get("scoring_host"), cfg.get("logging_host")):
        if host:
            ex.ssh(host, ["sudo", "rm", "-f", CURRENT_TEAM_FILE])


def release_secrets(ex, cfg, params):
    return {"released": _release(ex, cfg, params["vms"])}


def teardown(ex, cfg, params):
    """After a run. With `keep` (another run of the round follows) the sandbox stays, powered
    off, for the next prepare to roll back; else it is destroyed."""
    _clear_current_team(ex, cfg)
    if params["keep"]:
        snapshots.stop(ex, snapshots.pool_vms(ex, "sandbox"))
        return {"destroyed": False, "kept": True}
    _destroy_sandbox(ex, cfg)
    return {"destroyed": True}


def _fetch_artifact(ex, cfg, artifact):
    """Read the artifact from the dashboard's media directory over SSH and
    verify its content hash before it is used. A mismatch fails the job."""
    media_root = cfg["dashboard_media_root"].rstrip("/")
    path = f"{media_root}/{artifact['storage_key']}"
    # The key is validated against a strict pattern (no traversal) and the
    # remote command is quoted (Executor.remote_command). base64 keeps the
    # binary intact over the text channel; sudo, because the media directory
    # belongs to the dashboard's service user (0750), not the login user.
    encoded = ex.ssh(cfg["dashboard_host"], ["sudo", "base64", "--", path], timeout=120)
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
    hosts = _ansible_hosts("devbox", ex.tofu_output("devbox"))
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


def _devbox_addresses(teams):
    """{VM name: its addresses on the team's own devbox networks}, for the dashboard to show.
    Each VM's first address is on its link towards A. They come from what generated the stack,
    and leave out the SSH fabric and maintenance addresses, which a team cannot use from a VM."""
    addresses = {}
    for team in teams:
        plan = team_addressing(team["slug"], team["index"])
        for role, cidrs in (("a", ["a_cidr"]), ("b", ["b_ab_cidr", "b_bc_cidr"]), ("c", ["c_cidr"])):
            addresses[f"devbox-{team['slug']}-{role}"] = [plan[cidr].split("/")[0] for cidr in cidrs]
    return addresses


def _write_devbox_token(ex, maintenance_ip):
    """Generate a token, write it into a devbox VM, return its SHA-256."""
    secret = f"FLAG{{{secrets.token_hex(16)}}}"
    ex.ssh(maintenance_ip, ["sudo", "install", "-D", "-m", "0444", "/dev/stdin", FLAG_PATH], input=secret)
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
    _push_devbox_map(ex, cfg, body)
    # Kept on the node too: a rollback of scoring and logging takes the map with it
    # (_restore_devbox_map), and the database is not the agent's to read.
    state = Path(cfg.get("state_dir", STATE_DIR))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    copy = state / "devbox-teams"
    with open(os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        fh.write(body)


def _push_devbox_map(ex, cfg, body):
    for host in (cfg.get("scoring_host"), cfg.get("logging_host")):
        if not host:
            raise validation.Invalid("scoring_host/logging_host not configured for the round agent")
        ex.ssh(host, ["sudo", "install", "-m", "0640", "-o", "root", "-g", "www-data", "/dev/stdin", DEVBOX_TEAMS_FILE], input=body)


def _restore_devbox_map(ex, cfg):
    """After scoring and logging are rolled back: their snapshot is older than the map, so
    devbox submissions would go unattributed until the next devbox job wrote it again."""
    copy = Path(cfg.get("state_dir", STATE_DIR)) / "devbox-teams"
    if copy.exists():
        _push_devbox_map(ex, cfg, copy.read_text())


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
    before = {(vm["vmid"], vm["name"]) for vm in snapshots.pool_vms(ex, "devbox")}
    ex.worms("devbox-apply", timeout=cfg.get("apply_timeout", 5400))
    # Only the VMs this apply created: one that was there before may have had its team on it.
    _snapshot_fresh_devbox_vms(ex, [vm for vm in _team_vms(ex) if (vm["vmid"], vm["name"]) not in before])
    _configure_devbox(ex, teams)
    report, sources = _devbox_report(ex, teams)
    _write_devbox_map(ex, cfg, teams)
    return {"vms": report, "services_sources": sources, "addresses": _devbox_addresses(teams)}


def devbox_deploy(ex, cfg, params):
    """Put a team's dev upload on its VM A and arm the fuse, as `deploy` does for a sandbox. The
    rebuild before it wrote the tokens, and scoring and logging know a devbox by its gateway's
    address, so there are no secrets to release and no current team to set. A devbox is not torn
    down when its time is up, so systemd stops main.sh instead."""
    start = params["start_vm"]
    vm = _devbox_vms(ex).get(start)
    if vm is None or vm["team"] != params["team_slug"]:
        raise validation.Invalid(f"{start} is not a VM of team {params['team_slug']}")
    if not _has_team_vm_cpu(ex, vm["vmid"]):
        raise validation.Invalid(f"{start} does not have the CPU a team's VM must have: {TEAM_VM_CPU}")
    _place_worm(ex, cfg, vm["maintenance_ip"], params["artifact"])
    delay = params["worm_delay"]
    armed_at = _arm_worm(ex, vm["maintenance_ip"], delay, f"--property=RuntimeMaxSec={params['run_duration']}")
    return {"start_vm": start, "worm_delay": delay, "team_uuid": params["team_uuid"], "armed_at": armed_at}


def _devbox_replace_args(slug):
    """`-replace` for a team's three VMs. No shell runs these, so the quotes must
    reach OpenTofu as they are: escaped ones are an invalid address."""
    return [f'-replace=module.devbox_vm["devbox-{slug}-{role}"].proxmox_virtual_environment_vm.vm' for role in ("a", "b", "c")]


def _team_vms(ex, slug=None):
    """The teams' devbox VMs (A, B, C; not the gateways) in the devbox pool, or one team's."""
    names = {name for name, info in _devbox_vms(ex).items() if slug is None or info["team"] == slug}
    return [vm for vm in snapshots.pool_vms(ex, "devbox") if vm["name"] in names]


def _snapshot_fresh_devbox_vms(ex, vms):
    """Snapshot devbox VMs OpenTofu has just created, once their first boot is done: before the
    configuration puts the team's keys on them, so no team has been on them. A reset rolls
    back to this, and the configuration and fresh tokens follow."""
    if not vms:
        return
    ips = {info["vmid"]: info["maintenance_ip"] for info in _devbox_vms(ex).values()}
    hosts = [ips[vm["vmid"]] for vm in vms]
    snapshots.wait_for_first_boot(ex, hosts)
    snapshots.take(ex, vms, "worms devbox: first boot, before any team key")
    snapshots.wait_for_ssh(ex, hosts)


def devbox_rebuild(ex, cfg, params):
    """Give one team a fresh devbox and new tokens, without touching other teams. The team's
    version has already been bumped by the orchestrator (which clears the dashboard score). Its
    three VMs go back to the snapshot taken before the team was ever on them; VMs without one
    (built before snapshots) are replaced instead, and snapshotted for the next time."""
    teams = params["teams"]
    slug = next((t["slug"] for t in teams if t["team_uuid"] == params["team_uuid"]), None)
    if slug is None:
        raise validation.Invalid("team_uuid is not in the teams list")

    ex.generate_devbox(teams, ex.repo_root / DEVBOX_GENERATED)
    vms = _team_vms(ex, slug)
    if len(vms) == len(DEVBOX_ROLES) and all(snapshots.has_clean(ex, vm) for vm in vms):
        _refuse_while_internet(cfg, "start a devbox")
        snapshots.roll_back(ex, vms)
        ips = {info["vmid"]: info["maintenance_ip"] for info in _devbox_vms(ex).values()}
        snapshots.wait_for_ssh(ex, [ips[vm["vmid"]] for vm in vms])
        how = "rolled back to its snapshot"
    else:
        # Replace just this team's three VMs (fresh disks); the rest is a no-op.
        ex.tofu("devbox", "apply", "-auto-approve", "-input=false", "-parallelism=1", *_devbox_replace_args(slug), timeout=cfg.get("apply_timeout", 5400))
        ex.worms("inventory", timeout=300)
        _snapshot_fresh_devbox_vms(ex, _team_vms(ex, slug))
        how = "replaced and snapshotted"
    _configure_devbox(ex, teams)

    # Rewrite this team's tokens only.
    vms = _devbox_vms(ex)
    rebuilt = []
    for name, info in sorted(vms.items()):
        if info["team"] == slug:
            rebuilt.append({"name": name, "flag_hash": _write_devbox_token(ex, info["maintenance_ip"])})
    _write_devbox_map(ex, cfg, teams)
    return {
        "team_uuid": params["team_uuid"],
        "devbox_version": params["devbox_version"],
        "devbox": how,
        "vms": rebuilt,
        "addresses": _devbox_addresses(teams),
    }


class _Unsafe(str):
    """A string written to the Ansible varfile with the `!unsafe` tag, so
    Ansible never templates it (used for attacker-controlled SSH keys)."""


def _unsafe_yaml(data) -> str:
    """Serialise `data` to YAML, emitting `_Unsafe` strings tagged `!unsafe`.
    PyYAML quotes/escapes every scalar, so even a key that slipped past
    validation cannot break out of its string or carry an evaluated template."""
    import yaml

    class Dumper(yaml.SafeDumper):
        pass

    Dumper.add_representer(
        _Unsafe,
        lambda dumper, value: dumper.represent_scalar("!unsafe", str(value), style='"'),
    )
    return yaml.dump(data, Dumper=Dumper, default_flow_style=False, allow_unicode=True)


def devbox_reconfigure(ex, cfg, params):
    """Re-push every team's SSH keys to the devbox VMs and the gateway, and the
    source->team map, without rebuilding any VM or touching tokens. Enqueued
    when a team adds/removes a key or leaves (docs/devbox.md). Idempotent."""
    teams = params["teams"]
    # The map first: it is what a reset of scoring and logging takes, and the configuration
    # takes minutes.
    _write_devbox_map(ex, cfg, teams)
    _configure_devbox(ex, teams)
    return {"reconfigured": [t["slug"] for t in teams], "addresses": _devbox_addresses(teams)}


def _configure_devbox(ex, teams):
    """Run the devbox + SSH-gateway Ansible with the teams' keys and VM targets
    as extra vars (CA trust, team authorized_keys, confined gateway keys).

    A team's SSH key is attacker-controlled text that Ansible renders into the
    devbox and gateway authorized_keys (on the Proxmox host, as root). Every
    key string is therefore tagged `!unsafe` in the variable file, so a
    `{{...}}`/`{%...%}` in it is never evaluated as a template -- defence in
    depth behind the strict validation (validation.ssh_keys, which already
    rejects template characters). See docs/security-assessment.md."""
    import tempfile

    devbox = ex.tofu_output("devbox").get("devbox", {}) or {}
    team_keys = {t["slug"]: [_Unsafe(k) for k in t["authorized_keys"]] for t in teams}
    gateway_keys = []
    for t in teams:
        vms = (devbox.get(t["slug"], {}) or {}).get("vms", [])
        # The gateway forwards each team to its ENTRY VM (A) only; the worm
        # pivots A->B->C inside the team's own devbox network, as in the sandbox.
        # permitopen is thus a single host, so a team key cannot open a forward
        # to B/C directly (docs/devbox.md; docs/security-assessment.md).
        entry = f"devbox-{t['slug']}-a"
        targets = [f"{vm['ssh_fabric_ip']}:22" for vm in vms if vm["name"] == entry]
        if not targets:
            # No VM A (yet): no line at all. Without permitopen the key could forward
            # anywhere the gateway reaches (sshd has AllowTcpForwarding local).
            continue
        for key in t["authorized_keys"]:
            gateway_keys.append({"public_key": _Unsafe(key), "comment": f"team={t['slug']}", "permitopen": targets})
    extra = {"devbox_team_keys": team_keys, "ssh_gateway_authorized_keys": gateway_keys}
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        fh.write(_unsafe_yaml(extra))
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
    "devbox_reconfigure": devbox_reconfigure,
    "devbox_deploy": devbox_deploy,
}
