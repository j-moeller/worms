"""Each VM's history: when it was created, configured, snapshotted, rolled back, destroyed, by
which code (its commit) and by what (`worms apply`, a host job). `worms status` shows each VM's
creation and last update, `worms show <vm>` all of it.

    /var/lib/worms/vm-history.jsonl     on the node, root's only (0600), append-only

One JSON object per line: {"time", "vmid", "name", "event", "commit", "by", "detail"}, time in
UTC. Events: EVENTS. It lives on the node only: sandbox and devbox VMs are assumed rooted by
their team, so nothing a VM says about itself is taken. Two writers may append at once, the CLI
and the round agent: each takes an exclusive flock(2) on the file and writes whole lines in one
write (flock(1) does the same from a laptop, over the node session). Writing it is best effort:
a build never fails for its history.

The commit is the controller's checkout (`git rev-parse --short=7 HEAD`, "-dirty" when tracked
files differ from it). The round agent's workspace has no .git: the workspace sync writes the
controller's commit into it (.worms-commit, ansible role round_agent, workspace.yml).

A VM older than the ledger has no "created" line: Proxmox's own creation time stands in
(`meta: ... ctime=<epoch>` in its configuration), with the commit unknown.
"""

from __future__ import annotations

import json
import os
import shlex
from datetime import datetime, timezone

LEDGER = "/var/lib/worms/vm-history.jsonl"
EVENTS = ("created", "recreated", "configured", "packages", "snapshot", "rolled_back", "destroyed")
CREATED = ("created", "recreated")
UPDATED = ("configured", "packages", "rolled_back")  # what `worms status` calls the last update
COMMIT_FILE = ".worms-commit"


def who() -> str:
    """What did it: the agent's job (WORMS_HISTORY_BY, set while it runs one) or the CLI's command."""
    return os.environ.get("WORMS_HISTORY_BY") or "worms"


def entry(vmid, name, event, commit, by, detail="") -> str:
    assert event in EVENTS, event
    return json.dumps({"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "vmid": int(vmid), "name": str(name),
                       "event": event, "commit": str(commit), "by": str(by)[:120], "detail": str(detail)[:300]},
                      separators=(",", ":"))  # fmt: skip


def append(lines, path: str = LEDGER) -> None:
    """Append on this machine (the node): an exclusive lock, whole lines, one write."""
    import fcntl

    if not lines:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, "".join(f"{line}\n" for line in lines).encode())
    finally:
        os.close(fd)  # and with it the lock


def append_command(lines, path: str = LEDGER) -> str:
    """The same as a shell command for the node (from a laptop): flock(1) takes the same lock."""
    text = "".join(f"{line}\n" for line in lines)
    write = f"printf %s {shlex.quote(text)} >> {shlex.quote(path)}"
    return f"mkdir -p {shlex.quote(os.path.dirname(path))} && umask 077 && flock {shlex.quote(path)} sh -c {shlex.quote(write)}"


def vmid_filter(vmid) -> str:
    """What every line of one VM's history holds (for grep -F on the node)."""
    return f'"vmid":{int(vmid)},'


def parse(text: str) -> list[dict]:
    """The ledger's events, oldest first; a line that is not one is skipped."""
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("vmid"), int) and event.get("event") in EVENTS:
            events.append(event)
    return events


def unfinished(events: list[dict], last: str) -> set[str]:
    """The VMs whose latest creation was not followed by `last`, the last playbook that configures
    them: the build that created them stopped before its end. A VM older than the ledger has no
    creation in it, and is not one."""
    pending: dict[int, str] = {}
    for event in events:
        if event["event"] in CREATED:
            pending[event["vmid"]] = event["name"]
        elif event["event"] == "destroyed" or (event["event"] in ("configured", "packages") and event.get("detail") == last):
            pending.pop(event["vmid"], None)
    return set(pending.values())


def summary(events: list[dict]) -> dict[int, dict]:
    """{vmid: {"name", "created": event, "updated": event}}: the VM's latest creation, and its
    latest update since. A vmid destroyed and created again starts over."""
    out: dict[int, dict] = {}
    for event in events:
        vm = out.setdefault(event["vmid"], {"name": event["name"], "created": None, "updated": None})
        if event["event"] in CREATED or event["event"] == "destroyed" or event["name"] != vm["name"]:
            vm.update(name=event["name"], created=event if event["event"] in CREATED else None, updated=None)
        elif event["event"] in UPDATED:
            vm["updated"] = event
    return out


def ctime(meta: str) -> str | None:
    """Proxmox's creation time of a VM from its `meta:` line (`creation-qemu=...,ctime=<epoch>`)."""
    for part in meta.split(","):
        key, _, value = part.strip().partition("=")
        if key == "ctime" and value.isdigit():
            return datetime.fromtimestamp(int(value), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def cell(event: dict | None) -> str:
    """`11-19 abc1234`: a date and a commit, for a table."""
    if not event:
        return "—"
    return f"{str(event.get('time', ''))[5:10]} {str(event.get('commit') or '?')[:13]}"


def grouped(events: list[dict]) -> list[list[str]]:
    """`worms show <vm>`'s rows, newest first: one run's events of one kind together (a build
    configures a VM with a dozen playbooks: one row, its playbooks in the detail)."""
    rows: list[list] = []
    for event in events:
        key = (event["event"], event.get("commit"), event.get("by"))
        if rows and rows[-1][0] == key:
            rows[-1][1] = event
            if event.get("detail") and event["detail"] not in rows[-1][2]:
                rows[-1][2].append(event["detail"])
        else:
            rows.append([key, event, [event["detail"]] if event.get("detail") else []])
    return [[str(e.get("time", "")).replace("T", " ").rstrip("Z"), e["event"], str(e.get("commit") or "?"), str(e.get("by") or ""),
             ", ".join(details)[:160]] for _, e, details in reversed(rows)]  # fmt: skip
