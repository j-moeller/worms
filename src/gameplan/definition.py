"""Defining a gameplan: its sandbox and where the worm starts.

A gameplan's `__init__` only describes. It assigns VMs to attributes (they register themselves,
as layers do in a torch module), joins them into networks, opens ports, generates credentials,
places files and names the VM the worm starts on. Nothing is built or contacted here: the
description becomes a spec (`spec()`, plain JSON), which

  - validation checks before a round is scheduled (gameplan/runtime.py, `validate`),
  - the round agent on the node builds into OpenTofu and Ansible (gameplan/build.py),
  - the conductor runs `execute()` against (gameplan/runtime.py, `Runner`).

What a VM runs is defined by its class: `packages`, `services`, `setup` commands and `files`
are installed when the sandbox is built, and every team's run starts from that snapshot.
Credentials and the files written with `Gameplan.write_file` are made fresh for every run.

docs/05-live-environment.md describes the API with an example (gameplans/round_one.py).
"""

from __future__ import annotations

import ipaddress
import posixpath
import random
import re

# The sandbox networks: /24s from this range (opentofu/shared/shared-variables.tf,
# networks.sandbox_vms). Each network's .1 is sandbox-gw, the way to scoring, logging and the
# private internet; VMs get addresses from .10 on.
SANDBOX_NETWORK = ipaddress.ip_network("10.64.0.0/10")
# The sandbox management bridge (vmbr4): the node reaches every VM here, SSH only. .1-.9 are
# reserved (the node is .1, sandbox-gw .2).
MAINTENANCE_NETWORK = ipaddress.ip_network("10.4.0.0/16")
MAINTENANCE_FIRST_HOST = 10
# The OS images a VM can boot from (opentofu/modules/os_images, local.platform.os_images).
IMAGES = ("debian",)
# The login user of every image: the worm runs as this user, and Ansible and the agent log in
# as it.
DEFAULT_USER = "debian"
FLAG_PATH = "/etc/worms/flag"

LIMITS = {
    "vms": 32,
    "networks": 30,  # sandbox-gw has a leg in every network: Proxmox allows 32 NICs a VM
    "nics": 8,  # per VM
    "cores": (1, 4),
    "memory": (256, 8192),  # MB
    "disk": (5, 64),  # GB
    "file_size": 64 * 1024,
    "command_size": 4096,
    "round_duration": (60, 7 * 86400),
}

VM_ATTRIBUTE = re.compile(r"^[a-z](?:[a-z0-9_]{0,39}[a-z0-9])?$")
PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]{1,62}$")
SERVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._-]{0,62}$")
USER = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
KEY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
NETWORK_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
PATH_CHARS = re.compile(r"^[A-Za-z0-9._+@/-]+$")
# Accounts a gameplan cannot create: the image's own, and what the packages above bring.
RESERVED_USERS = {
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news", "uucp", "proxy",
    "www-data", "backup", "list", "irc", "nobody", "sshd", "messagebus", "postgres", "django", DEFAULT_USER,
}  # fmt: skip
KEY_TYPES = ("ed25519", "rsa", "ecdsa")

# A per-run secret inside a file's content: replaced on the node, when the file is written.
SECRET_MARKER = re.compile(r"@@worms-secret:([a-z0-9]+)\.([a-z]+)@@")

Allow = "ACCEPT"
Deny = "DROP"
Incoming = "in"
Outgoing = "out"


class GameplanError(ValueError):
    """The gameplan is not valid, or used the API the wrong way. Before a round: the gameplan
    cannot be scheduled. During a run: the attempt fails like any other error."""


class RoundError(RuntimeError):
    """The infrastructure failed (a host job, the Proxmox API, a check). The attempt is thrown
    away and the team's worm runs again (docs/05-live-environment.md)."""


class Secret(str):
    """A per-run secret, as it stands in a file's content until the node writes the file. Use it
    like a string (an f-string, `+`): the node replaces it with the run's value."""

    def __new__(cls, owner, field):
        return super().__new__(cls, f"@@worms-secret:{owner}.{field}@@")


