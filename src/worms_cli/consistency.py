"""What `worms status` compares: a copy of a fact against its source, each check a pure function
of facts read once (manage.py: the host probe, the VM listing, the database). A finding is a
Row: what differs, and the command that repairs it (docs/03-manage-py.md, `worms status`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

COMMIT = re.compile(r"[0-9a-f]{7,40}(-dirty)?")
SERVICE_POOLS = ("ephemeral", "private_internet")  # worms_cli/pipeline.py: the VMs a reset rolls back


@dataclass(frozen=True)
class Row:
    name: str
    state: str  # output.status_cell's: "error", "warn", "running", "ok", "unknown"
    text: str
    repair: str = ""

    def said(self) -> str:
        return self.text + (f": {self.repair}" if self.repair else "")


def _same_commit(a: str, b: str) -> bool:
    return a.removesuffix("-dirty") == b.removesuffix("-dirty")


def _epoch(stamp: str) -> float | None:
    """A VM history time (`2026-11-25T17:06:16Z`), in seconds."""
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return f"{seconds // 86400} d"
    if seconds >= 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 60} min"


# -- the service VMs' snapshots (T4) -----------------------------------------------------------------


def parse_snapshots(text: str) -> dict[int, dict]:
    """{vmid: {"time": epoch, "commit": str, "description": str}}: each VM's `clean` snapshot, from
    the host probe's lines `<.../<vmid>.conf> <snaptime> <description>` (the description is the
    comment line under `[clean]` in the VM's config: `worms <commit>`, pipeline._take_snapshots)."""
    out = {}
    for line in text.splitlines():
        path, _, rest = line.partition(" ")
        snaptime, _, description = rest.partition(" ")
        vmid = path.rsplit("/", 1)[-1].removesuffix(".conf")
        if vmid.isdigit() and snaptime.isdigit():
            words = description.split()
            commit = words[1] if len(words) >= 2 and words[0] == "worms" and COMMIT.fullmatch(words[1]) else ""
            out[int(vmid)] = {"time": int(snaptime), "commit": commit, "description": description.strip()}
    return out


def snapshot_cell(vm: dict, snapshots: dict, now: float) -> str:
    """`a3a66c9 2 d` for a service VM's `clean` snapshot, `—` without one, "" for any other VM."""
    if vm.get("pool") not in SERVICE_POOLS:
        return ""
    snapshot = snapshots.get(int(vm["vmid"]))
    if not snapshot:
        return "—"
    return f"{snapshot['commit'] or '?'} {age(now - snapshot['time'])}"


def snapshot_rows(vms: list, snapshots: dict, deployed: dict, events: list) -> list[Row]:
    """The service VMs whose `clean` snapshot, which every reset and round build returns them to,
    is not what was deployed: its commit differs from deployed.json's for the VM's component, or
    the VM was configured after it (the VM history: `configured`/`packages` later than the
    snapshot), which the next reset undoes. A VM without a snapshot is the attention block's
    `snapshots` row."""
    configured: dict[tuple, float] = {}
    for event in events:
        if event.get("event") in ("configured", "packages"):
            when = _epoch(event.get("time", ""))
            key = (event.get("vmid"), event.get("name"))
            if when is not None and when > configured.get(key, 0):
                configured[key] = when
    behind, after = [], []
    for vm in sorted((vm for vm in vms if vm.get("pool") in SERVICE_POOLS), key=lambda vm: vm["name"]):
        snapshot = snapshots.get(int(vm["vmid"]))
        if not snapshot:
            continue
        entry = deployed.get(vm["name"]) if isinstance(deployed, dict) else None
        commit = entry.get("commit", "") if isinstance(entry, dict) else ""
        if commit and snapshot["commit"] and not _same_commit(commit, snapshot["commit"]):
            behind.append(f"{vm['name']} (snapshot {snapshot['commit']}, deployed {commit})")
        if configured.get((int(vm["vmid"]), vm["name"]), 0) > snapshot["time"]:
            after.append(vm["name"])
    rows = []
    if behind:
        rows.append(Row("snapshot behind deployed", "warn", "; ".join(behind) + ": a reset or round build returns them "
                        "to the snapshot's code", "`worms apply <component>` takes the snapshot again"))  # fmt: skip
    if after:
        rows.append(Row("configured after its snapshot", "warn", ", ".join(after) + ": the next reset or round build "
                        "undoes that configuration", "`worms apply <component>` (it snapshots at its end)"))  # fmt: skip
    return rows


