"""`worms run <target>... [--root] -- <command>`: one command on one VM or several, without a
login. (`worms login <vm> -- <command>` is the same for one VM.)

    worms run dashboard -- systemctl is-active dashboard nginx
    worms run services -- df -h /            every service VM, one prefix per VM
    worms run node -- qm list                the Proxmox node
    worms run alpha/a --root -- ls /root     a team's VM: only when named

A target is `node`, a VM by its name, an alias or `<team>/<role>` (worms_cli/teams.py), or a
group: `infrastructure`, `services`, `private-internet` (worms_cli/components.py, GROUPS). A group
holds platform VMs only: a team's VM (devbox, sandbox) runs a command only when it is named
itself.

It goes as `worms login` does, never interactive (BatchMode, no terminal, no prompt): the WORMS
key in its own agent, never forwarded; the node over the session's SSH (here: a local shell).
The words after `--` are joined with spaces and run by the VM's shell, as ssh does; `--root`
runs them with `sudo -n sh -c`. Each VM's output comes as it comes, one `[<vm>]` prefix per line
when there are several; what a team's VM prints is escaped (it is assumed rooted by its team).
The exit code is the worst of them.
"""

from __future__ import annotations

import codecs
import os
import shlex
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from worms_cli import output
from worms_cli.build import BuildError

STOPPED = 1  # the exit code of a VM that is not running (it is not started for this)


@dataclass(frozen=True)
class Target:
    name: str
    vm: dict | None  # None: the node
    untrusted: bool = False


def groups() -> dict[str, tuple[str, ...]]:
    """The groups `worms run` takes: the build's VM groups, platform VMs only."""
    from worms_cli.components import COMPONENTS, GROUPS

    return {name: tuple(n for n in members if COMPONENTS[n].kind == "vm") for name, (members, _) in GROUPS.items()}


def escape(text: str) -> str:
    """What a team's VM prints, safe for this terminal: control characters escaped, `\\r` too
    (it could overwrite another VM's prefix); lines and tabs kept."""
    from worms_cli.rsync import printable

    return printable(text).replace("\r", "\\r")


def command_line(words) -> str:
    """The command line for the VM's shell: one word as it is (pipes, redirections: `worms run dns --
    'journalctl -n 50 | grep named'`), several words each quoted, so they arrive as typed."""
    words = list(words)
    return (words[0] if len(words) == 1 else shlex.join(words)).strip()


class Remote:
    def __init__(self, config, catalogue=None):
        from worms_cli.vms import UNTRUSTED_POOLS, Catalogue

        self.config = config
        self.catalogue = catalogue or Catalogue(config)
        self.session = self.catalogue.runner.session
        self.untrusted_pools = UNTRUSTED_POOLS
        self._lock = threading.Lock()

    def targets(self, names) -> list[Target]:
        """The VMs `names` mean, each once, in the order given; a BuildError for one that is not
        there, before anything runs."""
        known = groups()
        out: dict[str, Target] = {}
        for name in names:
            if name == "node":
                out.setdefault("node", Target("node", None))
            elif name in known:
                there = {vm["name"]: vm for vm in self.catalogue.vms()}
                members = [there[n] for n in known[name] if n in there and there[n].get("pool") not in self.untrusted_pools]
                if not members:
                    raise BuildError(f"no VM of the group {name!r} is there ({', '.join(known[name])})")
                for vm in members:
                    out.setdefault(vm["name"], Target(vm["name"], vm))
            else:
                vm = self.catalogue.resolve(name)
                out.setdefault(vm["name"], Target(vm["name"], vm, vm.get("pool") in self.untrusted_pools))
        return list(out.values())

    def argv(self, target: Target, command: str, root: bool) -> tuple[list[str], dict]:
        if target.vm is None:
            if self.session.laptop:
                self.session.check_node()
                return self.session.ssh_argv(command), self.session.env()
            return ["sh", "-c", command], dict(os.environ)
        if root:
            command = f"sudo -n sh -c {shlex.quote(command)}"
        return self.catalogue.ssh_argv(target.vm, command, interactive=False)

    def run(self, names, words, root: bool = False) -> int:
        command = command_line(words)
        if not command:
            raise BuildError("which command? `worms run <target>... -- <command>`")
        targets = self.targets(names)
        team = [t.name for t in targets if t.untrusted]
        if team:
            output.warn(f"{', '.join(team)}: a team's VM, assumed rooted by the team: what it prints is escaped")
        several = len(targets) > 1
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            # One VM may read the input: a platform VM this terminal's, a team's VM only what is piped
            # in (what is typed at a terminal could be a secret); several VMs never.
            piped = not sys.stdin.isatty()
            codes = list(pool.map(lambda t: self._one(t, command, root, several, None if not several and (piped or not t.untrusted)
                                                      else subprocess.DEVNULL), targets))  # fmt: skip
        if several:
            failed = [f"{t.name} ({code})" for t, code in zip(targets, codes) if code]
            print(output.color(f"{len(targets) - len(failed)} of {len(targets)} ok" + (f"; exit codes: {', '.join(failed)}" if failed else ""),
                               "dim" if not failed else "yellow"), file=sys.stderr)  # fmt: skip
        return max(codes)

    def _one(self, target: Target, command: str, root: bool, several: bool, stdin) -> int:
        prefix = f"[{target.name}] " if several else ""
        if target.vm is not None and target.vm.get("status") != "running":
            self._write(sys.stderr, f"{prefix}{target.name} is {target.vm.get('status')}: not run (`qm start {target.vm['vmid']}` on the node)\n")
            return STOPPED
        try:
            argv, env = self.argv(target, command, root)
        except Exception as exc:  # noqa: BLE001  (no address in the inventories, the node out of reach)
            self._write(sys.stderr, f"{prefix}{exc}\n")
            return 255
        proc = subprocess.Popen(argv, env=env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        relays = [threading.Thread(target=self._relay, args=(proc.stdout, sys.stdout, prefix, target.untrusted), daemon=True),
                  threading.Thread(target=self._relay, args=(proc.stderr, sys.stderr, prefix, target.untrusted), daemon=True)]  # fmt: skip
        for relay in relays:
            relay.start()
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
            code = 130
        for relay in relays:
            relay.join()
        return code

    def _relay(self, pipe, sink, prefix: str, untrusted: bool) -> None:
        """Copy a VM's output to ours as it comes: whole lines with a prefix when there are
        several VMs, escaped when the VM is a team's."""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        fd = pipe.fileno()
        while chunk := os.read(fd, 65536):
            text = decoder.decode(chunk)
            text = escape(text) if untrusted else text
            if not prefix:
                self._write(sink, text)
                continue
            pending += text
            *lines, pending = pending.split("\n")
            if lines:
                self._write(sink, "".join(f"{prefix}{line}\n" for line in lines))
        rest = (escape(decoder.decode(b"", final=True)) if untrusted else decoder.decode(b"", final=True))
        if pending + rest:
            self._write(sink, f"{prefix}{pending}{rest}\n" if prefix else rest)

    def _write(self, sink, text: str) -> None:
        with self._lock:
            sink.write(text)
            sink.flush()