class SSHKey:
    """A key pair, generated for every run (`Gameplan.generate_ssh_key`). `name` is its file
    name in ~/.ssh; `public` and `private` stand for the run's keys in a file's content."""

    def __init__(self, ident, name, type):
        self.id, self.name, self.type = ident, name, type
        self.public = Secret(ident, "public")
        self.private = Secret(ident, "private")

    def __repr__(self):
        return f"SSHKey({self.name!r})"


class PasswordLogin:
    """A user with a password, generated for every run (`Gameplan.generate_ssh_pwd_login`). The
    user name is fixed; `pwd` (or `password`) stands for the run's password."""

    def __init__(self, ident, user):
        self.id, self.user = ident, user
        self.pwd = self.password = Secret(ident, "password")

    def __repr__(self):
        return f"PasswordLogin({self.user!r})"


class FirewallRule:
    """One rule of a VM's firewall. Everything a rule does not allow is dropped, both ways;
    traffic towards scoring, logging and the private internet is always allowed (through
    sandbox-gw).

        FirewallRule(Allow, 22, Incoming)                  # SSH in, from the VM's networks
        FirewallRule(Allow, 5432, Outgoing, peers=[db])    # to one VM only
        FirewallRule(Allow, (8000, 8100), Incoming)        # a port range
        FirewallRule(Allow, None, Incoming, proto="icmp")  # ping

    peers: VMs and networks; default: every network the VM is in. A rule never reaches beyond
    the sandbox networks."""

    def __init__(self, action, port, direction, proto="tcp", peers=None):
        self.action, self.port, self.direction, self.proto = action, port, direction, proto
        self.peers = None if peers is None else list(peers) if isinstance(peers, (list, tuple)) else [peers]

    def __repr__(self):
        return f"FirewallRule({self.action}, {self.port}, {self.direction}, proto={self.proto!r})"

    def problems(self):
        out = []
        if self.action not in (Allow, Deny):
            out.append(f"{self!r}: the action is Allow or Deny")
        if self.direction not in (Incoming, Outgoing):
            out.append(f"{self!r}: the direction is Incoming or Outgoing")
        if self.proto not in ("tcp", "udp", "icmp"):
            out.append(f"{self!r}: proto is tcp, udp or icmp")
        if self.proto == "icmp" and self.port is not None:
            out.append(f"{self!r}: an icmp rule has no port")
        if self.port is not None and _dport(self.port) is None:
            out.append(f"{self!r}: the port is a number (1-65535), a (low, high) range, or None for all")
        for peer in self.peers or []:
            if not isinstance(peer, (BasicVM, Network)):
                out.append(f"{self!r}: a peer is a VM or a network")
        return out


def _dport(port):
    if isinstance(port, bool):
        return None
    if isinstance(port, int) and 1 <= port <= 65535:
        return str(port)
    if isinstance(port, (tuple, list)) and len(port) == 2 and all(isinstance(p, int) and not isinstance(p, bool) for p in port):
        low, high = port
        if 1 <= low < high <= 65535:
            return f"{low}:{high}"
    return None


class Network:
    """A layer-2 network of the sandbox: one bridge, one /24, the VMs on it. VMs reach each
    other only on networks they share, and only as their firewalls allow."""

    def __init__(self, index, name, vms):
        self.index, self.name, self.vms = index, name, vms

    def __repr__(self):
        return f"Network({self.name!r})"

    @property
    def cidr(self):
        return next(s for i, s in enumerate(SANDBOX_NETWORK.subnets(new_prefix=24)) if i == self.index)

    @property
    def bridge(self):
        return f"vmbr{1000 + self.index}"


class Flag:
    """The flag of a VM, as `Gameplan.remove_flag` returns it: `add_flag` puts a new one there."""

    def __init__(self, vm):
        self.vm = vm

    def __repr__(self):
        return f"Flag({self.vm.name})"


class FileHandle:
    """A file a gameplan declared (`Gameplan.write_file`): written at the start of every run,
    or with `at_reset=False` only when `execute()` writes it."""

    def __init__(self, ident, vm, path, content, mode, owner, at_reset):
        self.id, self.vm, self.path, self.content = ident, vm, path, content
        self.mode, self.owner, self.at_reset = mode, owner, at_reset

    def __repr__(self):
        return f"File({self.vm.name}:{self.path})"


