"""`worms rsync <source>... <target>`: rsync between here and a VM, as `rsync -avz` does, over the
VM's management SSH (as `worms login` reaches it, with the WORMS key).

The arguments are rsync's own, with a VM's login name (`worms login --help`) for the host:

  worms rsync team-1-backup/ devbox-t1-a:      up: the directory's contents into the home
  worms rsync devbox-t2-a: team-2-backup/      down: the home's contents into the directory
  worms rsync debian@dashboard:logs .          as another user: the path is from their home

As in rsync, an argument is on a VM when it has a ":" with no "/" before it (`./a:b` is a local
file), a later source `:path` is on the first source's VM, a trailing "/" on a source copies its
contents rather than itself, and `vm:` alone is the home. rsync gets these as they are, with only
the VM's name changed to its management address; rsync's daemon syntax (`host::`, `rsync://`) is
refused. The VM's rsync runs as the user before the "@" (sudo), from that user's home: root's on
a devbox, where the team works as root, the login user's (`debian`) elsewhere, root's on the node.

A devbox or sandbox VM is assumed rooted by its team, and the CLI often runs on the node as root,
so with one of them this end of the transfer is guarded: downloaded files get no setuid bits, no
owner of the VM's and no device files (--no-perms keeps their other permissions), and symbolic
links are kept inert (--munge-links) on the way down and restored on the way up, so a backup
goes back as it came. What the VM prints is escaped before the terminal shows it.
"""

from __future__ import annotations

import codecs
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass

from worms_cli import output, teams
from worms_cli.build import BuildError
from worms_cli.vms import UNTRUSTED_POOLS, Catalogue

DEFAULT = ["-a", "-v", "-z"]
# --preview: what would be sent, counted (files, their sizes, the bytes), nothing sent.
PREVIEW = ["-a", "-z", "--dry-run", "--stats", "--human-readable"]
# The receiving end of a download from a team's VM (see the docstring). Options given after
# `--` come later, and so can override these, knowingly.
GUARD_DOWN = ["--munge-links", "--no-perms", "--no-owner", "--no-group", "--no-devices", "--no-specials"]
GUARD_UP = ["--munge-links"]  # unmunges what a download munged
# Ours to set: the transport and the remote command.
REFUSED = ("-e", "--rsh", "--rsync-path", "--daemon", "--server", "--sender")
# A team's VM as `[<user>@]<team>/<role>:<path>` (Rsync._team_spec).
TEAM_TARGET = re.compile(r"(?:([^/:@]+)@)?([^/:@]+/(?:a|b|c|gw)):(.*)", re.S)

# The VM's end, as `--rsync-path`: rsync as user $1, from $1's home.
REMOTE = r"""
u=$1; shift
home=$(getent passwd -- "$u" | cut -d: -f6)
if [ -z "$home" ]; then echo "worms rsync: no user $u here" >&2; exit 3; fi
run='cd -- "$1" || exit 3; shift; exec rsync "$@"'
if [ "$(id -un)" = "$u" ]; then exec sh -c "$run" rsync "$home" "$@"; fi
if [ "$(id -u)" -eq 0 ]; then exec runuser -u "$u" -- sh -c "$run" rsync "$home" "$@"; fi
exec sudo -n -u "$u" -- sh -c "$run" rsync "$home" "$@"
"""


@dataclass(frozen=True)
class Spec:
    """One argument: a local path, or a path on a VM."""

    arg: str
    vm: str | None = None  # the login name as typed; "" for `:path` (the first source's VM)
    user: str | None = None  # before the "@"
    path: str = ""

    @property
    def remote(self) -> bool:
        return self.vm is not None


