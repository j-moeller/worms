"""`worms start|stop <target>...`: VMs on and off with `qm` on the node, by the names `worms
run` takes (worms_cli/remote.py: a VM, `<team>/<role>`, a group of platform VMs). And the devbox
VMs that something else stopped, which are written down on the node:

    held        a round with "shut down every devbox" (round agent, hold_devboxes:
                /var/lib/worms/held-devboxes.json); its release job starts them again.
                `worms start --held` starts them by hand, once no round is on.
    window      `worms open window` shut them down (WINDOW_STOPPED); `worms close window`
                starts them again once the window is closed.

No team VM is started while the internet window is open, and a sandbox VM is the round agent's:
neither started nor stopped here (`worms emergency-stop` stops everything).
"""

from __future__ import annotations

import json
import shlex

from worms_cli import output
from worms_cli.build import BuildError

# scripts/round_agent/handlers.py, held_devboxes_path (the agent's state_dir, /var/lib/worms).
HELD_DEVBOXES = "/var/lib/worms/held-devboxes.json"
# The devbox VMs `worms open window` shut down, for `worms close window` to start again.
WINDOW_STOPPED = "/var/lib/worms/window-stopped-devboxes.json"
INTERNET_FLAG = "/run/worms/internet-allowed"
SHUTDOWN = "qm shutdown {vmid} --timeout 120 --forceStop 1"  # forced after 120 s: a team's VM may ignore it


NO_RECORD = 3  # read_record's exit code for "there is none"


def read_record(runner, path: str) -> list[dict]:
    """The VMs a record on the node lists ([{vmid, name}]); [] when there is none. A BuildError
    when it cannot be read: that says nothing about what it holds."""
    code, out, err = runner.session.node_run(["sh", "-c", f"[ -e {shlex.quote(path)} ] || exit {NO_RECORD}; cat {shlex.quote(path)}"])
    if code == NO_RECORD:
        return []
    if code != 0:
        raise BuildError(f"cannot read {path} on the node: {err.strip()[:200] or f'exit {code}'}")
    try:
        data = json.loads(out or "{}")
    except ValueError as exc:
        raise BuildError(f"{path} on the node is not JSON: {exc}") from None
    vms = data.get("vms", []) if isinstance(data, dict) else data
    return [{"vmid": int(vm["vmid"]), "name": str(vm["name"])} for vm in vms if isinstance(vm, dict) and "vmid" in vm and "name" in vm]


def write_record(runner, path: str, vms: list[dict]) -> None:
    """Write (or, with no VM, remove) a record on the node, root's only."""
    if not vms:
        runner.node_sh(f"rm -f {shlex.quote(path)}", quiet=True)
        return
    text = json.dumps({"vms": [{"vmid": int(vm["vmid"]), "name": vm["name"]} for vm in vms]})
    directory = path.rsplit("/", 1)[0]
    runner.node_sh(f"mkdir -p {shlex.quote(directory)} && umask 077 && printf '%s\\n' {shlex.quote(text)} > {shlex.quote(path)}",
                   quiet=True)  # fmt: skip


def still_there(runner, recorded: list[dict]) -> list[dict]:
    """The recorded VMs that are still there (same vmid and name: a vmid may have been given to
    another VM since) and stopped."""
    there = {int(vm["vmid"]): vm for vm in runner.vms()}
    return [there[r["vmid"]] for r in recorded if r["vmid"] in there and there[r["vmid"]]["name"] == r["name"]
            and there[r["vmid"]].get("status") == "stopped"]  # fmt: skip


def start(runner, vms: list[dict]) -> list[str]:
    """`qm start` each; the names of those that did not start."""
    failed = [vm["name"] for vm in vms if runner.node_sh(f"qm start {int(vm['vmid'])}", check=False) != 0]
    if failed:  # `qm start` fails on a VM that runs already
        running = {vm["name"] for vm in runner.vms() if vm.get("status") == "running"}
        failed = [name for name in failed if name not in running]
    return failed