# ---------------------------------------------------------------------------
# VMs
# ---------------------------------------------------------------------------


class BasicVM:
    """A Debian VM. Subclasses describe what else it runs (DjangoVM, PostgreVM below):

        packages  installed from the private apt cache when the sandbox is built
        remove    packages removed then
        files     {path: content} written then (no per-run secrets)
        setup     shell commands then, as root, after the packages and files
        services  systemd units enabled and running then

    The resources are the class's unless given: BasicVM(cores=2, memory=1024, disk=8).
    `install`/`uninstall` add to the packages of one VM.

    In `execute()` a VM is also a handle on its power: start(), stop(), shutdown(),
    suspend(), resume(), status()."""

    image = "debian"
    cores = 1
    memory = 512  # MB: the ceiling; the node may reclaim unused RAM (docs/01-components.md)
    disk = 5  # GB
    packages: tuple = ()
    remove: tuple = ()
    files: dict = {}
    setup: tuple = ()
    services: tuple = ()

    def __init__(self, *, cores=None, memory=None, disk=None, image=None):
        cls = type(self)
        self.cores = cls.cores if cores is None else cores
        self.memory = cls.memory if memory is None else memory
        self.disk = cls.disk if disk is None else disk
        self.image = cls.image if image is None else image
        self._install = list(cls.packages)
        self._uninstall = list(cls.remove)
        self._files = dict(cls.files)
        self._setup = list(cls.setup)
        self._services = list(cls.services)
        self.name = None  # set when it is assigned to the gameplan
        self._plan = None

    def __repr__(self):
        return f"{type(self).__name__}({self.name or 'unregistered'})"

    def _defining(self, what):
        if self._plan is not None and self._plan.phase != "define":
            raise GameplanError(f"{self!r}.{what}() describes the sandbox: call it in __init__")

    def install(self, *packages):
        self._defining("install")
        self._install.extend(packages)

    def uninstall(self, *packages):
        self._defining("uninstall")
        self._uninstall.extend(packages)

    # -- in execute() ----------------------------------------------------------

    def _runtime(self, what):
        if self._plan is None or self._plan.runner is None:
            raise GameplanError(f"{self!r}.{what}() acts on the running sandbox: call it in execute()")
        return self._plan.runner

    def start(self):
        self._runtime("start").power(self, "start")

    def stop(self):
        """Power off at once, as a pulled plug: the worm cannot delay it."""
        self._runtime("stop").power(self, "stop")

    def shutdown(self, timeout=60):
        """Ask the VM to shut down; after `timeout` seconds it is powered off anyway (a worm
        that is root on it can ignore the request)."""
        self._runtime("shutdown").power(self, "shutdown", timeout=timeout)

    def suspend(self):
        """Pause the VM in memory (qm suspend): nothing on it runs until resume()."""
        self._runtime("suspend").power(self, "suspend")

    def resume(self):
        self._runtime("resume").power(self, "resume")

    def status(self):
        """running, paused or stopped."""
        return self._runtime("status").status(self)


# Examples of VMs that run something. A gameplan can define its own the same way.