# -- which commit this system runs (T7) -------------------------------------------------------


def version_summary(checkout: str, deployed: dict, expected, snapshots: dict, running: dict) -> tuple[str, list[str]]:
    """("on" or "mixed", the lines `worms status` shows): one answer to "which commit does this
    system run?". `checkout`: this checkout's HEAD (`-dirty` with changes); `deployed`:
    deployed.json ({component: {commit}}), `expected` the components it should name (a missing
    one reads "unknown"); `snapshots`: {service VM: its `clean` snapshot's commit}; `running`:
    {app: the commit its units started with (T8)}. On one commit only when every one of them,
    and the checkout, agree and the checkout is clean. A service VM without a snapshot: "no
    snapshot" (the attention block's `snapshots` row says what to do)."""
    groups: dict[str, list[str]] = {}

    def add(commit, label):
        groups.setdefault(commit or "unknown", []).append(label)

    for name in expected:
        entry = deployed.get(name) if isinstance(deployed, dict) else None
        add(entry.get("commit", "") if isinstance(entry, dict) else "", name)
    for vm, commit in sorted(snapshots.items()):
        groups.setdefault(commit or "no snapshot", []).append(f"{vm} (snapshot)")
    for app, commit in sorted(running.items()):
        add(commit, f"{app} (running)")
    if len(groups) == 1 and checkout in groups and not checkout.endswith("-dirty"):
        return "on", [f"system: on {checkout}"]
    state = "unknown" if checkout == "unknown" else "mixed"  # not a checkout: nothing to compare with

    def said(labels):
        return ", ".join(labels) if len(labels) <= 5 else f"{len(labels)} components"

    order = sorted(groups.items(), key=lambda item: (item[0] in ("unknown", "no snapshot"), -len(item[1]), item[0]))
    return state, [f"system: {state}", *(f"  {commit}: {said(labels)}" for commit, labels in order), f"  checkout: {checkout}"]


# -- copies that disagree with their source (T11) -----------------------------------------------

MANAGED_POOLS = ("sandbox", "devbox", "persistent", "ephemeral", "private_internet")


def state_vms(text: str) -> dict[int, str]:
    """{vmid: name} of the Proxmox VMs an OpenTofu state holds (a terraform.tfstate's text)."""
    import json

    try:
        state = json.loads(text) if text.strip() else {}
    except ValueError:
        return {}
    found = {}
    for resource in state.get("resources", []) if isinstance(state, dict) else []:
        if resource.get("type") != "proxmox_virtual_environment_vm":
            continue
        for instance in resource.get("instances", []):
            attributes = instance.get("attributes") or {}
            if str(attributes.get("vm_id", "")).isdigit():
                found[int(attributes["vm_id"])] = str(attributes.get("name", ""))
    return found


def version_row(state: str) -> list[Row]:
    if state != "mixed":  # "on", or not known (the host not read)
        return []
    return [Row("versions", "warn", "the system runs more than one commit (the version block above)",
                "`worms apply` brings what changed to this checkout; `worms apply <component>` one")]  # fmt: skip


def devbox_map_rows(host_hash: str, held: dict) -> list[Row]:
    """The devbox map on scoring and logging (`held`: {VM: its sha256, "" without one; a VM not
    asked is left out}) against the host's (devbox_map.PATH: `host_hash`, "" without one)."""
    if not host_hash:
        return []
    other = sorted(vm for vm, digest in held.items() if digest != host_hash)
    if not other:
        return []
    return [Row("devbox map", "error", f"{', '.join(other)} hold another devbox map than the host's: their devbox "
                "submissions are attributed by an old one, or to no team", "`worms step devbox-map` pushes it again")]  # fmt: skip


def current_team_rows(teams: dict, round_runs: bool) -> list[Row]:
    """`current-team` on scoring and logging (`teams`: {VM: its content}) while no round's attempt
    runs: their submissions are recorded for a team that is not playing."""
    named = sorted(vm for vm, team in teams.items() if team.strip())
    if round_runs or not named:
        return []
    team = teams[named[0]].strip()
    return [Row("current team", "warn", f"{', '.join(named)} still name a round's team ({team[:8]}…) while no round "
                "runs", "`worms round teardown` clears it")]  # fmt: skip