class Power:
    def __init__(self, config, catalogue=None):
        from worms_cli.remote import Remote

        self.remote = Remote(config, catalogue)
        self.runner = self.remote.catalogue.runner

    def _targets(self, names) -> list[dict]:
        targets = self.remote.targets(names)
        if any(t.vm is None for t in targets):
            raise BuildError("the node is not switched on or off from here")
        sandboxes = [t.name for t in targets if t.vm.get("pool") == "sandbox"]
        if sandboxes:
            raise BuildError(f"{', '.join(sandboxes)}: a round's sandbox VM, the round agent's to start and stop "
                             "(`worms emergency-stop` stops every VM)")  # fmt: skip
        return [t.vm for t in targets]

    def stop(self, names) -> int:
        failed = []
        for vm in self._targets(names):
            if vm.get("status") == "stopped":
                print(output.status_cell("stopped", f"{vm['name']}: stopped already"))
                continue
            if self.runner.node_sh(SHUTDOWN.format(vmid=int(vm["vmid"])), check=False) != 0:
                failed.append(vm["name"])
            else:
                print(output.status_cell("stopped", f"{vm['name']}: stopped"))
        if failed:
            output.error(f"did not stop: {', '.join(failed)}")
            return 1
        return 0

    def start(self, names, held: bool = False) -> int:
        if held:
            return self.start_held()
        vms = self._targets(names)
        team = [vm for vm in vms if vm.get("pool") == "devbox"]
        if team:
            self._refuse_while_window("start a devbox VM")
            holds = {r["vmid"] for r in read_record(self.runner, HELD_DEVBOXES)}
            on_hold = [vm["name"] for vm in team if int(vm["vmid"]) in holds]
            if on_hold:
                raise BuildError(f"{', '.join(on_hold)}: held shut down by a round with devboxes off ({HELD_DEVBOXES}): "
                                 "its release starts them; once no round is on, `worms start --held`")  # fmt: skip
        stopped = [vm for vm in vms if vm.get("status") != "running"]
        for vm in vms:
            if vm.get("status") == "running":
                print(output.status_cell("running", f"{vm['name']}: running already"))
        failed = start(self.runner, stopped)
        for vm in stopped:
            if vm["name"] not in failed:
                print(output.status_cell("running", f"{vm['name']}: started"))
        if failed:
            output.error(f"did not start: {', '.join(failed)} (`worms logs {failed[0]}` once it is up, or the node's task log)")
            return 1
        return 0

    def start_held(self) -> int:
        """The devbox VMs a round holds shut down, started by hand: refused while the round is
        on (a sandbox VM exists) or the window is open. The record stays: the round agent takes
        no devbox job until the round's release removes it."""
        self._refuse_while_window("start the held devbox VMs")
        sandboxes = sorted(vm["name"] for vm in self.runner.vms() if vm.get("pool") == "sandbox")
        if sandboxes:
            raise BuildError(f"a round is on ({len(sandboxes)} sandbox VM(s)): its release starts the held devboxes "
                             "after its last attempt")  # fmt: skip
        recorded = read_record(self.runner, HELD_DEVBOXES)
        if not recorded:
            print(f"no devbox VM is held ({HELD_DEVBOXES} is not there)")
            return 0
        vms = still_there(self.runner, recorded)
        failed = start(self.runner, vms)
        print(output.status_cell("ok" if not failed else "error", f"started {len(vms) - len(failed)} of {len(recorded)} held devbox VM(s)"
                                 + (f"; not: {', '.join(failed)}" if failed else "")))  # fmt: skip
        print(output.color(f"The hold stays recorded ({HELD_DEVBOXES}): the round agent takes no devbox job until the "
                           f"round releases it. If that round is over for good: `worms run node -- rm {HELD_DEVBOXES}`", "dim"))  # fmt: skip
        return 1 if failed else 0

    def _refuse_while_window(self, what: str) -> None:
        if self.runner.flag_exists(INTERNET_FLAG):
            raise BuildError(f"refusing to {what} while internet access is allowed: `worms close window` first")
