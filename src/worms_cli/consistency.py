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