DJANGO_UNIT = """[Unit]
Description=Django site (gunicorn)
After=network-online.target

[Service]
User=django
WorkingDirectory=/srv/site
ExecStart=/usr/bin/gunicorn --bind 0.0.0.0:8000 worms_site.wsgi
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""


class DjangoVM(BasicVM):
    """A Django project (/srv/site, as `django-admin startproject worms_site` makes it) served
    by gunicorn on port 8000. Open the port with a firewall rule to reach it from other VMs.
    (Not `site`: Python has a module of that name, and django-admin refuses it.)"""

    memory = 1024
    packages = ("python3-django", "gunicorn")
    files = {"/etc/systemd/system/django-site.service": DJANGO_UNIT}
    setup = (
        "id -u django >/dev/null 2>&1 || useradd --system --home-dir /srv/site --shell /usr/sbin/nologin django",
        "test -f /srv/site/manage.py || { mkdir -p /srv/site && django-admin startproject worms_site /srv/site; }",
        "sed -i \"s/^ALLOWED_HOSTS = .*/ALLOWED_HOSTS = ['*']/\" /srv/site/worms_site/settings.py",
        "chown -R django: /srv/site",
    )
    services = ("django-site",)


class PostgreVM(BasicVM):
    """PostgreSQL, listening on the VM's sandbox addresses (port 5432), password logins from the
    sandbox networks. No database or user of its own: add them with `setup`."""

    memory = 1024
    packages = ("postgresql",)
    setup = (
        "for d in /etc/postgresql/*/main; do echo \"listen_addresses = '*'\" > $d/conf.d/worms.conf; "
        "grep -q '^host all all 10.64.0.0/10' $d/pg_hba.conf || echo 'host all all 10.64.0.0/10 scram-sha-256' >> $d/pg_hba.conf; done",
        "systemctl restart postgresql",
    )
    services = ("postgresql",)


# ---------------------------------------------------------------------------
# The gameplan
# ---------------------------------------------------------------------------


def _as_list(value, what):
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, BasicVM):
        return [value]
    raise GameplanError(f"{what}: give a VM or a list of VMs")


def _home(user):
    return "/root" if user == "root" else f"/home/{user}"


def _file_path(path, owner):
    """An absolute, normalised path; `~` is the owner's home."""
    if not isinstance(path, str) or not path or "\x00" in path or "\n" in path or len(path) > 200:
        return None
    if path == "~" or path.startswith("~/"):
        path = _home(owner) + path[1:]
    if not path.startswith("/") or ".." in path.split("/"):
        return None
    path = posixpath.normpath(path)
    if path in ("/", FLAG_PATH) or path.startswith("/etc/worms/") or not PATH_CHARS.fullmatch(path):
        return None
    return path


