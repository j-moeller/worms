"""The fixed menu of privileged operations the round agent runs on the host.

One function per host-job kind. Each takes the executor, the agent config and
the already-validated parameters, and returns the JSON result the conductor
reads. They contain no string taken from the database: every command is built
from fixed literals and validated values.

See docs/05-live-environment.md for how these fit the round lifecycle.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from gameplan import build as gameplan_build
from gameplan import loader as gameplan_loader
from gameplan import render as gameplan_render
from gameplan import runtime as gameplan_runtime
from gameplan.definition import AGENT_SOCKET, SECRET_MARKER, GameplanError
from generate_devbox import team_addressing
from round_agent import devbox_map, failure, host_state, probe, snapshots, startup, validation

FLAG_PATH = "/etc/worms/flag"
# The gameplan files, in the agent's workspace (synced from the controller's checkout).
GAMEPLANS = "gameplans"
# Root's home: the worm starts as root on the entry VM (and a devbox's VM A).
WORM_DIR = "/root"
WORM_ZIP = "user-worm.zip"
# The zip is unpacked here; the worm is `bash main.sh` run in this
# directory (docs/08-user-guide.md).
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
DEVBOX_GENERATED = "opentofu/stacks/devbox/generated_devboxes.tf"
DEVBOX_ROLES = ("a", "b", "c")
# While it exists, internet access is allowed (ansible Internet.yml).
INTERNET_FLAG = "/run/worms/internet-allowed"
# The VMs a sandbox reaches, which a new round's sandbox is built on after a rollback; their
# snapshot is `worms apply`'s (src/worms_cli/pipeline.py).
SERVICE_POOLS = ("ephemeral", "private_internet")
# The parts of the private internet a round cannot run without: the build installs from apt-cache,
# and the check requires DNS, apt and PyPI (devpi) to answer. Any other may be off, the operator
# accepting it at the preflight (worms_cli/rounds.py); the ephemeral ones (scoring, logging,
# monitor) never.
ROUND_NEEDS = ("dns", "apt-cache", "devpi")
# The agent's own records on the host (root only).
STATE_DIR = "/var/lib/worms"
# Team code is assumed hostile and root on its VM. This CPU offers it no hardware virtualisation,
# so no /dev/kvm and no way to drive the host's nested-KVM code (docs/07-security.md). The
# sandbox and devbox stacks set it. These checks refuse anything else, such as a VM changed by hand.
TEAM_VM_CPU = "x86-64-v3"
# What has to run on the host for any team VM to run (check, preflight).
HOST_UNITS = ("proxmox-firewall", "worms-host-firewall", "worms-firewall-watchdog.timer", "worms-mirror")
# The scoring and logging APIs on their VMs (role sandbox_api): nginx in front of the app.
SANDBOX_API_UNITS = ("sandbox-api", "nginx")
# sandbox-gw's memory ceiling (opentofu/stacks/sandbox/main.tf, its `dedicated`).
SANDBOX_GW_MEMORY_MB = 512
# What a QEMU holds beyond its VM's ceiling (its code, device emulation, page tables): 18-38 MB
# at the peak for a 512 MB VM touched in full (worms-dev, 2026-11-22: scope peaks of 530-550 MB).
QEMU_OVERHEAD_MB = 40
# What the host keeps free beyond every ceiling of a round's sandbox (preflight; MEMORY_RESERVE_MB).
MEMORY_RESERVE_MB = 1024
# The platform's addresses (all.vars: infrastructure_ips, services_ips, ...): `worms step
# inventory` writes it into the workspace (scripts/generate_inventory.py).
INVENTORY = "ansible/inventory.generated.yml"
APT_CACHE_DIR = "/var/cache/apt-cacher-ng"  # role apt_cache, apt_cache_cache_dir
OBSERVE_WINDOW = 6 * 3600  # how far back observe reads the host's records without a `since`
# Proxmox can see a QEMU gone before systemd's line of its end can be read: observe reads the
# records once more after this (seconds) before it says it does not know why a VM stopped.
OBSERVE_REREAD = 2
NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")


def _has_team_vm_cpu(ex, vmid, config=None):
    """Read from the host, because what a hostile VM says about itself proves nothing. Anything
    but exactly this CPU fails, including added flags and a config that could not be read.
    `config`: the VM's `qm config`, if read already."""
    config = ex.run(["qm", "config", str(vmid)], check=False, timeout=60) if config is None else config
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


def _sandbox_vms(ex, hosts=None):
    """{name: {maintenance_ip, vmid, addresses}} from the sandbox stack state (its outputs, if
    read already)."""
    hosts = ex.tofu_output("sandbox") if hosts is None else hosts
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