def sandbox_rows(pool: set, state: set, generated: str, unfinished: bool) -> list[Row]:
    """The round's sandbox, coherent: no VM in the pool, none in its state, no definition and no
    unfinished build; or a kept one, the same VMs in the pool and the state, and its definition."""
    problems = []
    if pool - state:
        problems.append(f"VMs in the pool no state holds ({', '.join(map(str, sorted(pool - state)))})")
    if state - pool:
        problems.append(f"VMs its state holds that Proxmox lacks ({', '.join(map(str, sorted(state - pool)))})")
    if not pool and not state and generated:  # the gameplan generated_vms.tf was written from
        problems.append(f"generated_vms.tf still defines {generated}'s sandbox with none built: an apply there builds "
                        "it again")  # fmt: skip
    if not pool and not state and unfinished:
        problems.append("an unfinished build is recorded with no VM")
    if not problems:
        return []
    return [Row("sandbox", "error", "; ".join(problems), "`worms round teardown`")]


def orphans(vms: list, managed: dict) -> list:
    """VMs Proxmox has in WORMS's pools (or named like a sandbox's or devbox's) that no OpenTofu
    state holds (`managed`: {vmid: name} of every state): no firewall or record follows them."""
    return [vm for vm in vms
            if (vm.get("pool") in MANAGED_POOLS or str(vm.get("name", "")).startswith(("sandbox-", "devbox-")))
            and int(vm["vmid"]) not in managed]  # fmt: skip


def orphan_rows(vms: list, managed: dict) -> list[Row]:
    """The `orphan VMs` row (`orphans`); `worms round teardown` offers the sandbox's for destruction."""
    orphans_ = sorted(vm["name"] for vm in orphans(vms, managed))
    if not orphans_:
        return []
    sandbox = [name for name in orphans_ if name.startswith("sandbox-")]
    return [Row("orphan VMs", "error", f"in no OpenTofu state: {', '.join(orphans_)}",
                ("`worms round teardown` for the sandbox's; " if sandbox else "") + "the rest: look first (`worms show <vm>`)")]  # fmt: skip


def backup_rows(jobs: list, vms: list) -> list[Row]:
    """Each backup job that names VM IDs: every one exists (a rebuilt VM gets a new one)."""
    there = {int(vm["vmid"]) for vm in vms}
    rows = []
    for job in jobs if isinstance(jobs, list) else []:
        ids = [int(i) for i in str(job.get("vmid") or "").split(",") if i.strip().isdigit()]
        missing = [i for i in ids if i not in there]
        if missing:
            rows.append(Row("backup job", "error", f"{job.get('id', '?')} backs up VM IDs that do not exist "
                            f"({', '.join(map(str, missing))})", "`worms apply` (the job by pool: T3)"))  # fmt: skip
    return rows


def drift_rows(plans: dict) -> list[Row]:
    """`worms status --deep`: each controller stack's `tofu plan -detailed-exitcode` (2: changes)."""
    changed = sorted(stack for stack, code in plans.items() if code == 2)
    failed = sorted(stack for stack, code in plans.items() if code not in (0, 2))
    rows = []
    if changed:
        rows.append(Row("drift", "warn", f"OpenTofu would change {', '.join(changed)}", "`worms apply --plan` says what"))
    if failed:
        rows.append(Row("drift", "unknown", f"no plan for {', '.join(failed)}", "`worms apply --plan` says why"))
    return rows


def outputs_rows(controller: dict, published: dict) -> list[Row]:
    """The platform's and the firewall's outputs the host's stacks read (T2), against this
    checkout's states: `controller` {stack: its state's serial}, `published` {stack: the serial
    published on the host, "" for none}. A checkout without the state (a laptop that never
    applied it) compares nothing."""
    stale = sorted(stack for stack, serial in controller.items()
                   if serial is not None and str(published.get(stack, "")) != str(serial))  # fmt: skip
    if not stale:
        return []
    said = ", ".join(f"{stack} (state serial {controller[stack]}, published {published.get(stack) or 'none'})" for stack in stale)
    return [Row("host outputs", "warn", f"the host's stacks read older outputs of {said}", "`worms step outputs`")]