class _Plan:
    """What a gameplan's __init__ recorded, and the state of a run against it."""

    def __init__(self, gameplan):
        self.gameplan = gameplan
        self.phase = "define"
        self.vms: list[BasicVM] = []
        self.networks: list[Network] = []
        self.rules: list[tuple[list, FirewallRule]] = []
        self.secrets: list = []
        self.setup: list[tuple] = []  # (op, vm, object, user)
        self.files: list[FileHandle] = []
        self.logins: dict[str, list] = {}  # VM name -> PasswordLogins on it
        self.entrypoint = None
        self.runner = None
        self.errors: list[str] = []

    def defining(self, what):
        if self.phase != "define":
            raise GameplanError(f"{what}() describes the sandbox: call it in __init__")

    def register(self, attribute, value):
        self.defining("assigning a VM")
        if not VM_ATTRIBUTE.fullmatch(attribute):
            raise GameplanError(
                f"self.{attribute}: a VM's attribute is lowercase letters, digits and _ (at most 41), "
                "and does not end in _"
            )
        if attribute == "gw":
            raise GameplanError("self.gw: sandbox-gw is the sandbox's router; name the VM differently")
        vms = value if isinstance(value, (list, tuple)) else [value]
        names = [f"sandbox-{attribute.replace('_', '-')}" + (f"-{i}" if isinstance(value, (list, tuple)) else "")
                 for i in range(len(vms))]  # fmt: skip
        for vm, name in zip(vms, names):
            if vm._plan is not None:
                raise GameplanError(f"{vm!r} is assigned twice (as {vm.name} and self.{attribute})")
            if any(other.name == name for other in self.vms):
                raise GameplanError(f"two VMs are called {name}")
            vm.name, vm._plan = name, self
            self.vms.append(vm)

    def known(self, vm, what):
        if not isinstance(vm, BasicVM) or vm._plan is not self:
            raise GameplanError(f"{what}: {vm!r} is not a VM of this gameplan (assign it to self.<name> first)")
        return vm

    # -- the spec ----------------------------------------------------------------

    def spec(self):
        """The sandbox as plain data, or GameplanError with every problem found."""
        errors = list(self.errors)
        if not self.vms:
            errors.append("the gameplan has no VM: assign one to self.<name>")
        if len(self.vms) > LIMITS["vms"]:
            errors.append(f"at most {LIMITS['vms']} VMs")
        if len(self.networks) > LIMITS["networks"]:
            errors.append(f"at most {LIMITS['networks']} networks")
        if self.entrypoint is None:
            errors.append("no worm entry point: call self.set_worm_entrypoint(vm)")
        duration = getattr(self.gameplan, "round_duration", None)
        low, high = LIMITS["round_duration"]
        if isinstance(duration, bool) or not isinstance(duration, int) or not low <= duration <= high:
            errors.append(f"round_duration is whole seconds in [{low}, {high}]")

        addresses = self._addresses()
        vms = []
        for index, vm in enumerate(self.vms):
            errors += self._vm_problems(vm)
            nics = [
                {"network": net.name, "bridge": net.bridge, "address": f"{addresses[(net.name, vm.name)]}/24",
                 "gateway": str(net.cidr.network_address + 1) if i == 0 else None}
                for i, net in enumerate(n for n in self.networks if vm in n.vms)
            ]  # fmt: skip
            if not nics:
                errors.append(f"{vm.name} is in no network: it could not even reach scoring (register_network)")
            if len(nics) > LIMITS["nics"]:
                errors.append(f"{vm.name} is in more than {LIMITS['nics']} networks")
            vms.append({
                "name": vm.name, "kind": type(vm).__name__, "image": vm.image, "cores": vm.cores,
                "memory": vm.memory, "disk": vm.disk,
                "maintenance": f"{MAINTENANCE_NETWORK.network_address + MAINTENANCE_FIRST_HOST + index}/{MAINTENANCE_NETWORK.prefixlen}",
                "nics": nics,
                "install": list(vm._install), "uninstall": list(vm._uninstall),
                "files": [{"path": p, "content": c} for p, c in sorted(vm._files.items())],
                "setup": list(vm._setup), "services": list(vm._services),
                "users": [login.user for login in self.logins.get(vm.name, [])],
                "firewall": [],
            })  # fmt: skip
        by_name = {vm["name"]: vm for vm in vms}
        for targets, rule in self.rules:
            for vm in targets:
                if vm.name in by_name:
                    entry = self._rule(vm, rule, addresses)
                    if not entry["peers"]:
                        errors.append(f"{vm.name}: {rule!r} reaches no address (its peers share no network with it)")
                    by_name[vm.name]["firewall"].append(entry)

        secrets = []
        for secret in self.secrets:
            if isinstance(secret, SSHKey):
                secrets.append({"id": secret.id, "kind": "ssh_key", "type": secret.type})
            else:
                secrets.append({"id": secret.id, "kind": "password", "user": secret.user})
        known_secrets = {(s.id, f) for s in self.secrets for f in (("public", "private") if isinstance(s, SSHKey) else ("password",))}

        setup = []
        for op, vm, thing, user in self.setup:
            entry = {"op": op, "vm": vm.name, "secret": thing.id}
            if op in ("private_key", "public_key"):
                entry.update(user=user, name=thing.name)
            setup.append(entry)
        files = []
        for handle in self.files:
            for owner_id, field in SECRET_MARKER.findall(handle.content):
                if (owner_id, field) not in known_secrets:
                    errors.append(f"{handle!r}: its content refers to a secret this gameplan did not generate")
            files.append({
                "id": handle.id, "vm": handle.vm.name, "path": handle.path, "content": handle.content,
                "mode": f"{handle.mode:04o}", "owner": handle.owner, "at_reset": handle.at_reset,
            })  # fmt: skip
        for vm in vms:
            for text in [*vm["setup"], *(f["content"] for f in vm["files"])]:
                if SECRET_MARKER.search(text):
                    errors.append(f"{vm['name']}: the run's secrets go into files written with self.write_file, "
                                  "not into what the sandbox is built with")  # fmt: skip
        if errors:
            raise GameplanError("\n".join(dict.fromkeys(errors)))
        return {
            "format": 1,
            "class": type(self.gameplan).__name__,
            "round_duration": duration,
            "entrypoint": self.entrypoint.name,
            "networks": [
                {"name": n.name, "bridge": n.bridge, "cidr": str(n.cidr), "vms": [vm.name for vm in n.vms]}
                for n in self.networks
            ],
            "vms": vms,
            "secrets": secrets,
            "setup": setup,
            "files": files,
        }

    def _addresses(self):
        """{(network, VM): address}, from .10 on, shuffled the same way every time."""
        out = {}
        for net in self.networks:
            hosts = list(range(10, 250))
            random.Random(f"{type(self.gameplan).__name__}/{net.name}").shuffle(hosts)
            for vm, host in zip(net.vms, hosts):
                out[(net.name, vm.name)] = str(net.cidr.network_address + host)
        return out

    def _rule(self, vm, rule, addresses):
        if rule.peers is None:
            peers = [str(n.cidr) for n in self.networks if vm in n.vms]
        else:
            peers = []
            for peer in rule.peers:
                if isinstance(peer, Network):
                    peers.append(str(peer.cidr))
                else:
                    peers += [f"{addresses[(n.name, peer.name)]}/32" for n in self.networks if peer in n.vms]
        return {
            "direction": rule.direction, "action": rule.action, "proto": rule.proto,
            "dport": None if rule.port is None else _dport(rule.port), "peers": sorted(set(peers)),
        }  # fmt: skip

    def _vm_problems(self, vm):
        out = []
        name = vm.name
        for field, (low, high) in (("cores", LIMITS["cores"]), ("memory", LIMITS["memory"]), ("disk", LIMITS["disk"])):
            value = getattr(vm, field)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                out.append(f"{name}: {field} is a whole number in [{low}, {high}]")
        if vm.image not in IMAGES:
            out.append(f"{name}: the image is one of {', '.join(IMAGES)}")
        for package in [*vm._install, *vm._uninstall]:
            if not isinstance(package, str) or not PACKAGE.fullmatch(package):
                out.append(f"{name}: {package!r} is not a Debian package name")
        for unit in vm._services:
            if not isinstance(unit, str) or not SERVICE.fullmatch(unit):
                out.append(f"{name}: {unit!r} is not a systemd unit name")
        for command in vm._setup:
            if not isinstance(command, str) or not command.strip() or len(command) > LIMITS["command_size"]:
                out.append(f"{name}: a setup command is a non-empty string of at most {LIMITS['command_size']} characters")
        for path, content in vm._files.items():
            if _file_path(path, "root") != path:
                out.append(f"{name}: {path!r} is not an absolute, normalised path outside /etc/worms")
            if not isinstance(content, str) or len(content.encode()) > LIMITS["file_size"]:
                out.append(f"{name}: the content of {path} is text of at most {LIMITS['file_size']} bytes")
        return out