def _resources(ex):
    """Every VM of the cluster, with its pool: [{vmid, name, node, status, pool}]. Its status is
    pvestatd's, up to 10 s old: _live_states has the present one."""
    out = ex.run(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"], timeout=60)
    return json.loads(out or "[]")


def _live_states(ex, resources):
    """{vmid: what Proxmox reports of the VM now} (status, qmpstatus: QEMU's own run state), from
    each node that holds one of `resources`: one call per node, for all of its VMs."""
    states = {}
    for node in sorted({str(vm.get("node")) for vm in resources if vm.get("node")}):
        if NODE_NAME.fullmatch(node):
            out = ex.run(["pvesh", "get", f"/nodes/{node}/qemu", "--full", "1", "--output-format", "json"], timeout=60)
            states.update({int(vm["vmid"]): vm for vm in json.loads(out or "[]")})
    return states


def _status(vm, states):
    """running, paused or stopped: the live state if there is one, else the resource list's."""
    return host_state.state(states.get(int(vm["vmid"])) or {"status": vm.get("status")})


def _qemu_up(vm, states):
    """Whether the VM has a QEMU, so holds memory (paused, or its guest halted, too)."""
    return (states.get(int(vm["vmid"])) or vm).get("status") != "stopped"


def _addressing(ex):
    """The platform's addresses (the generated inventory's all.vars), {} without it. The workspace
    sync leaves the inventory out (the agent's own stacks make it): a workspace that has none yet,
    as no sandbox has been built or destroyed there, gets it here (`worms step inventory`, which
    writes that file alone)."""
    import yaml

    if not (ex.repo_root / INVENTORY).exists():
        try:
            ex.worms("inventory", timeout=300)
        except Exception as error:  # noqa: BLE001  (the checks that need it fail, saying so)
            _note(ex, f"the inventory could not be generated: {type(error).__name__}: {error}")
    try:
        data = yaml.safe_load((ex.repo_root / INVENTORY).read_text()) or {}
        return (data.get("all") or {}).get("vars") or {}
    except (OSError, yaml.YAMLError, AttributeError):
        return {}


def _note(ex, text):
    note = getattr(ex, "note", None)
    if note is not None:
        note(text)


def _oom_priority(ex, cfg, resources=None, states=None):
    """The OOM killer's first choice is a team VM (host_state.lower_oom_priority): every sandbox and
    devbox VM with a QEMU, a paused or halted one too (it holds its memory). Its pid file decides:
    the resource list's status can be 10 s old, and right after a start says stopped. Returns the
    vmids done."""
    resources = _resources(ex) if resources is None else resources
    team = [vm for vm in resources if vm.get("pool") in ("sandbox", "devbox")]
    up = [int(vm["vmid"]) for vm in team if _qemu_up(vm, states or {})]  # a missing QEMU of these is a line in the job log
    return host_state.lower_oom_priority([int(vm["vmid"]) for vm in team], cfg.get("state_dir", STATE_DIR),
                                         lambda text: _note(ex, text), up=up)  # fmt: skip


class Refused(RuntimeError):
    """A refusal whose message names nothing of a gameplan's content (a file name, a version,
    the internet window): it may reach the database."""


def _quiet(job):
    """For the jobs that load a gameplan or apply what it defines. Their errors can quote it
    (Ansible prints a failed setup command, OpenTofu the resource it refused), and a job's
    error goes to the database, which the dashboard reads. The reason goes to the job log; the
    database gets where it failed, never what it said (failure.py: an Ansible role and its
    file:line, the VMs; an exception's type and where it was raised). The exception hidden is
    kept as `original`, for the host's own accounts (the round session's terminal, summary.log)."""

    def wrapper(ex, cfg, params):
        try:
            return job(ex, cfg, params)
        except (validation.Invalid, Refused):  # our own messages, which name no content
            raise
        except Exception as error:  # noqa: BLE001
            ex.note(f"{job.__name__} failed: {type(error).__name__}: {error}")
            quiet = RuntimeError(f"the {job.__name__} job failed: {failure.from_exception(error).public} "
                                 "(what it said: the job's log on the host)")  # fmt: skip
            quiet.original = error
            raise quiet from None

    wrapper.__name__ = wrapper.__qualname__ = job.__name__
    wrapper.__doc__ = job.__doc__
    return wrapper


def _gameplan(ex, name, sha256):
    """The gameplan the job names, from the host's own copy: the file must be exactly the one
    the orchestrator validated (sha256), and valid here too, with the host's apt cache list.
    Its problems go to the job log only: the database (which the dashboard reads) gets none of
    a gameplan's content. Returns (spec, loaded)."""
    directory = ex.repo_root / GAMEPLANS
    try:
        _, digest = gameplan_loader.read(directory, name)  # its name, presence, type and size
    except GameplanError as error:
        raise Refused(str(error)) from None
    if digest != sha256:
        raise Refused(f"{name} is not the version expected (sha256 {digest[:12]}, expected {sha256[:12]}): "
                      "deploy the same checkout to the host and the orchestrator")  # fmt: skip
    loaded = gameplan_loader.load(directory, name, sha256)
    problems = gameplan_runtime.validate(loaded.cls, packages=gameplan_build.seeded_packages(ex.repo_root))
    if problems:
        for problem in problems:
            ex.note(f"gameplan {name}: {problem}")
        raise validation.Invalid(f"the gameplan {name} is not valid on the host: the job log lists why")
    return gameplan_runtime.instantiate(loaded.cls).spec(), loaded


@_quiet
def gameplans(ex, cfg, params):
    """Every gameplan file on the host, its SHA-256 and whether it is valid here. Why one is
    not goes to the job log, not into the result."""
    directory = ex.repo_root / GAMEPLANS
    seeded = gameplan_build.seeded_packages(ex.repo_root)
    out = []
    for name in gameplan_loader.available(directory):
        try:
            loaded = gameplan_loader.load(directory, name)
            problems = gameplan_runtime.validate(loaded.cls, packages=seeded)
            digest = loaded.sha256
        except Exception as error:  # noqa: BLE001  (a gameplan that does not even load)
            problems = [f"{type(error).__name__}: {error}"]
            try:
                digest = gameplan_loader.read(directory, name)[1]
            except Exception:  # noqa: BLE001  (unreadable as well: too large, replaced, gone)
                digest = ""
        for problem in problems:
            ex.note(f"gameplan {name}: {problem}")
        out.append({"file": name, "sha256": digest, "valid": not problems})
    return {"gameplans": out}


def _refuse_while_internet(cfg, what):
    """Team VMs never run while the internet window is open (Internet.yml): the services they
    reach could then reach out. OpenTofu refuses to build them then; a rollback or a start
    goes past it, so these check it themselves."""
    if Path(cfg.get("internet_flag", INTERNET_FLAG)).exists():
        raise Refused(f"internet access is allowed ({INTERNET_FLAG}): refusing to {what}")


def _sandbox_hosts(ex):
    """Every sandbox VM's management address, its gateway's too."""
    hosts = _ansible_hosts("sandbox", ex.tofu_output("sandbox"))
    return [host["management_ip"] for host in hosts.values()]


def _reusable_sandbox(ex, hcl):
    """The VMs of the sandbox the stack holds, if it is the one `hcl` defines and every VM
    of it has its clean snapshot; else None (an apply that did not finish too: _build_sandbox
    continues it or builds anew)."""
    if ex.sandbox_definition() != hcl:
        return None
    outputs = ex.tofu_output("sandbox")
    if outputs and "ansible_hosts" not in outputs:
        return None
    built = {int(host["vm_id"]) for host in _ansible_hosts("sandbox", outputs).values()}
    pool = snapshots.pool_vms(ex, "sandbox")
    if not built or {vm["vmid"] for vm in pool} != built:
        return None
    return pool if all(snapshots.has_clean(ex, vm) for vm in pool) else None


# A sandbox build begun and not finished (state dir): written before the build changes any VM,
# marked `configured` once its configuration ran through, removed once every VM has its clean
# snapshot (the snapshot step) or the sandbox is destroyed. While it is there no worm has run on
# that sandbox (prepare refuses a sandbox without its snapshot), so the operator may have the next
# build continue on its VMs (resume_build) instead of building anew, and a configured one goes
# straight to its snapshot.
SANDBOX_BUILD = "sandbox-build.json"


def _build_record_path(cfg):
    return Path(cfg.get("state_dir", STATE_DIR)) / SANDBOX_BUILD


def unfinished_build(cfg):
    """{definition (the HCL's SHA-256), gameplan, began, run_uuid} of the build left unfinished, or None."""
    try:
        record = json.loads(_build_record_path(cfg).read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("definition") else None


def _record_build(cfg, hcl, params):
    path = _build_record_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"definition": hashlib.sha256(hcl.encode()).hexdigest(), "gameplan": params.get("gameplan"),
              "began": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_uuid": params.get("run_uuid")}  # fmt: skip
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(record, handle)


def _build_configured(cfg, devboxes=()):
    """The build's configuration ran through: what is left of it is its snapshot. `devboxes`: the
    VMIDs of the devbox VMs it shut down, which the snapshot step starts again once it is taken."""
    record = unfinished_build(cfg)
    if record is None:
        return
    fd = os.open(str(_build_record_path(cfg)), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({**record, "configured": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "devboxes": sorted(set(record.get("devboxes") or []) | set(devboxes))}, handle)  # fmt: skip


def _build_finished(cfg):
    _build_record_path(cfg).unlink(missing_ok=True)


# The sandbox's last reset to its snapshot (state dir): the step that did it (snapshot, rollback)
# and the host's clock just before it started any VM. The next prepare takes it (and removes it)
# as when its attempt began: the check's `start` then knows a VM that booted from the snapshot
# for this attempt from one left running since an earlier one.
SANDBOX_RESET = "sandbox-reset.json"


def _note_reset(cfg, how):
    path = Path(cfg.get("state_dir", STATE_DIR)) / SANDBOX_RESET
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"how": how, "began": host_state.clock()}, handle)


def _take_reset(cfg):
    """The last reset's record, removed: one reset, one prepare. None without one."""
    path = Path(cfg.get("state_dir", STATE_DIR)) / SANDBOX_RESET
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    path.unlink(missing_ok=True)
    return record if isinstance(record, dict) and isinstance(record.get("began"), dict) else None


def _destroy_sandbox(ex, cfg):
    # One at a time: VMs destroyed in parallel fight over the storage lock, and a volume
    # Proxmox gives up on then blocks whichever new VM gets that ID.
    before = snapshots.listed(ex, lambda: snapshots.pool_vms(ex, "sandbox"))  # for the VM history only
    ex.tofu("sandbox", "destroy", "-auto-approve", "-input=false", "-parallelism=1", timeout=cfg.get("apply_timeout", 5400))
    _build_finished(cfg)  # nothing left to continue
    snapshots.record(ex, lambda: [vm for vm in before if vm["vmid"] not in {v["vmid"] for v in snapshots.pool_vms(ex, "sandbox")}],
                     "destroyed", "the round's sandbox")  # fmt: skip
    ex.worms("inventory", timeout=300)


def _reset_services(ex, cfg):
    """The service VMs back to the snapshot `worms apply` took of them, all or none. Returns
    what happened, for the run's record: without a snapshot on each, they stay as they are. One
    that was off (a part of the private internet the operator turned off, accepted at the
    preflight) is rolled back too, and off again afterwards."""
    vms = [vm for pool in SERVICE_POOLS for vm in snapshots.pool_vms(ex, pool)]
    missing = sorted(vm["name"] for vm in vms if not snapshots.has_clean(ex, vm))
    if not vms or missing:
        return f"not reset: no clean snapshot on {', '.join(missing) or 'any service VM'} (`worms vm rebuild services`)"
    off = {vm["vmid"] for vm in vms if vm.get("status") != "running"}
    snapshots.roll_back(ex, vms)
    snapshots.wait_for_ssh(ex, cfg.get("service_hosts") or [cfg["scoring_host"], cfg["logging_host"]])
    _push_devbox_map(ex, cfg)
    if not off:
        return f"reset: {len(vms)} VMs back to their clean snapshot"
    again = [vm for pool in SERVICE_POOLS for vm in snapshots.pool_vms(ex, pool) if vm["vmid"] in off]
    snapshots.shut_down(ex, again)
    return f"reset: {len(vms)} VMs back to their clean snapshot; off again, as they were: {', '.join(vm['name'] for vm in again)}"


def _unsafe_tree(value):
    """Every string in `value` tagged `!unsafe` for Ansible (_unsafe_yaml): a gameplan's files
    and commands are written as they are, never templated."""
    if isinstance(value, dict):
        return {key: _unsafe_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unsafe_tree(item) for item in value]
    return _Unsafe(value) if isinstance(value, str) else value


def _build_sandbox(ex, cfg, hcl, config, params, resume=False):
    """A new sandbox: OpenTofu's apply and the configuration, its snapshot still to take (the
    snapshot step) before any team's code reaches it. While it is built the devbox VMs are shut
    down and the service VMs it installs from are back to their snapshot, so nothing in it comes
    from team code: every run of the round starts from its snapshot. `resume` (the operator's
    choice, `worms round start --resume`): a build of this same definition left unfinished
    (unfinished_build) is continued on its VMs, OpenTofu's apply and the configuration run
    again; any other sandbox is destroyed first."""
    _refuse_while_internet(cfg, "build a sandbox")
    unfinished = unfinished_build(cfg)
    continued = (resume and unfinished is not None and ex.sandbox_definition() == hcl
                 and unfinished["definition"] == hashlib.sha256(hcl.encode()).hexdigest())  # fmt: skip
    if continued:
        ex.note(f"continuing the sandbox build begun {unfinished['began']}: its VMs kept, OpenTofu's apply and "
                "the configuration run again")  # fmt: skip
    else:
        if resume:
            ex.note("no unfinished build of this gameplan's sandbox to continue: it is built anew")
        if ex.tofu_output("sandbox") or unfinished is not None:
            _destroy_sandbox(ex, cfg)  # another round's, one a worm ran on, or a build not to continue
    devboxes = snapshots.shut_down(ex, snapshots.pool_vms(ex, "devbox"))
    try:
        services = _reset_services(ex, cfg)
        ex.install_sandbox_definition(hcl)
        if not continued:
            _record_build(cfg, hcl, params)
        ex.tofu("sandbox", "init", "-input=false", timeout=600)
        ex.tofu("sandbox", "apply", "-auto-approve", "-input=false", "-parallelism=1", timeout=cfg.get("apply_timeout", 5400))
        if continued:
            # Kept powered off (the failed session's end): up again for the configuration.
            snapshots.start(ex, [vm for vm in snapshots.pool_vms(ex, "sandbox") if vm.get("status") != "running"])
        snapshots.record(ex, lambda: snapshots.pool_vms(ex, "sandbox"), "created", "the round's sandbox")
        # Refresh the inventory and configure the sandbox: the internal CA (a worm reaches scoring
        # and logging over verified HTTPS) and what the gameplan installs on each VM.
        with tempfile.TemporaryDirectory() as tmp:
            varfile = Path(tmp) / "sandbox-vms.yml"
            varfile.write_text(_unsafe_yaml(_unsafe_tree(config)))
            ex.worms("round-configure-sandbox", timeout=cfg.get("configure_timeout", 1800), args=f"-e @{varfile}")
        # The devboxes it shut down stay off until the snapshot is taken (the snapshot step starts them).
        _build_configured(cfg, [int(vm["vmid"]) for vm in devboxes])
    except BaseException:
        if devboxes:
            _refuse_while_internet(cfg, "start the devbox VMs again")
            snapshots.start(ex, devboxes)
        raise
    return services


def _gameplans_sandbox(ex, params):
    """(spec, loaded, hcl): the gameplan the orchestrator validated, and the sandbox it defines."""
    spec, loaded = _gameplan(ex, params["gameplan"], params["gameplan_sha256"])
    return spec, loaded, gameplan_build.hcl(spec, loaded.filename, loaded.sha256)


@_quiet
def build(ex, cfg, params):
    """build(): the round's sandbox from its gameplan, once per round, before any team's attempt:
    OpenTofu's apply and the configuration (Ansible: the CA, what the gameplan installs). Its
    snapshot is the next step's (snapshot). Not built again: the sandbox of this gameplan, every
    VM with its clean snapshot (a later session of the round rolls it back instead), and a build
    configured already, whose snapshot failed (the snapshot step is left). A build left
    unfinished is continued on its VMs when the operator says so (resume_build: OpenTofu's apply
    and the configuration again), else destroyed and built anew."""
    spec, _loaded, hcl = _gameplans_sandbox(ex, params)
    if _reusable_sandbox(ex, hcl):
        return {"built": False, "snapshot_due": False,
                "sandbox": "kept: this gameplan's sandbox is there, every VM with its clean snapshot (rolled back for each team)"}  # fmt: skip
    record = unfinished_build(cfg)
    if (params.get("resume_build") and record and record.get("configured") and ex.sandbox_definition() == hcl
            and record["definition"] == hashlib.sha256(hcl.encode()).hexdigest()):  # fmt: skip
        ex.note(f"the build begun {record['began']} was configured {record['configured']}: only its snapshot is left")
        return {"built": False, "snapshot_due": True, "sandbox": "configured already: its snapshot is left"}
    services = _build_sandbox(ex, cfg, hcl, gameplan_build.ansible_vars(spec), params, resume=params.get("resume_build", False))
    return {"built": True, "snapshot_due": True, "sandbox": "built and configured", "services": services}


def snapshot(ex, cfg, params):
    """snapshot(): every VM of the round's sandbox, its gateway too, its clean snapshot, taken
    once its build is configured and before any team's code reaches it (each VM shut down
    first: no guest agent, so a running one would be snapshotted like a power cut), then started
    again: the first team's attempt begins on it as it is. The build is finished then."""
    record = unfinished_build(cfg)
    vms = snapshots.pool_vms(ex, "sandbox")
    if not vms:
        raise RuntimeError("no sandbox VM to snapshot: the build step comes first")
    if record is None:
        raise RuntimeError("no build of the sandbox is on record: a team's code may have run on it since its build. "
                           "`worms round teardown` destroys it; the next `worms round start` builds it again")  # fmt: skip
    if not record.get("configured"):
        raise RuntimeError(f"the sandbox's build (begun {record['began']}) did not finish its configuration: the build step comes first")
    definition = hashlib.sha256(ex.sandbox_definition().encode()).hexdigest()
    if record["definition"] != definition:
        raise RuntimeError("the sandbox the stack holds is not the one on record as built")
    _refuse_while_internet(cfg, "start the sandbox after its snapshot")
    _note_reset(cfg, "snapshot")  # every VM boots from here on: the first attempt's prepare counts from it
    snapshots.take(ex, vms, f"worms round sandbox {definition[:12]}")
    _build_finished(cfg)
    # The devbox VMs the build shut down, up again now that nothing of theirs can reach the snapshot.
    off, started = set(record.get("devboxes") or []), []
    for vm in snapshots.pool_vms(ex, "devbox"):
        if int(vm["vmid"]) in off:
            try:
                snapshots.start(ex, [vm])
                started.append(vm["name"])
            except Exception as exc:  # noqa: BLE001  (started meanwhile, say: the snapshot stands)
                ex.note(f"{vm['name']} not started again: {exc}")
    return {"taken": [vm["name"] for vm in vms], "devboxes_started": started}


@_quiet
def rollback(ex, cfg, params):
    """load_snapshot(): every VM of the round's sandbox back to its clean snapshot, started, so that
    nothing of the team before is left (a boot instead of a build). For every attempt but the
    first after the snapshot was taken."""
    _spec, _loaded, hcl = _gameplans_sandbox(ex, params)
    built = _reusable_sandbox(ex, hcl)
    if not built:
        raise RuntimeError("no sandbox of this gameplan with every VM's clean snapshot to roll back to: the build and "
                           "snapshot steps come first")  # fmt: skip
    _refuse_while_internet(cfg, "start a sandbox")
    _note_reset(cfg, "rollback")  # every VM boots from here on: the attempt's prepare counts from it
    snapshots.roll_back(ex, built)
    snapshots.wait_for_ssh(ex, _sandbox_hosts(ex))
    return {"rolled_back": [vm["name"] for vm in built]}


def delete_snapshot(ex, cfg, params):
    """delete_snapshot(): the clean snapshot of every VM of the round's sandbox deleted, before
    the sandbox is destroyed (teardown)."""
    return {"deleted": snapshots.delete(ex, snapshots.pool_vms(ex, "sandbox"))}


# -- a run's secrets and files ---------------------------------------------------------------


def _run_state_path(cfg, run_uuid):
    return Path(cfg.get("state_dir", STATE_DIR)) / "runs" / f"{run_uuid}.json"


def _save_run_state(cfg, run_uuid, state):
    """The run's gameplan and secrets, on the host only (root, 0600), for files execute()
    writes later. Only the current run's: a new prepare removes the others."""
    directory = _run_state_path(cfg, run_uuid).parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for stale in directory.glob("*.json"):
        stale.unlink()
    path = _run_state_path(cfg, run_uuid)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        json.dump(state, fh)


def _load_run_state(cfg, run_uuid):
    path = _run_state_path(cfg, run_uuid)
    if not path.is_file():
        raise validation.Invalid(f"no state for run {run_uuid} on the host (prepare did not run, or it was torn down)")
    return json.loads(path.read_text())


def _generate_secrets(ex, spec):
    """{secret id: {field: value}}, new for this run: key pairs from ssh-keygen, passwords and
    TOTP seeds from `secrets` (gameplan/render.py). They stay on the host; the database never
    sees them. Keys come last: a key's passphrase may hold the run's passwords."""
    out = {}
    keys = []
    for secret in spec["secrets"]:
        if secret["kind"] == "ssh_key":
            keys.append(secret)
        else:
            out[secret["id"]] = gameplan_render.new_secret(secret)
    for secret in keys:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            bits = ["-b", "3072"] if secret["type"] == "rsa" else []
            ex.run(["ssh-keygen", "-q", "-t", secret["type"], *bits, "-N", "", "-C", "worms-gameplan", "-f", key], timeout=120)
            private = key.read_text()
            if secret.get("passphrase"):
                private = gameplan_render.encrypt_ssh_key(private, _fill(secret["passphrase"], out))
            out[secret["id"]] = {"private": private, "public": (Path(tmp) / "key.pub").read_text().strip()}
    return out


def _fill(content, values):
    """The file's content with the run's secrets in place of their markers."""
    return SECRET_MARKER.sub(lambda m: values[m.group(1)][m.group(2)], content)


def _home(user):
    return "/root" if user == "root" else f"/home/{user}"


def _ssh_dir(ex, ip, user):
    ex.ssh(ip, ["sudo", "install", "-d", "-m", "0700", "-o", user, "-g", user, f"{_home(user)}/.ssh"])


# add_private_ssh_key (gameplan/definition.py): the user's ssh-agent holds its keys that have no
# passphrase. A service of the VM: it starts at every boot (a worm may restart the VM) and loads
# them again from their files, listed in AGENT_KEYS/<user>; a stale socket (a crash, a reboot)
# would keep it from binding. ssh, scp and sftp as the user offer the keys (IdentityAgent,
# AGENT_CLIENT), and a login shell has SSH_AUTH_SOCK (AGENT_LOGIN). `$$` is systemd's `$`.
AGENT_KEYS = "/etc/worms/ssh-agent"
AGENT_UNIT_PATH = "/etc/systemd/system/worms-ssh-agent@.service"
AGENT_UNIT = f"""[Unit]
Description=ssh-agent of %i, holding the keys the gameplan gave it (worms)

[Service]
Type=forking
User=%i
ExecStart=/bin/sh -c 'rm -f "$$HOME/.ssh/{AGENT_SOCKET}"; exec /usr/bin/ssh-agent -a "$$HOME/.ssh/{AGENT_SOCKET}" >/dev/null'
ExecStartPost=-/bin/sh -c 'SSH_AUTH_SOCK="$$HOME/.ssh/{AGENT_SOCKET}" xargs -r ssh-add -q < {AGENT_KEYS}/%i'
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""
AGENT_CLIENT = "/etc/ssh/ssh_config.d/worms-agent.conf"
AGENT_LOGIN = "/etc/profile.d/worms-agent.sh"
AGENT_LOGIN_TEXT = f"""# worms: a login shell of a user with an ssh-agent (a gameplan's add_private_ssh_key) has it
if [ -z "$SSH_AUTH_SOCK" ] && [ -S "$HOME/.ssh/{AGENT_SOCKET}" ]; then export SSH_AUTH_SOCK="$HOME/.ssh/{AGENT_SOCKET}"; fi
"""


def _agent_script(agents):
    """The shell script that gives each user of `agents` ({user: [key paths]}) its ssh-agent,
    then checks that the agent holds all of them. Users and key names are the gameplan's,
    checked (USER, KEY_NAME); the paths are quoted all the same."""
    client = "".join(f"Match localuser {user}\n    IdentityAgent {_home(user)}/.ssh/{AGENT_SOCKET}\n" for user in agents)
    lines = ["set -e", f"install -d -m 0755 {AGENT_KEYS}"]
    for path, text in ((AGENT_UNIT_PATH, AGENT_UNIT), (AGENT_CLIENT, client), (AGENT_LOGIN, AGENT_LOGIN_TEXT)):
        lines += [f"cat > {path} <<'WORMS_EOF'", text.rstrip("\n"), "WORMS_EOF"]
    for user, paths in agents.items():
        lines.append(f"printf '%s\\n' {shlex.join(paths)} > {AGENT_KEYS}/{user}")
    lines.append("systemctl daemon-reload")
    for user, paths in agents.items():
        socket = f"{_home(user)}/.ssh/{AGENT_SOCKET}"
        lines += [
            f"systemctl enable --quiet worms-ssh-agent@{user}",
            f"systemctl restart worms-ssh-agent@{user}",
            f"held=$(runuser -u {user} -- env SSH_AUTH_SOCK={socket} ssh-add -l 2>/dev/null | grep -c SHA256: || true)",
            f'[ "$held" -eq {len(paths)} ] || {{ echo "the ssh-agent of {user} holds $held of its {len(paths)} keys" >&2; exit 1; }}',
        ]
    return "\n".join(lines) + "\n"


def _apply_setup(ex, vms, spec, values):
    """Before the worm: every key and password of the run, as the gameplan placed them; then the
    ssh-agents of the users given private keys."""
    keys = {s["id"]: s for s in spec["secrets"]}
    agents = {}  # VM name -> {user: [its keys without a passphrase]}
    for step in spec["setup"]:
        ip = vms[step["vm"]]["maintenance_ip"]
        secret = values[step["secret"]]
        if step["op"] == "password":
            user = keys[step["secret"]]["user"]
            ex.ssh(ip, ["sudo", "chpasswd"], input=f"{user}:{secret['password']}\n")
        elif step["op"] == "private_key":
            user = step["user"]
            _ssh_dir(ex, ip, user)
            path = f"{_home(user)}/.ssh/{step['name']}"
            ex.ssh(ip, ["sudo", "install", "-m", "0600", "-o", user, "-g", user, "/dev/stdin", path], input=secret["private"])
            if not keys[step["secret"]].get("passphrase"):  # an agent would hold it unlocked
                held = agents.setdefault(step["vm"], {}).setdefault(user, [])
                if path not in held:
                    held.append(path)
        elif step["op"] == "public_key":
            user = step["user"]
            _ssh_dir(ex, ip, user)
            path = f"{_home(user)}/.ssh/authorized_keys"
            ex.ssh(ip, ["sudo", "tee", "-a", path], input=secret["public"] + "\n")
            ex.ssh(ip, ["sudo", "chown", f"{user}:{user}", path])
            ex.ssh(ip, ["sudo", "chmod", "0600", path])
        else:
            raise validation.Invalid(f"unknown setup step {step['op']!r}")
    for name, users in agents.items():
        ex.ssh(vms[name]["maintenance_ip"], ["sudo", "sh", "-s"], input=_agent_script(users))


def _write_gameplan_file(ex, ip, entry, values):
    """Text as it is (with the run's secrets); a document, archive or vault made here first
    (gameplan/render.py) and sent base64-encoded, as binary does not survive the text channel."""
    install = ["install", "-D", "-m", entry["mode"], "-o", entry["owner"], "-g", entry["owner"], "/dev/stdin", entry["path"]]
    if isinstance(entry["content"], str):
        ex.ssh(ip, ["sudo", *install], input=_fill(entry["content"], values))
        return
    data = gameplan_render.render(entry["content"], values)
    ex.ssh(ip, ["sudo", "sh", "-c", "base64 -d | " + shlex.join(install)], input=base64.b64encode(data).decode("ascii"))


# What read_file runs on a VM: the file's size and SHA-256, or that it is not a regular file.
# The path is the script's argument, never part of it.
READ_FILE = 'if [ -f "$1" ] && [ ! -L "$1" ]; then stat -c %s -- "$1" && sha256sum < "$1"; else echo missing; fi'


def _guest_step(strict, action):
    """Run `action` on a VM. strict (before the worm): a failure fails the job. Otherwise the
    worm may have caused it (it is root on the VM): the reason is returned instead."""
    if strict:
        action()
        return None
    try:
        action()
        return None
    except Exception as error:  # noqa: BLE001  (whatever the VM did to us)
        return f"{type(error).__name__}: {str(error)[:300]}"


def _place_flags(ex, vms, names, strict):
    """A fresh flag on each VM, read back to check it is there. Returns (released, failed)."""
    released, failed = [], []
    for name in names:
        info = vms.get(name)
        if info is None:
            raise validation.Invalid(f"{name} is not a VM of the current sandbox")
        secret = f"FLAG{{{secrets.token_hex(16)}}}"
        digest = hashlib.sha256(secret.encode()).hexdigest()

        def place(ip=info["maintenance_ip"], secret=secret, digest=digest):
            # -D: /etc/worms does not exist in a fresh VM.
            ex.ssh(ip, ["sudo", "install", "-D", "-m", "0444", "/dev/stdin", FLAG_PATH], input=secret)
            written = ex.ssh(ip, ["sudo", "sha256sum", FLAG_PATH]).split()[:1]
            if written != [digest]:
                raise RuntimeError(f"the flag on {name} reads back differently")

        problem = _guest_step(strict, place)
        if problem:
            failed.append({"vm": name, "error": problem})
        else:
            released.append({"vm": name, "flag_hash": digest})
    return released, failed


def _remove_flags(ex, vms, names, strict):
    removed, failed = [], []
    for name in names:
        info = vms.get(name)
        if info is None:
            raise validation.Invalid(f"{name} is not a VM of the current sandbox")
        problem = _guest_step(strict, lambda ip=info["maintenance_ip"]: ex.ssh(
            ip, ["sudo", "sh", "-c", f"rm -f {FLAG_PATH} && test ! -e {FLAG_PATH}"]))  # fmt: skip
        if problem:
            failed.append({"vm": name, "error": problem})
        else:
            removed.append(name)
    return removed, failed


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------


@_quiet
def prepare(ex, cfg, params):
    """prepare(): everything of a team's run that comes before the worm, on the round's sandbox
    as its snapshot holds it (the build and snapshot steps made it, once; every attempt but the
    first after them rolled it back: the rollback step). This run's keys, passwords and files,
    and a fresh flag on every VM. It builds nothing and rolls nothing back: a sandbox that is not
    this gameplan's, has no snapshot, or has a VM not running is refused, and so is one not reset
    since the last prepare. When the attempt began is when its reset did (the kernel's clock,
    before the snapshot or the rollback started a VM: _note_reset), kept with the run: the check
    takes a VM whose QEMU is older for one that did not boot from the snapshot for this attempt."""
    reset = _take_reset(cfg)
    spec, loaded, hcl = _gameplans_sandbox(ex, params)
    built = _reusable_sandbox(ex, hcl)
    if not built:
        raise RuntimeError("no sandbox of this gameplan with every VM's clean snapshot: the build and snapshot steps come first")
    if reset is None:
        raise RuntimeError("the sandbox was not reset since the last attempt's prepare: the rollback step (to the snapshot) comes first")
    began = reset["began"]
    down = sorted(vm["name"] for vm in built if vm.get("status") != "running")
    if down:
        raise RuntimeError(f"sandbox VMs not running: {', '.join(down)}: the rollback step (to the snapshot) comes first")
    snapshots.wait_for_ssh(ex, _sandbox_hosts(ex))

    vms = _sandbox_vms(ex)
    missing = sorted({vm["name"] for vm in spec["vms"]} - set(vms))
    if missing:
        raise RuntimeError(f"the sandbox lacks VMs of the gameplan: {', '.join(missing)}")
    values = _generate_secrets(ex, spec)
    _apply_setup(ex, vms, spec, values)
    _install_capture_vm(ex, vms)  # the gameplan's VMs (not the gateway)
    for entry in spec["files"]:
        if entry["at_reset"]:
            _write_gameplan_file(ex, vms[entry["vm"]]["maintenance_ip"], entry, values)
    released, _ = _place_flags(ex, vms, [vm["name"] for vm in spec["vms"]], strict=True)
    _save_run_state(cfg, params["run_uuid"], {"gameplan": loaded.filename, "sha256": loaded.sha256, "secrets": values, "began": began})
    _oom_priority(ex, cfg)  # every VM is up now, each with a QEMU of its own

    networks = gameplan_build.vm_networks(spec)
    gateway = _ansible_hosts("sandbox", ex.tofu_output("sandbox")).get("sandbox_gw")
    return {
        "sandbox": "as its snapshot holds it",
        "topology": gameplan_build.topology(spec),
        "entrypoint": spec["entrypoint"],
        "gateway": {"name": gateway["name"], "vmid": int(gateway["vm_id"])} if gateway else None,
        "released": released,
        "vms": [
            {
                "name": name,
                "maintenance_ip": vms[name]["maintenance_ip"],
                "vmid": vms[name]["vmid"],
                "addresses": vms[name]["addresses"],
                "networks": networks.get(name, []),
            }
            for name in sorted(networks)
        ],
    }


def _recorder(results):
    def record(name, ok, detail=""):
        results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:200]})

    return record


def _host_units(ex, record):
    for unit in HOST_UNITS:
        out = ex.run(["systemctl", "is-active", unit], check=False).strip()
        record(f"host:{unit}", out == "active", out)


def _devbox_address(ex, addressing):
    """A devbox gateway's address on the services bridge, the one bridge sandbox and devbox share:
    the devbox stack's, else the first of the block kept for them."""
    try:
        devbox = ex.tofu_output("devbox").get("devbox") or {}
        found = sorted(str(d["services_source_ip"]) for d in devbox.values() if isinstance(d, dict) and d.get("services_source_ip"))
        if found:
            return found[0]
    except Exception:  # noqa: BLE001  (no devbox stack: the block's address will do)
        pass
    try:
        return str(next(ipaddress.ip_network(addressing["devbox_services_block"]).hosts()))
    except (KeyError, TypeError, ValueError, StopIteration):
        return None


def _probe_plans(ex, spec, addressing, vms):
    """{VM name: (probe parameters, the host's notes)} for every VM of the run's gameplan. As for
    the jobs that load a gameplan (_quiet): an error here may quote it, so it goes to the job log."""
    maintenance = {name: info["maintenance_ip"] for name, info in vms.items()}
    devbox = _devbox_address(ex, addressing)
    try:
        return {vm["name"]: probe.plan(spec, vm["name"], addressing, maintenance, devbox) for vm in spec["vms"]}
    except Exception as error:  # noqa: BLE001
        _note(ex, f"check: the probes of the run's gameplan: {type(error).__name__}: {error}")
        raise RuntimeError("the check job could not plan its probes: its log on the host says why") from None


def _started(name, vmid, resources, states, run, now):
    """Whether a VM of the sandbox started as it should, as the host alone sees it: (ok, detail).
    It is in the pool sandbox under its name; Proxmox says it runs, and so does QEMU's own run
    state (not paused, not halted). With the run, its QEMU also started after this attempt's
    sandbox was reset for this attempt (the kernel's clock, which the snapshot or the rollback noted
    and prepare kept with the run): a VM left running from before the
    rollback would hold the last run's state. `resources`: {vmid: the cluster's entry}; `states`:
    _live_states; `now`: host_state.clock()."""
    vm = resources.get(int(vmid))
    if vm is None:
        return False, f"Proxmox has no VM {vmid}"
    if vm.get("pool") != "sandbox" or vm.get("name") != name:
        return False, f"VM {vmid} is {vm.get('name')} in the pool {vm.get('pool') or '(none)'}, not {name} in sandbox"
    live = states.get(int(vmid)) or {"status": vm.get("status")}
    if live.get("status") != "running":
        return False, str(live.get("status") or "not running")
    if live.get("qmpstatus") != "running":
        what = "halted" if live.get("qmpstatus") in host_state.HALTED else "not running"
        return False, f"Proxmox says running, but its QEMU is {live.get('qmpstatus') or 'in no state it says'} ({what})"
    if run is None:
        return True, "running"
    began, started = run.get("began") or {}, host_state.qemu_started(vmid)
    if started is None:
        return False, "Proxmox says running, but the host finds no QEMU process of it"
    if began.get("uptime") is None:
        return False, "running, but its attempt has no note of its reset: whether it booted in this attempt is not known"
    if began.get("boot_id") and now.get("boot_id") and began["boot_id"] != now["boot_id"]:
        return True, "running, booted in this attempt (the host itself restarted after the sandbox's reset)"
    if started < began["uptime"]:
        return False, (f"running since {began['uptime'] - started:.0f} s before this attempt's reset (the rollback, or the snapshot): it did not boot "
                       "in this attempt and may hold the last run's state")  # fmt: skip
    age = f", {now['uptime'] - started:.0f} s ago" if now.get("uptime") is not None else ""
    return True, f"running, booted in this attempt (its QEMU started {started - began['uptime']:.0f} s after the sandbox's reset{age})"


def _gateway_snippet(ex, config):
    """The cloud-init sandbox-gw was built with (startup.gateway_template reads it): the snippet its
    Proxmox config names, which the sandbox stack rendered from sandbox-gw.tftpl in the apply that
    built it. Only that apply writes it (_build_sandbox, on a destroyed stack), and the snapshot is
    taken after it. Not the template in the workspace: a deploy during a round replaces that, and the
    round's gateway, rolled back to its snapshot, keeps what it was built with. `config`: `qm config`."""
    volume = startup.snippet(config)
    if not volume:
        raise RuntimeError("its Proxmox config names no cloud-init snippet (cicustom user=)")
    path = ex.run(["pvesm", "path", volume], timeout=60).strip()
    try:
        return Path(path).read_text()
    except (OSError, ValueError) as error:
        raise RuntimeError(f"its cloud-init snippet ({volume}, {path or 'no path'}) could not be read: {error}") from None


def _init_plan(ex, name, info, config, specs, stack, addressing):
    """startup.INIT's parameters and the host's notes for a VM of the sandbox or for sandbox-gw."""
    macs = startup.nic_macs(config)
    if not info.get("gateway"):
        return startup.plan_vm(specs[name], macs)
    gateway = (stack.get("sandbox") or {}).get("gateway")
    if not gateway:
        raise RuntimeError("the sandbox stack's outputs have no sandbox.gateway")
    init, notes = startup.plan_gateway(gateway, macs, addressing, _gateway_snippet(ex, config))
    if not init["chains"] or "net.ipv4.ip_forward" not in notes["sysctl"]:
        raise RuntimeError("its cloud-init snippet defines no nftables chain or does not turn IP forwarding on")
    return init, notes


# The checks that decide whether an attempt starts (the operator's choice, 2026-11-24): every VM
# running and booted in this attempt (start), the probe that answered, the flag on it, DNS,
# scoring, logging, apt and PyPI reachable; and the isolation the platform's rules demand: the
# host's firewalls up, no team VM with the host's CPU, nothing forbidden (the internet, the
# infrastructure) reachable from a VM. Everything else (how it booted, the gameplan's own
# connections and files, the mirror) is a warning.
GATES = re.compile(r"^(?:host:(?:proxmox-firewall|worms-host-firewall)|sandbox:vms-registered"
                   r"|vm:[^:]+:(?:start|no-kvm|probe|flag|dns|scoring|logging|apt|pypi|isolated))$")  # fmt: skip


def check(ex, cfg, params):
    """Sanity checks on the host and the sandbox, right before the worm. Returns {ok, failures,
    checks}. The host's units; for every VM of the sandbox and for sandbox-gw, that it started
    (startup.py: in the pool, running, its QEMU too) and, for each but the gateway, its CPU. With
    the run (`run_uuid`), all at once, while no team code has reached them: that each one booted
    in this attempt and came up as it should, from inside (cloud-init done, the boot finished, its
    addresses, route, sshd, users and services; the gateway's rules and sysctl settings), and then
    the probes (probe.py) of every VM of its gameplan: its flag, the platform's names, scoring and
    logging over verified HTTPS, apt and PyPI through the private internet, the infrastructure and
    the internet out of its reach, the gameplan's firewall both ways, its files. A VM that did not
    start, or did not answer, is not probed. The OOM priority of every team VM, as after any start.

    Only the checks GATES names fail it (`failures`: the attempt does not start); the others are
    `warnings`, in the result and the job's log."""
    results = []
    record = _recorder(results)
    _host_units(ex, record)

    mirror = ex.run(["/usr/local/sbin/worms-mirror", "status"], check=False, timeout=60)
    record("host:mirror-receive-only", "drop" in mirror, "")

    stack = ex.tofu_output("sandbox")
    vms = _sandbox_vms(ex, stack)
    record("sandbox:vms-registered", bool(vms), f"{len(vms)} VM(s)")
    run = _load_run_state(cfg, params["run_uuid"]) if params.get("run_uuid") else None
    specs, addressing, plans = {}, {}, {}
    if run is not None:
        spec, _ = _gameplan(ex, run["gameplan"], run["sha256"])
        specs = {vm["name"]: vm for vm in spec["vms"]}
        addressing = _addressing(ex)
        record("host:inventory", bool(addressing), "" if addressing else
               f"no {INVENTORY} in the workspace: the probes lack the platform's addresses (`worms step inventory`)")  # fmt: skip
        plans = _probe_plans(ex, spec, addressing, vms)
    targets = dict(vms)
    gateway = _ansible_hosts("sandbox", stack).get("sandbox_gw")
    if gateway:
        targets[gateway["name"]] = {"maintenance_ip": gateway["management_ip"], "vmid": int(gateway["vm_id"]), "gateway": True}
    resources = _resources(ex)
    states = _live_states(ex, resources)
    by_vmid, now = {int(vm["vmid"]): vm for vm in resources}, host_state.clock()

    def one(name):
        info, out = targets[name], []
        config = ex.run(["qm", "config", str(info["vmid"])], check=False, timeout=60)
        started, detail = _started(name, info["vmid"], by_vmid, states, run, now)
        out.append((f"vm:{name}:start", started, detail))
        # Not the gateway's: main.tf gives it no CPU type of the team VMs' (this would always fail), and it
        # is rolled back to its snapshot before every attempt like the rest of the pool.
        if not info.get("gateway"):
            out.append((f"vm:{name}:no-kvm", _has_team_vm_cpu(ex, info["vmid"], config), f"its CPU must be {TEAM_VM_CPU}"))
        if run is None:
            return out
        if not started:  # nothing of it is asked: it may be the last run's
            out.append((f"vm:{name}:init", False, "not checked: it did not start in this attempt (start)"))
            if name in plans:
                out.append((f"vm:{name}:probe", False, "not run: it did not start in this attempt (start)"))
            return out
        reply, skipped = None, "the VM did not answer its start and initialisation check (init)"
        try:
            init, notes = _init_plan(ex, name, info, config, specs, stack, addressing)
        except Exception as error:  # noqa: BLE001  (a spec or a stack it cannot read: that VM fails, by name only)
            _note(ex, f"check {name}: its start and initialisation check: {type(error).__name__}: {error}")
            out.append((f"vm:{name}:init", False, "not checked: it could not be planned (the job log on the host says why)"))
            skipped = "its start and initialisation check could not be planned (init)"
        else:
            reply, _, why = probe.run(ex, info["maintenance_ip"], init, startup.INIT, startup.SSH_TIMEOUT)
            try:
                out += startup.records(name, init, notes, reply, lambda text: _note(ex, text), why)
            except Exception as error:  # noqa: BLE001  (an answer it cannot read: that VM fails, by name only)
                _note(ex, f"check {name}: its start and initialisation check's answer: {type(error).__name__}: {error}")
                out.append((f"vm:{name}:init", False, "an answer that could not be read (the job log on the host says why)"))
        if name in plans and reply is None:
            out.append((f"vm:{name}:probe", False, f"not run: {skipped}"))
        elif name in plans:
            params_, notes = plans[name]
            reply, sent, why = probe.run(ex, info["maintenance_ip"], params_)
            try:
                out += probe.records(name, params_, notes, reply, sent, lambda text: _note(ex, text), why)
            except Exception as error:  # noqa: BLE001  (an answer it cannot read: that VM fails, by name only)
                _note(ex, f"check {name}: its probe's answer: {type(error).__name__}: {error}")
                out.append((f"vm:{name}:probe", False, "an answer that could not be read (the job log on the host says why)"))
        return out

    # One thread per VM, the gateway too (at most 33: a gameplan has up to 32 VMs): the start and
    # initialisation check waits up to startup.WAIT for a VM's boot, the probes take up to a few
    # minutes each, and qm a second per call. None waits for another.
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        for rows in pool.map(one, sorted(targets)):
            for row in rows:
                record(*row)
    for name in sorted(set(plans) - set(vms)):
        record(f"vm:{name}:probe", False, "the gameplan's VM is not in the sandbox")
    _oom_priority(ex, cfg, resources, states)

    failures = [r["check"] for r in results if not r["ok"] and GATES.match(r["check"])]
    warnings = [r["check"] for r in results if not r["ok"] and not GATES.match(r["check"])]
    for row in results:  # the job's log says each failure and warning and why, not only the result
        if not row["ok"]:
            what = "failed" if row["check"] in failures else "warning (it does not fail the attempt)"
            _note(ex, f"{what}: {row['check']}: {row.get('detail') or '(no reason given)'}")
    _note(ex, f"{len(failures)} of the checks that decide failed" + (f": {', '.join(failures)}" if failures else "")
          + (f"; {len(warnings)} warning(s)" if warnings else ""))  # fmt: skip
    return {"ok": not failures, "failures": failures, "warnings": warnings, "checks": results}


def _packages_in_cache(ex, spec, addressing, record):
    """Every package the gameplan installs has a .deb in apt-cacher-ng's cache: a build installs
    from there, offline. One listing of the cache; which packages lack one goes to the job log
    (what a gameplan installs is its content). Any version counts, and its dependencies are not
    looked at: a cache whose index now wants a newer .deb than it holds passes, and the build
    fails then."""
    wanted = sorted({package for vm in spec["vms"] for package in vm["install"]})
    if not wanted:
        record("packages:in-cache", True, "the gameplan installs nothing")
        return
    host = ((addressing.get("private_internet_ips") or {}).get("apt_cache") or {}).get("maintenance")
    if not host:
        record("packages:in-cache", False, f"no address of apt-cache ({INVENTORY})")
        return
    try:
        listing = ex.ssh(host, ["sudo", "find", APT_CACHE_DIR, "-type", "f", "-name", "*.deb", "-printf", "%f\\n"], timeout=120)
    except Exception as error:  # noqa: BLE001
        _note(ex, f"preflight: the apt cache's listing: {type(error).__name__}: {error}")
        record("packages:in-cache", False, "apt-cache did not list its cache (the job log on the host says why)")
        return
    cached = {name.split("_", 1)[0] for name in listing.split() if name.endswith(".deb")}  # <package>_<version>_<arch>.deb
    missing = [package for package in wanted if package not in cached]
    for package in missing:
        _note(ex, f"preflight: no .deb of {package} in the apt cache")
    record("packages:in-cache", not missing, f"{len(missing)} of {len(wanted)} packages not in the apt cache (the job log on the host "
           "names them)" if missing else f"{len(wanted)} packages there")  # fmt: skip


def _growth_mb(vms, states):
    """{name: MB} for each of these VMs with a QEMU: how much more of the host's memory that QEMU
    can take, its ceiling (and QEMU_OVERHEAD_MB) less what it holds now (Proxmox's memhost, else
    its QEMU's RSS). A VM whose ceiling Proxmox does not give is left out."""
    out = {}
    for vm in vms:
        if not _qemu_up(vm, states) or not vm.get("maxmem"):
            continue
        held = int(vm["memhost"]) // 2**20 if vm.get("memhost") else host_state.qemu_rss_mb(vm["vmid"])
        held = int(vm.get("mem") or 0) // 2**20 if held is None else held
        out[str(vm.get("name"))] = max(0, int(vm["maxmem"]) // 2**20 + QEMU_OVERHEAD_MB - held)
    return out


@_quiet
def preflight(ex, cfg, params):
    """Before each attempt of a round, before anything is built, rolled back or started: can the
    host run the gameplan's sandbox, and is everything around it as it must be? Changes nothing.
    Returns {ok, failures, checks, warnings, memory: {available_mb, needed_mb, reserve_mb,
    total_mb}}. Memory: a rooted guest can touch all of its RAM and defeat KSM and ballooning,
    so each VM counts with its ceiling, and each QEMU with its own overhead. Available is
    MemAvailable and what the ZFS ARC gives back under pressure (down to its floor, c_min): the
    kernel takes that before it kills anything. The VMs a worm reaches (the service VMs) and the devbox VMs
    that run can grow towards their ceilings too: a warning when that would not fit as well."""
    spec, _ = _gameplan(ex, params["gameplan"], params["gameplan_sha256"])
    results, warnings = [], []
    record = _recorder(results)
    window = Path(cfg.get("internet_flag", INTERNET_FLAG)).exists()
    record("host:internet-window-closed", not window,
           f"{INTERNET_FLAG} exists: internet access is allowed (`worms internet off`)" if window else "")  # fmt: skip
    _host_units(ex, record)
    options = ex.run(["pvesh", "get", "/cluster/firewall/options", "--output-format", "json"], check=False, timeout=60)
    try:
        options = json.loads(options or "{}")
    except ValueError:
        options = {}
    record("host:cluster-firewall-enabled", str(options.get("enable")) == "1", f"enable: {options.get('enable')}")

    resources = _resources(ex)
    states = _live_states(ex, resources)
    services = sorted((vm for vm in resources if vm.get("pool") in SERVICE_POOLS), key=lambda vm: vm["name"])
    optional = []  # a part of the private internet that is off: the operator may run the round without it
    for vm in services:
        status = _status(vm, states)
        record(f"services:{vm['name']}:running", status == "running", status)
        if status != "running" and vm.get("pool") == "private_internet" and vm["name"] not in ROUND_NEEDS:
            optional.append(f"services:{vm['name']}:running")
    for kind in ("scoring", "logging"):
        host = cfg.get(f"{kind}_host")
        command = ["sh", "-c", f"systemctl is-active {' '.join(SANDBOX_API_UNITS)} || true"]
        try:
            out = ex.ssh(host, command, timeout=60).split() if host else []
        except Exception as error:  # noqa: BLE001  (the VM does not answer)
            _note(ex, f"preflight: {kind} ({host}): {type(error).__name__}: {error}")
            out = []
        for unit, state in zip(SANDBOX_API_UNITS, out + ["no answer"] * len(SANDBOX_API_UNITS)):
            record(f"services:{kind}:{unit}", state == "active", state if host else f"no {kind}_host configured")
    # Off is no QEMU: a paused VM, or one whose guest halted, still holds its memory.
    if params["hold_devboxes"]:
        running = sorted(vm["name"] for vm in resources if vm.get("pool") == "devbox" and _qemu_up(vm, states))
        record("devboxes:held", not running, f"not off: {', '.join(running)}" if running else "every devbox VM is off")
    running = sorted(vm["name"] for vm in resources if vm.get("pool") == "sandbox" and _qemu_up(vm, states))
    record("sandbox:not-running", not running, f"not off: {', '.join(running)}" if running else "")

    memory = host_state.memory()
    qemus = len(spec["vms"]) + 1  # and the gateway
    overhead = qemus * QEMU_OVERHEAD_MB
    needed = sum(int(vm["memory"]) for vm in spec["vms"]) + SANDBOX_GW_MEMORY_MB + overhead
    reserve = int(cfg.get("memory_reserve_mb", MEMORY_RESERVE_MB))
    arc = memory.get("arc_reclaimable_mb") or 0
    available = None if memory["available_mb"] is None else memory["available_mb"] + arc
    admitted = available is not None and available >= needed + reserve
    record("memory:admission", admitted, f"{available} MB available ({memory['available_mb']} MB free and {arc} MB the "
           f"ZFS ARC gives back); {needed} MB for the sandbox (the ceilings of its {qemus - 1} VMs and the gateway, and "
           f"{overhead} MB for their QEMUs) and {reserve} MB to spare")  # fmt: skip
    growth = _growth_mb([vm for vm in resources if vm.get("pool") in (*SERVICE_POOLS, "devbox")], states)
    if admitted and available < needed + reserve + sum(growth.values()):
        # Not a failure: a host with every VM at its ceiling may not exist (worms-dev, 32 GB, cannot
        # run round_one so). Only the operator can weigh that: an OOM kill of a team VM then fails
        # that attempt (observe), the services' QEMUs are killed last (_oom_priority).
        biggest = ", ".join(f"{name} {mb} MB" for name, mb in sorted(growth.items(), key=lambda item: -item[1])[:4])
        warnings.append(f"memory: the service and devbox VMs that run can still grow by {sum(growth.values())} MB towards their "
                        f"ceilings (most: {biggest}); with that, {needed + reserve + sum(growth.values()) - available} MB would "
                        "be missing and the host's OOM killer would kill a team VM, failing that attempt")  # fmt: skip
    _packages_in_cache(ex, spec, _addressing(ex), record)

    # The services are rolled back to their clean snapshot before a new sandbox is built on them.
    def clean(vm):
        try:
            return snapshots.has_clean(ex, vm)
        except Exception:  # noqa: BLE001
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for vm, has in zip(services, pool.map(clean, services)):
            if has is None:
                warnings.append(f"{vm['name']}: its snapshots could not be read")
            elif not has:
                warnings.append(f"{vm['name']} has no clean snapshot: a new round's sandbox is built on it as it is "
                                "(`worms vm rebuild services`)")  # fmt: skip
    failures = [r["check"] for r in results if not r["ok"]]
    return {
        "ok": not failures, "failures": failures, "checks": results, "warnings": warnings, "optional": optional,
        "memory": {"available_mb": available, "needed_mb": needed, "reserve_mb": reserve, "total_mb": memory["total_mb"]},
    }  # fmt: skip


def _host_records(ex, cfg, since, vmids):
    """The host's own records since `since` (host_state.Records): the log lines of the kernel,
    systemd (of these VMs' scopes only: the rest of PID 1's lines, some 26000 a day, would push
    a long run's out of JOURNAL_LINES), qmeventd, Proxmox's task loggers and systemd-coredump,
    its task list, and the QEMU processes recorded."""
    matches = ["_TRANSPORT=kernel", "+", "_PID=1", *(f"UNIT={int(vmid)}.scope" for vmid in sorted(set(vmids))),
               "+", "SYSLOG_IDENTIFIER=qmeventd"]  # fmt: skip
    for logger in (*host_state.TASK_LOGGERS, host_state.COREDUMP_LOGGER):
        matches += ["+", f"SYSLOG_IDENTIFIER={logger}"]
    try:
        journal = ex.run(["journalctl", f"--since=@{int(since)}", "--no-pager", "-q", "-o", "short-unix",
                          "-n", str(host_state.JOURNAL_LINES), *matches], timeout=60)  # fmt: skip
        task_list = host_state.tasks()
    except Exception as error:  # noqa: BLE001
        return host_state.Records(since, error=f"{type(error).__name__}: {error}")
    return host_state.Records(since, journal, task_list, host_state.read_record(cfg.get("state_dir", STATE_DIR)))


def observe(ex, cfg, params):
    """What the host sees of a run's VMs now, in seconds, asking none of them (after the worm they
    are rooted): each one's state and, if it is not running, why (host_state: oom, crash, host,
    guest, unknown, from the host's records since `since`; a guest halted for good is stopped);
    the host's memory; the devbox VMs that have a QEMU; and the OOM priority of every team VM,
    set again (a VM started since has a new QEMU). Only VMs of the sandbox pool. Returns {vms:
    [{vmid, name, status, cause, detail}], memory, devboxes_running, oom_adjusted}. The agent
    runs it beside any other job (agent.py, observer)."""
    resources = _resources(ex)
    pool = {int(vm["vmid"]): vm for vm in resources if vm.get("pool") == "sandbox"}
    outside = [vmid for vmid in params["vmids"] if vmid not in pool]
    if outside:
        raise validation.Invalid(f"not VMs of the sandbox pool: {', '.join(map(str, outside))}")
    states = _live_states(ex, resources)
    since = params["since"] if params.get("since") is not None else time.time() - OBSERVE_WINDOW
    entries = {vmid: states.get(vmid) or {"status": pool[vmid].get("status")} for vmid in params["vmids"]}
    status = {vmid: host_state.state(entry) for vmid, entry in entries.items()}
    down = [vmid for vmid in params["vmids"] if status[vmid] != "running"]
    causes = {}
    if down:  # the records are read before the record of QEMUs changes (_oom_priority)
        records = _host_records(ex, cfg, since, down)
        causes = {vmid: records.cause(vmid, status[vmid], entries[vmid].get("qmpstatus")) for vmid in down}
        late = [vmid for vmid in down if causes[vmid][0] == "unknown" and not records.error and status[vmid] == "stopped"]
        if late:  # stopped, and no line of it yet
            time.sleep(OBSERVE_REREAD)
            records = _host_records(ex, cfg, since, late)
            causes.update({vmid: records.cause(vmid, status[vmid], entries[vmid].get("qmpstatus")) for vmid in late})
    vms = [{"vmid": vmid, "name": pool[vmid].get("name"), "status": status[vmid], "cause": causes.get(vmid, (None, ""))[0],
            "detail": causes.get(vmid, (None, ""))[1]} for vmid in params["vmids"]]  # fmt: skip
    devboxes = sorted(vm["name"] for vm in resources if vm.get("pool") == "devbox" and _qemu_up(vm, states))
    adjusted = _oom_priority(ex, cfg, resources, states)
    return {"vms": vms, "memory": host_state.memory(), "devboxes_running": devboxes, "oom_adjusted": adjusted}


def deploy(ex, cfg, params):
    """Put the team's worm on its entry VM and arm the fuse that starts it after worm_delay
    seconds; check the fuse is armed. Every flag is placed already (prepare)."""
    vms = _sandbox_vms(ex)
    start = params["start_vm"]
    if start not in vms:
        raise validation.Invalid(f"start_vm {start} is not in the sandbox")
    start_ip = vms[start]["maintenance_ip"]

    # Tell the scoring and logging servers which team is running now, so they
    # record its uuid on every submission (no access token is used anymore).
    _set_current_team(ex, cfg, params["team_uuid"])

    _place_worm(ex, cfg, start_ip, params["artifact"])
    delay = params["worm_delay"]
    armed_at = _arm_worm(ex, start_ip, delay)
    _check_armed(ex, start_ip, start)
    return {"start_vm": start, "worm_delay": delay, "team_uuid": params["team_uuid"], "armed_at": armed_at}


def _check_armed(ex, ip, name):
    """Armed: the worm's timer waits, or (a short fuse) the worm runs already."""
    states = ex.ssh(ip, ["sh", "-c", f"systemctl is-active {WORM_UNIT}.timer {WORM_UNIT}.service || true"], timeout=60)
    if not {"active", "activating"} & set(states.split()):
        raise RuntimeError(f"the worm's timer on {name} is not armed ({' '.join(states.split()) or 'no unit'})")


def _check_running(ex, vmid, name):
    """The VM runs (the host's word): one powered off would fail the deploy with a bare SSH error."""
    said = ex.run(["qm", "status", str(vmid)], check=False, timeout=60).strip()
    status = said.removeprefix("status:").strip()
    if status != "running":
        raise RuntimeError(f"{name} is {status or 'unknown to the host'}, not running: the worm cannot go onto it (the "
                           f"team's devbox reset starts it again, or `worms vm start {name}`)")  # fmt: skip


def _place_worm(ex, cfg, ip, artifact):
    """The verified zip onto a VM, unpacked, root's (the worm's own: it starts as root)."""
    raw = _fetch_artifact(ex, cfg, artifact)
    # base64 over stdin so the binary survives the text channel and never appears on a command line.
    ex.ssh(
        ip,
        ["sudo", "/bin/sh", "-c", f"base64 -d > {WORM_DIR}/{WORM_ZIP}"],
        input=base64.b64encode(raw).decode("ascii"),
    )
    # Unpack it now, so a broken upload fails the deploy with a reason
    # instead of a worm that never starts.
    ex.ssh(
        ip,
        ["sudo", "/bin/sh", "-c", UNPACK.format(dir=WORM_DIR, code=WORM_CODE, zip=WORM_ZIP, entry=WORM_ENTRY)],
    )


# An earlier worm's unit and fuse stopped (its processes with it: the unit's cgroup), and
# forgotten, so that systemd-run may use the name again. Fixed literals; nothing there is an error.
STOP_WORM = (f"systemctl stop {WORM_UNIT}.timer {WORM_UNIT}.service 2>/dev/null; "
             f"systemctl reset-failed {WORM_UNIT}.timer {WORM_UNIT}.service 2>/dev/null; true")  # fmt: skip


def _stop_worm(ex, ip):
    ex.ssh(ip, ["sudo", "/bin/sh", "-c", STOP_WORM])


def _arm_worm(ex, ip, delay, *properties):
    """Arm a one-shot systemd timer on the VM: after `delay` it runs the worm as root (User=root:
    HOME=/root), in its directory. No shell: fixed literals. Returns when the fuse was lit (the
    host's clock): the worm's start is dated from this, not from when anyone noticed the job was
    done."""
    ex.ssh(
        ip,
        [
            "sudo", "systemd-run", f"--on-active={delay}", f"--unit={WORM_UNIT}", *properties,
            "--working-directory", f"{WORM_DIR}/{WORM_CODE}", "--uid", "root",
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


def remove_flags(ex, cfg, params):
    """Remove flags, and check that they are gone. strict (before the worm): any failure fails
    the job; otherwise failures are reported (the worm may be behind them)."""
    removed, failed = _remove_flags(ex, _sandbox_vms(ex), params["vms"], params["strict"])
    return {"removed": removed, "failed": failed}


def place_flags(ex, cfg, params):
    """New flags, read back. strict as for remove_flags."""
    released, failed = _place_flags(ex, _sandbox_vms(ex), params["vms"], params["strict"])
    return {"released": released, "failed": failed}


@_quiet
def write_file(ex, cfg, params):
    """A file the run's gameplan declared, by its number: its content comes from the host's
    copy of the gameplan and the run's secrets, never from the job. A failure after the worm
    is returned, but by name only: the reason (it may quote the path) goes to the job log."""
    state = _load_run_state(cfg, params["run_uuid"])
    spec, _ = _gameplan(ex, state["gameplan"], state["sha256"])
    entry = next((f for f in spec["files"] if f["id"] == params["file"]), None)
    if entry is None:
        raise validation.Invalid(f"the gameplan has no file {params['file']}")
    vms = _sandbox_vms(ex)
    if entry["vm"] not in vms:
        raise validation.Invalid(f"{entry['vm']} is not a VM of the current sandbox")
    problem = _guest_step(params["strict"], lambda: _write_gameplan_file(ex, vms[entry["vm"]]["maintenance_ip"], entry, state["secrets"]))
    if problem:
        ex.note(f"file {params['file']} on {entry['vm']}: {problem}")
        problem = "the file could not be written: the job log on the host says why"
    return {"written": problem is None, "vm": entry["vm"], "error": problem}


@_quiet
def read_file(ex, cfg, params):
    """Whether a file is on a sandbox VM, its size and SHA-256, for execute(): never its
    content, which would reach the database. The file is named by its number in the gameplan's
    `reads`; its path comes from the host's copy. After the worm (not strict) the VM may lie or
    fail: the reason is returned, by name only (it may quote the path); the job log has it."""
    state = _load_run_state(cfg, params["run_uuid"])
    spec, _ = _gameplan(ex, state["gameplan"], state["sha256"])
    entry = next((r for r in spec["reads"] if r["id"] == params["file"]), None)
    if entry is None:
        raise validation.Invalid(f"the gameplan has no readable file {params['file']}")
    vms = _sandbox_vms(ex)
    if entry["vm"] not in vms:
        raise validation.Invalid(f"{entry['vm']} is not a VM of the current sandbox")
    found = {}

    def read():
        out = ex.ssh(vms[entry["vm"]]["maintenance_ip"], ["sudo", "sh", "-c", READ_FILE, "sh", entry["path"]], timeout=120)
        lines = out.split()
        if lines == ["missing"]:
            found.update(exists=False, size=None, sha256=None)
        elif len(lines) == 3 and lines[0].isdigit() and validation.SHA256.fullmatch(lines[1]) and lines[2] == "-":
            found.update(exists=True, size=int(lines[0]), sha256=lines[1])
        else:
            raise RuntimeError("the VM's answer is not a size and a SHA-256")

    problem = _guest_step(params["strict"], read)
    if problem:
        ex.note(f"reading file {params['file']} on {entry['vm']}: {problem}")
        return {"error": "the file could not be read: the job log on the host says why"}
    return found


def teardown(ex, cfg, params):
    """With `keep`: an attempt's end. Its VMs powered off, the sandbox and its snapshot kept for
    the next attempt's rollback. Without (the round's end, after delete_snapshot): the sandbox
    destroyed. The run's secrets go either way."""
    _clear_current_team(ex, cfg)
    if params.get("run_uuid"):
        _run_state_path(cfg, params["run_uuid"]).unlink(missing_ok=True)
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
# Devbox (docs/04-dev-environment.md)
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


def _devbox_map_path(cfg):
    """The host's devbox map (devbox_map.PATH, under the agent's state_dir)."""
    return Path(cfg.get("state_dir", STATE_DIR)) / Path(devbox_map.PATH).name


def _write_devbox_map(ex, cfg, teams):
    """The source-address -> team-uuid map, so that a devbox submission is attributed to its
    team (docs/04-dev-environment.md): the services source of a team is its devbox-gw address
    from the stack output. Written on the host, the one source (root only: the database is not
    the agent's to read again), then pushed from it onto scoring and logging (devbox_map)."""
    devbox = ex.tofu_output("devbox").get("devbox", {}) or {}
    lines = []
    for team in teams:
        info = devbox.get(team["slug"])
        if info and info.get("services_source_ip"):
            lines.append(f"{info['services_source_ip']} {team['team_uuid']}")
    path = _devbox_map_path(cfg)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        fh.write("".join(f"{line}\n" for line in lines))
    _push_devbox_map(ex, cfg)


def _push_devbox_map(ex, cfg):
    """The host's map onto scoring and logging (devbox_map.push). After a rollback of them: their
    snapshot is older than the map. None yet (no devbox job has run): nothing to push."""
    hosts = [cfg.get("scoring_host"), cfg.get("logging_host")]
    if not all(hosts):
        raise validation.Invalid("scoring_host/logging_host not configured for the round agent")
    try:
        devbox_map.push(ex.ssh, hosts, path=_devbox_map_path(cfg))
    except devbox_map.Missing:
        pass


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
    _install_capture_vm(ex, _devbox_vms(ex))  # every team's A, B and C (not the gateways)
    report, sources = _devbox_report(ex, teams)
    _write_devbox_map(ex, cfg, teams)
    return {"vms": report, "services_sources": sources, "addresses": _devbox_addresses(teams)}


def devbox_deploy(ex, cfg, params):
    """Put a team's dev upload on its VM A and arm the fuse, as `deploy` does for a sandbox. The
    devbox is as it is: no reset before it (since 2026-11-24), so an earlier upload's worm, still
    running or its fuse still set, is stopped first (its unit's name is the new one's). The tokens
    are those of the devbox's last reset, and scoring and logging know a devbox by its gateway's
    address, so there are no secrets to release and no current team to set. A devbox is not torn
    down when its time is up, so systemd stops main.sh instead."""
    start = params["start_vm"]
    vm = _devbox_vms(ex).get(start)
    if vm is None or vm["team"] != params["team_slug"]:
        raise validation.Invalid(f"{start} is not a VM of team {params['team_slug']}")
    if not _has_team_vm_cpu(ex, vm["vmid"]):
        raise validation.Invalid(f"{start} does not have the CPU a team's VM must have: {TEAM_VM_CPU}")
    _check_running(ex, vm["vmid"], start)
    _stop_worm(ex, vm["maintenance_ip"])
    _place_worm(ex, cfg, vm["maintenance_ip"], params["artifact"])
    delay = params["worm_delay"]
    armed_at = _arm_worm(ex, vm["maintenance_ip"], delay, f"--property=RuntimeMaxSec={params['run_duration']}")
    _check_armed(ex, vm["maintenance_ip"], start)  # as a round's deploy: Running on the dashboard means it was
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


# A devbox VM's clean snapshot is from before any team key or token. A reset rolls back to it and
# writes, over SSH, the internal CA, the team's keys and new tokens: seconds. A snapshot from the
# first boot only (before 2026-11-19) lacks the devbox's setup (apt and pip pointed at the private
# internet, NTP, hostname, CA): the team's next reset sets it up once, without the network (no apt
# refresh, no package), and takes the snapshot again; every reset after that is the rollback alone.
DEVBOX_CONFIGURED = "worms devbox: configured, before any team key"
CA_CERT = "ansible/pki/worms/ca.crt"  # in the workspace (tasks/workspace.yml copies it); worms_cli.keys.CA_FILE
CA_INSTALL = "install -m 644 /dev/stdin /usr/local/share/ca-certificates/worms-internal-ca.crt && update-ca-certificates"  # worms_cli.keys.CA_SCRIPT


def _configure_devbox_base(ex, teams, slug, network=True):
    """The team's three VMs configured without a team key (an empty key file), and that made their
    clean snapshot. Nothing of the team has reached them: they were just built or rolled back.
    Without `network`: no apt refresh and no package, only files (a reset's one-time setup)."""
    overrides = {} if network else {"apt_client_refresh_offline": False, "devbox_packages": []}
    _configure_devbox(ex, [{**t, "authorized_keys": []} for t in teams], limit=[f"devbox-{slug}-{role}" for role in DEVBOX_ROLES],
                      overrides=overrides)  # fmt: skip
    vms = _team_vms(ex, slug)
    snapshots.take(ex, vms, DEVBOX_CONFIGURED)
    ips = {info["vmid"]: info["maintenance_ip"] for info in _devbox_vms(ex).values()}
    snapshots.wait_for_ssh(ex, [ips[vm["vmid"]] for vm in vms])


def _try_configure_devbox_base(ex, teams, slug, network=True):
    """_configure_devbox_base, or False if it failed: the team still gets its keys and tokens, and
    its upload runs (on VMs as their snapshot left them); no snapshot is taken, the next reset tries
    again. A team must never wait on a package the offline apt cache lacks."""
    try:
        _configure_devbox_base(ex, teams, slug, network)
    except Exception as exc:  # noqa: BLE001  (Ansible's failure, in the job log)
        ex.note(f"the devbox's configuration failed, it is used as its snapshot left it: {exc}"[:2000])
        return False
    return True


def _install_ca(ex, vms):
    """The internal CA, trusted on the team's VMs: a worm reaches scoring and logging over
    verified HTTPS (role ca_distribute does the same in a configuration)."""
    if not vms:
        return
    ca = (ex.repo_root / CA_CERT).read_text()
    for info in vms.values():
        ex.ssh(info["maintenance_ip"], ["sudo", "sh", "-c", CA_INSTALL], input=ca)


# `capture-vm` on every team VM (sandbox and devbox): it submits the VM's flag to scoring, in the
# background, once a minute until scoring accepts it (scripts/capture-vm). Written over SSH after
# every reset (a devbox's, a sandbox attempt's prepare), as the CA is, so a snapshot from before
# it has it too.
CAPTURE_VM = "scripts/capture-vm"  # in the workspace
CAPTURE_VM_PATH = "/usr/bin/capture-vm"


def _install_capture_vm(ex, vms):
    """/usr/bin/capture-vm on these VMs ({name: {maintenance_ip, ...}})."""
    if not vms:
        return
    script = (ex.repo_root / CAPTURE_VM).read_text()
    for info in vms.values():
        ex.ssh(info["maintenance_ip"], ["sudo", "install", "-m", "0755", "/dev/stdin", CAPTURE_VM_PATH], input=script)


def _write_team_keys(ex, team, vms):
    """The team's SSH keys on its VMs, directly: the key file `worms key sync` writes too.
    The SSH gateway's lines do not change with a reset."""
    from worms_cli import keys

    text = "".join(f"{key}\n" for key in map(keys.canonical, team["authorized_keys"]) if key)
    for info in vms.values():
        ex.ssh(info["maintenance_ip"], ["sudo", "sh", "-c", keys.DEVBOX_SCRIPT], input=text)


def devbox_rebuild(ex, cfg, params):
    """Give one team a fresh devbox and new tokens, without touching other teams. The team's
    version has already been bumped by the orchestrator (which clears the dashboard score). Its
    three VMs go back to their clean snapshot; then the CA, its keys and new tokens over SSH: no
    Ansible, seconds. VMs without a snapshot, or all three when `fresh` (`worms apply devbox
    --rebuild`), are replaced, configured and snapshotted: a full rebuild."""
    teams = params["teams"]
    team = next((t for t in teams if t["team_uuid"] == params["team_uuid"]), None)
    if team is None:
        raise validation.Invalid("team_uuid is not in the teams list")
    slug = team["slug"]

    vms = _team_vms(ex, slug)
    if not params.get("fresh") and len(vms) == len(DEVBOX_ROLES) and all(snapshots.has_clean(ex, vm) for vm in vms):
        _refuse_while_internet(cfg, "start a devbox")
        snapshots.roll_back(ex, vms)
        ips = {info["vmid"]: info["maintenance_ip"] for info in _devbox_vms(ex).values()}
        snapshots.wait_for_ssh(ex, [ips[vm["vmid"]] for vm in vms])
        if all(snapshots.clean_description(ex, vm) == DEVBOX_CONFIGURED for vm in vms):
            how = "rolled back to its snapshot"
        elif _try_configure_devbox_base(ex, teams, slug, network=False):
            how = "rolled back, set up once (no network) and snapshotted: later resets are the rollback alone"
        else:
            how = "rolled back; its one-time setup failed (the job log says why), the next reset tries again"
    else:
        # Replace just this team's three VMs (fresh disks); the rest is a no-op.
        ex.generate_devbox(teams, ex.repo_root / DEVBOX_GENERATED)
        ex.tofu("devbox", "apply", "-auto-approve", "-input=false", "-parallelism=1", *_devbox_replace_args(slug), timeout=cfg.get("apply_timeout", 5400))
        snapshots.record(ex, lambda: _team_vms(ex, slug), "recreated", "fresh disks (devbox rebuild)")
        ex.worms("inventory", timeout=300)
        ips = {info["vmid"]: info["maintenance_ip"] for info in _devbox_vms(ex).values()}
        snapshots.wait_for_first_boot(ex, [ips[vm["vmid"]] for vm in _team_vms(ex, slug)])
        if _try_configure_devbox_base(ex, teams, slug):
            how = "replaced, configured and snapshotted"
        else:
            how = "replaced; its configuration failed (the job log says why), the next reset tries again"
    own = {name: info for name, info in _devbox_vms(ex).items() if info["team"] == slug}
    _install_ca(ex, own)
    _install_capture_vm(ex, own)
    _write_team_keys(ex, team, own)

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
    when a team adds/removes a key or leaves (docs/04-dev-environment.md). Idempotent.
    Only the keys (DevboxKeys.yml): no package and no CA, so nothing here needs the
    internet or what the offline apt cache holds."""
    teams = params["teams"]
    # The map first: it is what a reset of scoring and logging takes.
    _write_devbox_map(ex, cfg, teams)
    _configure_devbox(ex, teams, step="devbox-keys")
    return {"reconfigured": [t["slug"] for t in teams], "addresses": _devbox_addresses(teams)}


def _configure_devbox(ex, teams, step="devbox-configure", limit=None, overrides=None):
    """Run the devbox + SSH-gateway Ansible with the teams' keys and VM targets
    as extra vars (CA trust, team authorized_keys, confined gateway keys); `step`
    devbox-keys: the keys alone. `limit`: only these VMs (a reset: the team's three).

    A team's SSH key is attacker-controlled text that Ansible renders into the
    devbox and gateway authorized_keys (on the Proxmox host, as root). Every
    key string is therefore tagged `!unsafe` in the variable file, so a
    `{{...}}`/`{%...%}` in it is never evaluated as a template -- defence in
    depth behind the strict validation (validation.ssh_keys, which already
    rejects template characters). See docs/07-security.md."""
    import tempfile

    devbox = ex.tofu_output("devbox").get("devbox", {}) or {}
    team_keys = {t["slug"]: [_Unsafe(k) for k in t["authorized_keys"]] for t in teams}
    gateway_keys = []
    for t in teams:
        vms = (devbox.get(t["slug"], {}) or {}).get("vms", [])
        # The gateway forwards each team to its ENTRY VM (A) only; the worm
        # pivots A->B->C inside the team's own devbox network, as in the sandbox.
        # permitopen is thus a single host, so a team key cannot open a forward
        # to B/C directly (docs/04-dev-environment.md; docs/07-security.md).
        entry = f"devbox-{t['slug']}-a"
        targets = [f"{vm['ssh_fabric_ip']}:22" for vm in vms if vm["name"] == entry]
        if not targets:
            # No VM A (yet): no line at all. Without permitopen the key could forward
            # anywhere the gateway reaches (sshd has AllowTcpForwarding local).
            continue
        for key in t["authorized_keys"]:
            gateway_keys.append({"public_key": _Unsafe(key), "comment": f"team={t['slug']}", "permitopen": targets})
    extra = {"devbox_team_keys": team_keys, "ssh_gateway_authorized_keys": gateway_keys, **(overrides or {})}
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        fh.write(_unsafe_yaml(extra))
        varfile = fh.name
    ex.worms(step, timeout=1800, args=f"-e @{varfile}" + (f" --limit {','.join(limit)}" if limit else ""))


# -- the devboxes, held shut down for a round (docs/05-live-environment.md) ---------------------

HELD_DEVBOXES = "held-devboxes.json"


def paused_path(cfg):
    """The operator's pause (`worms jobs pause`): while it exists the agent claims no job (agent.py,
    claim); the one under way ends first. Its text says who paused it, and when."""
    return Path(cfg.get("state_dir", STATE_DIR)) / "agent-paused"


def held_devboxes_path(cfg):
    """The host's record of the devbox VMs shut down for rounds with devboxes_off: while it
    exists the agent takes no devbox job (agent.py, claim), which would start one (a reset) or
    need it running."""
    return Path(cfg.get("state_dir", STATE_DIR)) / HELD_DEVBOXES


def _write_held(path, held):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(held))
    os.replace(tmp, path)


def hold_devboxes(ex, cfg, params):
    """Shut down every devbox VM and gateway that runs (the pool devbox), until release_devboxes.
    The record comes first (no devbox job from then on), and each VM is in it before it is shut
    down: release_devboxes starts exactly these again, after a crash or a second hold too."""
    path = held_devboxes_path(cfg)
    held = json.loads(path.read_text()) if path.exists() else {"rounds": [], "vms": []}
    if params["round_uuid"] not in held["rounds"]:
        held["rounds"].append(params["round_uuid"])
    _write_held(path, held)
    stopped = []
    for vm in snapshots.pool_vms(ex, "devbox"):
        if vm.get("status") != "running":
            continue
        entry = {"vmid": int(vm["vmid"]), "name": vm["name"]}
        if entry not in held["vms"]:
            held["vms"].append(entry)
            _write_held(path, held)
        snapshots.shut_down(ex, [vm])
        stopped.append(vm["name"])
    return {"stopped": stopped, "held": [entry["name"] for entry in held["vms"]]}


def release_devboxes(ex, cfg, params):
    """Start again the devbox VMs hold_devboxes shut down: those still there (same vmid and
    name) and stopped. Then the hold is over. Not while internet access is allowed."""
    path = held_devboxes_path(cfg)
    if not path.exists():
        return {"started": []}
    held = json.loads(path.read_text())
    _refuse_while_internet(cfg, "start the devbox VMs again")
    there = {int(vm["vmid"]): vm for vm in snapshots.pool_vms(ex, "devbox")}
    start = [
        there[entry["vmid"]] for entry in held.get("vms", [])
        if entry["vmid"] in there and there[entry["vmid"]]["name"] == entry["name"] and there[entry["vmid"]].get("status") == "stopped"
    ]  # fmt: skip
    snapshots.start(ex, start)
    path.unlink()
    return {"started": [vm["name"] for vm in start]}


HANDLERS = {
    "gameplans": gameplans,
    "preflight": preflight,
    "build": build,
    "snapshot": snapshot,
    "rollback": rollback,
    "prepare": prepare,
    "check": check,
    "observe": observe,
    "deploy": deploy,
    "remove_flags": remove_flags,
    "place_flags": place_flags,
    "write_file": write_file,
    "read_file": read_file,
    "delete_snapshot": delete_snapshot,
    "teardown": teardown,
    "devbox_provision": devbox_provision,
    "devbox_rebuild": devbox_rebuild,
    "devbox_reconfigure": devbox_reconfigure,
    "devbox_deploy": devbox_deploy,
    "hold_devboxes": hold_devboxes,
    "release_devboxes": release_devboxes,
}