def parse(arg: str) -> Spec:
    """As rsync reads an argument: on a host when a ":" comes before any "/"."""
    if arg.lower().startswith("rsync://"):
        raise BuildError(f"{arg}: rsync's daemon syntax; a VM is `<vm>:<path>`")
    colon = arg.find(":")
    if colon < 0 or "/" in arg[:colon]:
        return Spec(arg)
    host, path = arg[:colon], arg[colon + 1 :]
    if path.startswith(":"):
        raise BuildError(f"{arg}: rsync's daemon syntax (`host::module`); a VM is `<vm>:<path>`")
    user, _, vm = host.rpartition("@")
    return Spec(arg, vm, user or None, path)


@dataclass(frozen=True)
class Plan:
    """What rsync is given: (the arguments, where they go)."""

    argv: list
    env: dict
    vm: str  # the VM's own name
    user: str
    down: bool  # from the VM
    guarded: bool
    devbox: bool = False


class Rsync:
    def __init__(self, config):
        self.config = config
        self.catalogue = Catalogue(config)
        self.session = self.catalogue.runner.session

    def plan(self, args: list[str], options: list[str], preview: bool = False) -> Plan:
        if len(args) < 2:
            raise BuildError("worms rsync <source>... <target>: a source and a target")
        for option in options:
            # -e, also in a cluster (-avze ssh); not a filter rule's argument (-f "- secret").
            if option.split("=", 1)[0] in REFUSED or re.match(r"-[A-Za-z]*e", option):
                raise BuildError(f"{option}: `worms rsync` sets the transport and the remote rsync itself")
        specs = [self._team_spec(parse(arg)) for arg in args]
        named = [s for s in specs if s.vm]
        if named:
            for s in named:
                if not self.catalogue.knows(s.vm):
                    raise BuildError(f"{s.arg}: no VM {s.vm!r} (`worms rsync --help` lists them); "
                                     f"a local path with a \":\" is ./{s.arg}")  # fmt: skip
        *sources, target = specs
        if target.remote and target.vm == "":
            raise BuildError(f"{target.arg}: a `:path` is on the first source's VM; the target names its own")
        if target.remote:
            if any(s.remote for s in sources):
                raise BuildError("the source and the target cannot both be on a VM: copy through here")
            side, down = target, False
        elif sources[0].remote:
            for s in sources[1:]:
                if not s.remote:
                    raise BuildError(f"{s.arg}: a local source after a VM's; a path on that VM is `:{s.arg}`")
                if s.vm and (s.vm, s.user) != (sources[0].vm, sources[0].user):
                    raise BuildError(f"{s.arg}: every source has to be on the same VM, as the same user")
            if sources[0].vm == "":
                raise BuildError(f"{sources[0].arg}: `:path` follows a source on a VM; the first one names it")
            side, down = sources[0], True
        else:
            if any(s.remote for s in sources):
                raise BuildError("a VM's path comes first among the sources (or is the target)")
            raise BuildError("neither the target nor the sources are on a VM: that is rsync's own job")
        return self._resolve(specs, side, down, [*(PREVIEW if preview else DEFAULT)], options)

    def _team_spec(self, spec: Spec) -> Spec:
        """`[<user>@]<team>/<role>:<path>` (`alpha/a:notes`) names a team's VM, though rsync reads a
        "/" before the ":" as a local path: when that team is there and no such local file is.
        `./alpha/a:notes` stays local."""
        match = TEAM_TARGET.fullmatch(spec.arg)
        if spec.remote or match is None or os.path.lexists(spec.arg):
            return spec
        try:
            if teams.target(self.catalogue.teams(), match.group(2)) is None:
                return spec
        except ValueError:
            return spec
        return Spec(spec.arg, match.group(2), match.group(1) or None, match.group(3))

    def _resolve(self, specs: list[Spec], side: Spec, down: bool, base: list[str], options: list[str]) -> Plan:
        env = self.session.env()
        if side.vm == "node":
            user = side.user or "root"
            if not self.session.laptop:  # the node is here: its paths are local ones
                home = _home(user)
                argv = [_local(home, s.path) if s.remote else s.arg for s in specs]
                return Plan(["rsync", *base, *options, "--", *argv], env, "node", user, down, False)
            self.session.check_node()
            ssh, destination, guarded, host = ["ssh", *self.session.ssh_opts()], self.config.ssh, False, {}
        else:
            vm = self.catalogue.resolve(side.vm)
            if vm.get("status") != "running":
                raise BuildError(f"{vm['name']} is {vm.get('status')}: start it first (`qm start {vm['vmid']}` on the node)")
            host = self.catalogue.hosts().get(vm["name"], {})
            user = side.user or ("root" if host.get("group") == "devbox" else host.get("ansible_user", "debian"))
            ssh = ["ssh", *self.catalogue.ssh_options(interactive=False), "-o", "ServerAliveInterval=15"]
            destination, guarded = self.catalogue.destination(vm), vm.get("pool") in UNTRUSTED_POOLS
            side = Spec(side.arg, vm["name"], user, side.path)
        argv = [f"{destination}:{s.path}" if s.remote else s.arg for s in specs]
        guard = (GUARD_DOWN if down else GUARD_UP) if guarded else []
        rsync_path = shlex.join(["sh", "-c", REMOTE, "worms-rsync", user])
        command = ["rsync", *base, *guard, "-e", shlex.join(ssh), f"--rsync-path={rsync_path}", *options, "--", *argv]
        return Plan(command, env, side.vm, user, down, guarded, host.get("group") == "devbox")

    def run(self, args: list[str], options: list[str], preview: bool = False) -> int:
        if not shutil.which("rsync"):
            raise BuildError("rsync is not installed here")
        plan = self.plan(args, options, preview)
        where = f"{plan.user}@{plan.vm}"
        print(output.color(f"{'from' if plan.down else 'to'} {where}: "
                           + ("a dry run, counted" if preview else f"rsync {' '.join(DEFAULT)}")
                           + (" (a team's VM: guarded, `worms rsync --help`)" if plan.guarded else ""), "dim"),
              flush=True)  # fmt: skip
        proc = subprocess.Popen(plan.argv, env=plan.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        relays = [_Relay(proc.stdout, sys.stdout), _Relay(proc.stderr, sys.stderr)]
        try:
            code = proc.wait()
        except KeyboardInterrupt:  # rsync has it too, and cleans up
            proc.wait()
            code = 130
        for relay in relays:
            relay.join()
        if code in (12, 127) and any("rsync" in line and "not found" in line for line in relays[1].tail):
            output.error(f"rsync is not installed on {plan.vm}"
                         + (": a devbox gets it when it is built again (`worms vm rebuild devboxes`, devbox_packages)" if plan.devbox else ""))  # fmt: skip
        return code


def _home(user: str) -> str:
    import pwd

    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        raise BuildError(f"no user {user} here") from None


def _local(home: str, path: str) -> str:
    """`node:<path>` on the node itself: a path from the user's home, as the remote end takes it
    (`node:` alone is the home's contents, as `host:` is)."""
    if path.startswith("/"):
        return path
    return os.path.join(home, path) if path else home.rstrip("/") + "/"


def printable(text: str) -> str:
    """Terminal control characters escaped, but lines and rsync's progress (\\r) kept."""
    return "".join(c if c.isprintable() or c in "\n\r\t" else c.encode("unicode_escape").decode("ascii") for c in text)


class _Relay(threading.Thread):
    """Copies rsync's output to ours as it comes, escaped; keeps the last lines (for a hint)."""

    def __init__(self, pipe, sink):
        super().__init__(daemon=True)
        self.pipe, self.sink, self.tail = pipe, sink, []
        self.start()

    def run(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = self.pipe.fileno()
        while chunk := os.read(fd, 65536):
            text = printable(decoder.decode(chunk))
            self.sink.write(text)
            self.sink.flush()
            self.tail = (self.tail + text.splitlines())[-5:]
