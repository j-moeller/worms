"""The clean state a VM returns to instead of being rebuilt: one snapshot per VM, `clean`.

A round's sandbox is built once and every team's run starts from its snapshot; a devbox reset
rolls the team's three VMs back; a new round's sandbox is built on service VMs rolled back to
the snapshot `worms apply` took of them (src/worms_cli/pipeline.py takes those). docs/05-live-environment.md.

A snapshot is only as clean as the moment it is taken. Team code is assumed hostile and root
on its VM, so every snapshot here is taken before any team code or team key has reached the
VM, and never replaced while it may have.

Snapshots are taken of stopped VMs: these VMs run no guest agent, so a snapshot of a running
one would hold only what a power cut leaves on the disk. A rollback therefore starts the VM
from a cold boot. Everything here goes through `qm` and `pvesh` on the host itself, with
numbers it read from Proxmox, never a value from the database.
"""

from __future__ import annotations

import json
import subprocess
import time

from round_agent.executor import CommandError

CLEAN = "clean"
SHUTDOWN_TIMEOUT = 120  # then the VM is powered off: what is on its disk is what counts
BOOT_TIMEOUT = 600


def pool_vms(ex, pool):
    """The VMs in a Proxmox pool, by vmid: [{vmid, name, node, status}]."""
    out = ex.run(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"], timeout=60)
    return sorted((vm for vm in json.loads(out or "[]") if vm.get("pool") == pool), key=lambda vm: vm["vmid"])


def has_clean(ex, vm):
    path = f"/nodes/{vm['node']}/qemu/{int(vm['vmid'])}/snapshot"
    out = ex.run(["pvesh", "get", path, "--output-format", "json"], timeout=60)
    return any(snapshot.get("name") == CLEAN for snapshot in json.loads(out or "[]"))


def clean_description(ex, vm):
    """The description of `vm`'s clean snapshot ("" without one): which state it holds."""
    path = f"/nodes/{vm['node']}/qemu/{int(vm['vmid'])}/snapshot"
    out = ex.run(["pvesh", "get", path, "--output-format", "json"], timeout=60)
    return next((str(s.get("description") or "").strip() for s in json.loads(out or "[]") if s.get("name") == CLEAN), "")


def shut_down(ex, vms):
    """Shut the running ones down (hard after SHUTDOWN_TIMEOUT). Returns those it shut down."""
    running = [vm for vm in vms if vm.get("status") == "running"]
    for vm in running:
        ex.run(["qm", "shutdown", str(int(vm["vmid"])), "--timeout", str(SHUTDOWN_TIMEOUT), "--forceStop", "1"],
               timeout=SHUTDOWN_TIMEOUT + 60)  # fmt: skip
    return running


def stop(ex, vms):
    """Power the running ones off at once: for VMs whose disk is rolled back before next use."""
    for vm in vms:
        if vm.get("status") == "running":
            ex.run(["qm", "stop", str(int(vm["vmid"]))], timeout=120)


def start(ex, vms):
    for vm in vms:
        ex.run(["qm", "start", str(int(vm["vmid"]))], timeout=120)


def recording(ex):
    """Whether this executor keeps the host's VM history (src/worms_cli/history.py)."""
    return getattr(ex, "history", None) is not None


def record(ex, vms, event, detail=""):
    """Append `vms` (a list, or a function that lists them, asked only when recording) to the
    VM history. Nothing of the history may fail a job: a listing that fails is a line in the
    job's log, and the job goes on."""
    if not recording(ex):
        return
    try:
        ex.history(vms() if callable(vms) else vms, event, detail)
    except Exception as exc:  # noqa: BLE001
        _note(ex, f"the VM history did not get {event}: {exc}")


def listed(ex, vms):
    """`vms()` for the history (a list to compare with later), or [] when it fails or nothing is
    recorded: never an error for the job."""
    if not recording(ex):
        return []
    try:
        return vms()
    except Exception as exc:  # noqa: BLE001
        _note(ex, f"the VM history cannot list the VMs: {exc}")
        return []


def _note(ex, text):
    note = getattr(ex, "note", None)
    if note is not None:
        try:
            note(text)
        except Exception:  # noqa: BLE001  (a log line is not worth a job either)
            pass


def take(ex, vms, description):
    """Make each VM's present state its clean snapshot (replacing an older one), and start it
    again. The caller vouches that no team code or key has reached these VMs yet."""
    for vm in vms:
        vmid = str(int(vm["vmid"]))
        shut_down(ex, [vm])
        if has_clean(ex, vm):
            ex.run(["qm", "delsnapshot", vmid, CLEAN], timeout=600)
        ex.run(["qm", "snapshot", vmid, CLEAN, "--description", description], timeout=600)
        record(ex, [vm], "snapshot", description)
        ex.run(["qm", "start", vmid], timeout=120)


def delete(ex, vms):
    """Each VM's clean snapshot deleted (those that have one). Returns the names of those it deleted."""
    deleted = []
    for vm in vms:
        if has_clean(ex, vm):
            ex.run(["qm", "delsnapshot", str(int(vm["vmid"])), CLEAN], timeout=600)
            record(ex, [vm], "snapshot_deleted", CLEAN)
            deleted.append(vm["name"])
    return deleted


def roll_back(ex, vms):
    """Each VM back to its clean snapshot (disk and configuration), started. A running VM is
    stopped first, without a shutdown: nothing on it is kept."""
    for vm in vms:
        ex.run(["qm", "rollback", str(int(vm["vmid"])), CLEAN, "--start", "1"], timeout=600)
        record(ex, [vm], "rolled_back", CLEAN)


def wait_for_ssh(ex, hosts, timeout=BOOT_TIMEOUT, pause=5):
    """Until every host answers over SSH, or TimeoutError."""
    deadline = time.monotonic() + timeout
    waiting, last = list(dict.fromkeys(hosts)), None
    while waiting:
        for host in list(waiting):
            try:
                ex.ssh(host, ["true"], timeout=30)
                waiting.remove(host)
            except (CommandError, subprocess.TimeoutExpired) as exc:  # not up yet
                last = exc
        if waiting:
            if time.monotonic() > deadline:
                raise TimeoutError(f"no SSH after {timeout} s: {', '.join(waiting)} ({last})")
            time.sleep(pause)


def wait_for_first_boot(ex, hosts):
    """SSH, then cloud-init done: what a freshly created VM has to finish before it is
    snapshotted, or its clean state would be half a first boot."""
    wait_for_ssh(ex, hosts)
    for host in hosts:
        try:
            ex.ssh(host, ["cloud-init", "status", "--wait"], timeout=BOOT_TIMEOUT)
        except CommandError as exc:
            if exc.returncode != 2:  # 2: done, with recoverable errors (degraded)
                raise