class Gameplan:
    """A round's sandbox (`__init__`) and what happens in it (`execute()`).

    `__init__` describes the sandbox: assign VMs to attributes, then use the methods below.
    `execute()` runs once per team. The worm starts when it calls `deploy_worm()`; the run
    ends `round_duration` seconds later (the round's duration per team on the dashboard),
    whether execute() has returned or not. Then every VM is stopped and the run is scored.

    Any exception ends the attempt: it is thrown away and run again (up to three more times).
    What the worm breaks is its own problem, not an error: once the worm runs, a flag or file
    that cannot be written any more is logged and skipped."""

    round_duration = 60 * 60  # seconds from the worm's start to the end of the run

    def __new__(cls, *args, **kwargs):
        self = super().__new__(cls)
        object.__setattr__(self, "_plan", _Plan(self))
        return self

    def __setattr__(self, name, value):
        if isinstance(value, BasicVM) or (
            isinstance(value, (list, tuple)) and value and all(isinstance(v, BasicVM) for v in value)
        ):
            self._plan.register(name, value)
        object.__setattr__(self, name, value)

    @property
    def vms(self):
        """Every VM of the gameplan, in the order they were assigned."""
        return list(self._plan.vms)

    # -- describing the sandbox (in __init__) ------------------------------------

    def register_network(self, vms, name=None):
        """Put VMs on one network of their own (a bridge and a /24). Returns the Network."""
        plan = self._plan
        plan.defining("register_network")
        members = [plan.known(vm, "register_network") for vm in _as_list(vms, "register_network")]
        if not members:
            raise GameplanError("register_network: a network needs at least one VM")
        if len(set(map(id, members))) != len(members):
            raise GameplanError("register_network: a VM is listed twice")
        name = name or f"net{len(plan.networks)}"
        if not isinstance(name, str) or not NETWORK_NAME.fullmatch(name) or any(n.name == name for n in plan.networks):
            raise GameplanError(f"register_network: {name!r} is not a new network name (lowercase, digits, -)")
        network = Network(len(plan.networks), name, members)
        plan.networks.append(network)
        return network

    def register_fw_rule(self, vms, rule):
        """Add a rule to the firewall of each VM (see FirewallRule)."""
        plan = self._plan
        plan.defining("register_fw_rule")
        if not isinstance(rule, FirewallRule):
            raise GameplanError("register_fw_rule: give a FirewallRule")
        problems = rule.problems()
        if problems:
            raise GameplanError("\n".join(problems))
        for peer in rule.peers or []:
            if isinstance(peer, BasicVM):
                plan.known(peer, "FirewallRule peers")
            elif peer not in plan.networks:
                raise GameplanError(f"FirewallRule peers: {peer!r} is not a network of this gameplan")
        targets = [plan.known(vm, "register_fw_rule") for vm in _as_list(vms, "register_fw_rule")]
        plan.rules.append((targets, rule))

    def generate_ssh_key(self, name="id_ed25519", type="ed25519"):
        """A key pair, new for every run. `name` is its file name in ~/.ssh."""
        plan = self._plan
        plan.defining("generate_ssh_key")
        if not isinstance(name, str) or not KEY_NAME.fullmatch(name) or name in ("authorized_keys", "known_hosts", "config"):
            raise GameplanError(f"generate_ssh_key: {name!r} is not a key file name")
        if type not in KEY_TYPES:
            raise GameplanError(f"generate_ssh_key: the type is one of {', '.join(KEY_TYPES)}")
        key = SSHKey(f"key{len(plan.secrets)}", name, type)
        plan.secrets.append(key)
        return key

    def generate_ssh_pwd_login(self, user=None):
        """A user and a password, new for every run (the user name stays). Put it on VMs with
        add_ssh_pwd_login; `login.user` and `login.pwd` can go into a file's content."""
        plan = self._plan
        plan.defining("generate_ssh_pwd_login")
        user = user or f"user{1 + sum(isinstance(s, PasswordLogin) for s in plan.secrets)}"
        if not isinstance(user, str) or not USER.fullmatch(user) or user in RESERVED_USERS:
            raise GameplanError(f"generate_ssh_pwd_login: {user!r} is not a user name a gameplan can create")
        if any(isinstance(s, PasswordLogin) and s.user == user for s in plan.secrets):
            raise GameplanError(f"generate_ssh_pwd_login: {user} exists already")
        login = PasswordLogin(f"login{len(plan.secrets)}", user)
        plan.secrets.append(login)
        return login

    def _owner(self, vm, user, what):
        if user in ("root", DEFAULT_USER) or user in [login.user for login in self._plan.logins.get(vm.name, [])]:
            return user
        raise GameplanError(f"{what}: {user!r} is not a user of {vm.name} (root, {DEFAULT_USER}, or a login added to it first)")

    def add_private_ssh_key(self, vms, key, user=DEFAULT_USER):
        """The private key, as ~/.ssh/<name> of `user` (0600)."""
        self._add_key("private_key", vms, key, user)

    def add_public_ssh_key(self, vms, key, user=DEFAULT_USER):
        """The public key, in ~/.ssh/authorized_keys of `user`: the private key logs in as it."""
        self._add_key("public_key", vms, key, user)

    def _add_key(self, op, vms, key, user):
        plan = self._plan
        plan.defining(f"add_{op.replace('_key', '')}_ssh_key")
        if not isinstance(key, SSHKey) or key not in plan.secrets:
            raise GameplanError(f"add_{op}: give a key from self.generate_ssh_key()")
        for vm in _as_list(vms, op):
            plan.known(vm, op)
            plan.setup.append((op, vm, key, self._owner(vm, user, op)))

    def add_ssh_pwd_login(self, vms, login=None):
        """Create the login's user on each VM and let it log in over SSH with its password."""
        plan = self._plan
        plan.defining("add_ssh_pwd_login")
        if login is None:
            logins = [s for s in plan.secrets if isinstance(s, PasswordLogin)]
            if len(logins) != 1:
                raise GameplanError("add_ssh_pwd_login: name the login (there is not exactly one)")
            login = logins[0]
        if not isinstance(login, PasswordLogin) or login not in plan.secrets:
            raise GameplanError("add_ssh_pwd_login: give a login from self.generate_ssh_pwd_login()")
        for vm in _as_list(vms, "add_ssh_pwd_login"):
            plan.known(vm, "add_ssh_pwd_login")
            on_vm = plan.logins.setdefault(vm.name, [])
            if login in on_vm:
                raise GameplanError(f"add_ssh_pwd_login: {login.user} is on {vm.name} already")
            on_vm.append(login)
            plan.setup.append(("password", vm, login, login.user))

    def write_file(self, vm, path=None, content=None, mode=0o644, owner=DEFAULT_USER, at_reset=True):
        """In __init__: declare a file, written into the VM at the start of every run (before the
        worm), or with at_reset=False only when execute() writes it. `~` is the owner's home;
        `content` may hold the run's secrets (login.pwd, key.private, ...). Returns its handle.

        In execute(): self.write_file(handle) writes a declared file now. Only declared files
        can be written: what goes into a VM never passes the database."""
        plan = self._plan
        if plan.phase == "run":
            if not isinstance(vm, FileHandle) or path is not None or content is not None:
                raise GameplanError("write_file in execute() takes a file declared in __init__ (its handle)")
            return plan.runner.write_file(vm)
        plan.defining("write_file")
        plan.known(vm, "write_file")
        owner = self._owner(vm, owner, "write_file")
        normalised = _file_path(path, owner)
        if normalised is None:
            raise GameplanError(f"write_file: {path!r} is not a path a gameplan can write (absolute or ~/..., "
                                "no .., not /etc/worms)")  # fmt: skip
        if not isinstance(content, str) or len(content.encode()) > LIMITS["file_size"]:
            raise GameplanError(f"write_file: the content is text of at most {LIMITS['file_size']} bytes")
        if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
            raise GameplanError("write_file: mode is a permission like 0o644 (no setuid, setgid or sticky bit)")
        handle = FileHandle(len(plan.files), vm, normalised, str(content), mode, owner, bool(at_reset))
        plan.files.append(handle)
        return handle

    def set_worm_entrypoint(self, vm):
        """The VM the worm is deployed to."""
        plan = self._plan
        plan.defining("set_worm_entrypoint")
        if plan.entrypoint is not None:
            raise GameplanError("set_worm_entrypoint: the worm has one entry point")
        plan.entrypoint = plan.known(vm, "set_worm_entrypoint")

    # -- the run (in execute()) -------------------------------------------------

    def execute(self):
        """What happens in a run. Override it; it must call self.deploy_worm() once."""
        raise GameplanError("the gameplan has no execute()")

    def _runner(self, what):
        runner = self._plan.runner
        if runner is None:
            raise GameplanError(f"{what}() acts on the running sandbox: call it in execute()")
        return runner

    def deploy_worm(self):
        """Put the team's worm on the entry VM and start it. The run's clock starts now."""
        self._runner("deploy_worm").deploy_worm()

    def wait(self, seconds):
        """Let time pass (the worm keeps running). Cut short at the end of the run."""
        self._runner("wait").wait(seconds)

    def remove_flag(self, vm):
        """Remove the VM's flag and check that it is gone. Returns the Flag for add_flag."""
        return self._runner("remove_flag").remove_flag(self._plan.known(vm, "remove_flag"))

    def add_flag(self, flag):
        """Put a new flag on the VM (a Flag from remove_flag, or the VM)."""
        vm = flag.vm if isinstance(flag, Flag) else flag
        self._runner("add_flag").add_flag(self._plan.known(vm, "add_flag"))

    def log(self, message):
        """A line in the orchestrator's journal (never in the database, never shown to teams)."""
        self._runner("log").log(str(message))

    @property
    def elapsed(self):
        """Seconds since the worm started (0 before)."""
        return self._runner("elapsed").elapsed()

    @property
    def remaining(self):
        """Seconds left in the run (round_duration before the worm starts)."""
        return self._runner("remaining").remaining()

    # -- for the framework --------------------------------------------------------

    def spec(self):
        return self._plan.spec()
