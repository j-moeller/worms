"""Defining a gameplan: its sandbox and where the worm starts.

A gameplan's `__init__` only describes. It assigns VMs to attributes (they register themselves,
as layers do in a torch module), joins them into networks, opens ports, generates credentials,
places files and names the VM the worm starts on. Nothing is built or contacted here: the
description becomes a spec (`spec()`, plain JSON), which

  - validation checks before a round is scheduled (gameplan/runtime.py, `validate`),
  - the round agent on the host builds into OpenTofu and Ansible (gameplan/build.py),
  - the conductor runs `execute()` against (gameplan/runtime.py, `Runner`).

What a VM runs is defined by its class: `packages`, `services`, `setup` commands and `files`
are installed when the sandbox is built, and every team's run starts from that snapshot.
Credentials and the files written with `Gameplan.write_file` are made fresh for every run.

docs/05-live-environment.md describes the API with an example (gameplans/round_one.py).
"""

from __future__ import annotations

import inspect
import ipaddress
import posixpath
import random
import re

from gameplan import content as _content
from gameplan.errors import GameplanError, RoundError

# The sandbox networks: /24s from this range (opentofu/shared/shared-variables.tf,
# networks.sandbox_vms). Each network's .1 is sandbox-gw, the way to scoring, logging and the
# private internet; VMs get addresses from .10 on.
SANDBOX_NETWORK = ipaddress.ip_network("10.64.0.0/10")
# The sandbox management bridge (vmbr4): the host reaches every VM here, SSH only. .1-.9 are
# reserved (the host is .1, sandbox-gw .2).
MAINTENANCE_NETWORK = ipaddress.ip_network("10.4.0.0/16")
MAINTENANCE_FIRST_HOST = 10
# The OS images a VM can boot from (opentofu/modules/os_images, local.platform.os_images).
IMAGES = ("debian",)
# The login user of every image: the host's own access (Ansible, the round agent) logs in as it,
# with sudo and no password. A gameplan cannot make it (add_user): it exists.
IMAGE_USER = "debian"
# Whom a gameplan's files, keys, logins and reads are for when it names no user: the image's
# login user (its home /home/debian, its keys in /home/debian/.ssh). The worm starts as root on
# the entry VM, which reads them there; add_private_ssh_key(add_to_root=True) gives root a key too.
DEFAULT_USER = IMAGE_USER
FLAG_PATH = "/etc/worms/flag"
# ~/.ssh/<this> of a user that add_private_ssh_key gave a key: its ssh-agent, which holds the key.
AGENT_SOCKET = "agent.sock"

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
    "groups": 16,  # per user
    "sudo_commands": 16,  # per user
    "description": 20000,  # characters
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
    "www-data", "backup", "list", "irc", "nobody", "sshd", "messagebus", "postgres", "django", IMAGE_USER,
}  # fmt: skip
KEY_TYPES = ("ed25519", "rsa", "ecdsa")

# A user's login shell (Gameplan.add_user). nologin: the user owns files or runs a service, and
# no login works for it. rbash: bash, restricted (no cd, no / in commands, no changing PATH).
SHELLS = ("/bin/bash", "/bin/sh", "/bin/rbash", "/usr/sbin/nologin")
DEFAULT_SHELL = "/bin/bash"
NOLOGIN = "/usr/sbin/nologin"
# A command a sudo rule allows (Gameplan.add_user sudo=[...]): an absolute path, then words. No
# commas, quotes, backslashes or wildcards a rule could be widened with, beyond sudoers' own `*`.
SUDO_COMMAND = re.compile(r"^/[A-Za-z0-9._+/-]+(?: [A-Za-z0-9._+/=:@*-]+)*$")

# What every sandbox VM keeps, whatever a gameplan removes: the host logs in over SSH as the
# image's user and becomes root with sudo, Ansible needs Python, the agent systemd, coreutils and
# chpasswd, the worm bash and Python, scoring is HTTPS (ca-certificates), the VM's network comes
# from cloud-init. The sandbox build also refuses a removal that would take one of these with it
# (roles/applications/sandbox_vm), as apt resolves dependencies there.
PROTECTED_PACKAGES = frozenset({
    "apt", "base-files", "bash", "ca-certificates", "cloud-init", "coreutils", "dpkg", "libc6", "libpam-modules",
    "libpam-runtime", "login", "netplan.io", "openssh-server", "passwd", "python3", "python3-minimal", "sudo",
    "systemd", "systemd-sysv", "util-linux",
})  # fmt: skip
# A per-run secret inside a file's content: replaced on the host, when the file is written.
SECRET_MARKER = re.compile(r"@@worms-secret:([a-z0-9]+)\.([a-z]+)@@")
PASSWORD_CHARSETS = ("alnum", "digits", "hex")
TOTP_LABEL = re.compile(r"^[A-Za-z0-9._@+-]{1,64}$")

# SSH with a key and a TOTP code (Gameplan.add_ssh_totp_login): the members of this group answer
# pam_google_authenticator after their key; everyone else logs in as before. Run when the sandbox
# is built, after the users are made. The Match block goes at the end of sshd_config, as the
# password logins' (roles/applications/sandbox_vm), not into sshd_config.d.
TOTP_GROUP = "worms-totp"
TOTP_WINDOW = 17  # codes accepted around the current one: the VMs' clocks come from the host
TOTP_SETUP = (
    "set -e; getent group worms-totp >/dev/null || groupadd --system worms-totp; "
    "grep -q '^# BEGIN worms totp logins' /etc/ssh/sshd_config || printf '%s\\n' '# BEGIN worms totp logins' "
    "'Match Group worms-totp' '    AuthenticationMethods publickey,keyboard-interactive' "
    "'    KbdInteractiveAuthentication yes' '# END worms totp logins' >> /etc/ssh/sshd_config; "
    "grep -q pam_google_authenticator /etc/pam.d/sshd || sed -i '0,/^@include common-auth/s//"
    "auth [success=ignore default=1] pam_succeed_if.so quiet user ingroup worms-totp\\n"
    "auth [success=done default=die] pam_google_authenticator.so\\n&/' /etc/pam.d/sshd; "
    "sshd -t; systemctl reload ssh"
)

# Telnet (Gameplan.add_telnet_login): inetutils-telnetd registers itself with inetd switched off
# (#<off># in /etc/inetd.conf); this switches it on, behind tcpd as the package means it. telnetd
# hands over to `login -h <host>`, and Debian 13's login (util-linux) asks PAM for the service
# `remote` then. Debian ships none, so PAM would fall back to `other`: any user with a password.
# This makes `remote` login's stack with a list in front, so only the users of telnet logins get
# in, not a user whose password is for su or FTP. Console logins (`login`) stay as they are.
TELNET_USERS = "/etc/worms-telnet-users"
TELNET_SETUP = (
    f"set -e; touch {TELNET_USERS}; [ -e /etc/pam.d/remote ] || cp /etc/pam.d/login /etc/pam.d/remote; "
    f"grep -q worms-telnet-users /etc/pam.d/remote || "
    f"sed -i '1i auth required pam_listfile.so item=user sense=allow file={TELNET_USERS} onerr=fail' /etc/pam.d/remote; "
    "update-inetd --enable telnet; grep -q '^telnet' /etc/inetd.conf; systemctl restart inetutils-inetd"
)

# FTP (Gameplan.add_ftp_login): vsftpd, local users with their password, read and write, not
# chrooted (Debian's default). Only the users of FTP logins get in (userlist_deny=NO); the PAM
# service also wants a shell listed in /etc/shells. Passive mode only, on these ports: a firewall
# rule has to open them as well as 21.
FTP_PASSIVE_PORTS = (40000, 40019)
FTP_USERS = "/etc/vsftpd.user_list"

# SSH on another port (BasicVM.set_ssh_port): sshd answers there as well as on 22, which the
# host logs in on over the management NIC (its rule is the platform's, not the gameplan's). The
# gameplan's firewall decides which of the two the sandbox networks see. A drop-in with any Port
# replaces sshd's default, so 22 is listed too. sshd re-binds on reload (SIGHUP).
SSH_PORT_CONF = "/etc/ssh/sshd_config.d/20-worms-port.conf"
# The ports the framework's own services take on a VM that runs them, by their unit: sshd cannot
# have one of them.
SERVICE_PORTS = {
    "inetutils-inetd": [(23, 23)],  # add_telnet_login
    "vsftpd": [(21, 21), FTP_PASSIVE_PORTS],  # add_ftp_login
    "django-site": [(8000, 8000)],  # DjangoVM
    "postgresql": [(5432, 5432)],  # PostgreVM
}
# A key's passphrase: text, which may hold the run's passwords (a Password, a login's pwd, a
# TOTP's secret), never a key. No line breaks: ssh reads it as one line.
PASSPHRASE_SIZE = 1024
PASSPHRASE_FIELDS = ("password", "secret")
FTP_CONF = f"""# Written by the gameplan framework (Gameplan.add_ftp_login).
listen=YES
listen_ipv6=NO
anonymous_enable=NO
local_enable=YES
write_enable=YES
local_umask=022
dirmessage_enable=YES
use_localtime=YES
xferlog_enable=YES
port_enable=NO
pasv_enable=YES
pasv_min_port={FTP_PASSIVE_PORTS[0]}
pasv_max_port={FTP_PASSIVE_PORTS[1]}
secure_chroot_dir=/var/run/vsftpd/empty
pam_service_name=vsftpd
ssl_enable=NO
userlist_enable=YES
userlist_deny=NO
userlist_file={FTP_USERS}
"""

Allow = "ACCEPT"
Deny = "DROP"
Incoming = "in"
Outgoing = "out"


class Secret(str):
    """A per-run secret, as it stands in a file's content until the host writes the file. Use it
    like a string (an f-string, `+`): the host replaces it with the run's value."""

    def __new__(cls, owner, field):
        return super().__new__(cls, f"@@worms-secret:{owner}.{field}@@")


class SSHKey:
    """A key pair, generated for every run (`Gameplan.generate_ssh_key`). `name` is its file
    name in ~/.ssh; `public` and `private` stand for the run's keys in a file's content. With a
    passphrase the private key is encrypted with it, as `ssh-keygen -p` does; `passphrase` is
    that text, for a file's content (None: the key is not encrypted)."""

    def __init__(self, ident, name, type, passphrase=None):
        self.id, self.name, self.type = ident, name, type
        self.public = Secret(ident, "public")
        self.private = Secret(ident, "private")
        self.passphrase = passphrase

    def __repr__(self):
        return f"SSHKey({self.name!r})"


class PasswordLogin:
    """A user with a password, generated for every run (`Gameplan.generate_login`). The user name
    is fixed; `pwd` (or `password`) stands for the run's password. The logins (add_ssh_pwd_login,
    add_telnet_login, add_ftp_login, add_local_login) put it on VMs."""

    def __init__(self, ident, user, length=None, charset=None):
        self.id, self.user = ident, user
        self.length, self.charset = length, charset  # None: a long random one
        self.pwd = self.password = Secret(ident, "password")

    def __repr__(self):
        return f"PasswordLogin({self.user!r})"


class Password(Secret):
    """A password of no user, new for every run (`Gameplan.generate_password`): for a vault, a
    zip, a document. It is a string, so it goes wherever text goes."""

    def __new__(cls, ident, length, charset):
        self = super().__new__(cls, ident, "password")
        self.id, self.length, self.charset = ident, length, charset
        return self

    def __repr__(self):
        return f"Password({self.id})"


class TOTP:
    """A time-based one-time password's secret, new for every run (`Gameplan.generate_totp`):
    what an authenticator app holds. `secret` stands for the base32 seed, `uri` for the
    otpauth:// URI with it (a QR code's content); both go into a file's content."""

    def __init__(self, ident, account, issuer, digits, period, algorithm):
        self.id, self.account, self.issuer = ident, account, issuer
        self.digits, self.period, self.algorithm = digits, period, algorithm
        self.secret = Secret(ident, "secret")

    def spec(self):
        return {"id": self.id, "account": self.account, "issuer": self.issuer, "digits": self.digits,
                "period": self.period, "algorithm": self.algorithm, "secret": str(self.secret)}  # fmt: skip

    @property
    def uri(self):
        return _content.totp_uri(self.spec())

    def __repr__(self):
        return f"TOTP({self.issuer}:{self.account})"


def _fields(secret):
    """The fields of a secret that a file's content can name."""
    if isinstance(secret, SSHKey):
        return ("public", "private")
    if isinstance(secret, TOTP):
        return ("secret",)
    return ("password",)


class FileState:
    """What `vm.read_file(path)` learns about a file in execute(): whether it is there, its size
    and its SHA-256. Its content stays on the host (it would pass the database, which the
    dashboard reads). Compare two reads to see a change. `error`: why it could not be read
    (only after the worm, which is root on the VM; before it, that fails the attempt)."""

    def __init__(self, exists=None, size=None, sha256=None, error=None):
        self.exists, self.size, self.sha256, self.error = exists, size, sha256, error

    def __eq__(self, other):
        return isinstance(other, FileState) and (self.exists, self.size, self.sha256) == (other.exists, other.size, other.sha256)

    def __hash__(self):
        return hash((self.exists, self.size, self.sha256))

    def __repr__(self):
        if self.error:
            return f"FileState(error={self.error!r})"
        return f"FileState(exists={self.exists}, size={self.size}, sha256={(self.sha256 or '')[:12]})"


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
        files     {path: content} written then (no per-run secrets)
        setup     shell commands then, as root, after the packages, users and files
        remove    packages removed then, after the setup commands (which may still use them)
        purge     packages removed with their configuration, as `remove`
        services  systemd units enabled and running then, last

    The resources are the class's unless given: BasicVM(cores=2, memory=1024, disk=8).
    `install`/`uninstall`, `add_setup` and `add_service` add to them for one VM, and
    `add_user(user, shell=, groups=, sudo=)` makes a user (the owner of files and logins).
    Where a method takes a user (owner=, user=) and is given none, it is debian, the image's
    login user: ~ is /home/debian. (The worm starts as root on the entry VM.)
    `ssh_port` is the port sshd answers on in the sandbox networks (22; `set_ssh_port`).

    Per run: `write_file(path, content)`, text or a document, archive or vault (also
    Gameplan.write_file).

    In `execute()` a VM is also a handle on its power: start(), stop(), shutdown(),
    suspend(), resume(), status(); and `read_file(path)` tells whether a file changed."""

    image = "debian"
    cores = 1
    memory = 512  # MB: the ceiling; the host may reclaim unused RAM (docs/01-components.md)
    disk = 5  # GB
    packages: tuple = ()
    remove: tuple = ()
    purge: tuple = ()
    files: dict = {}
    setup: tuple = ()
    services: tuple = ()
    ssh_port = 22

    def __init__(self, *, cores=None, memory=None, disk=None, image=None):
        cls = type(self)
        self.cores = cls.cores if cores is None else cores
        self.memory = cls.memory if memory is None else memory
        self.disk = cls.disk if disk is None else disk
        self.image = cls.image if image is None else image
        self.ssh_port = cls.ssh_port
        self._install = list(cls.packages)
        self._purge = list(cls.purge)
        self._uninstall = [p for p in cls.remove if p not in self._purge]
        self._required = set()  # packages a login of the gameplan needs here: never removed
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
        self._install.extend(p for p in packages if p not in self._install)

    def uninstall(self, *packages, purge=False):
        """Packages removed when the sandbox is built, after the setup commands: a package
        installed for them (a compiler, say) can go again. `purge=True` removes their
        configuration files too. What the platform needs on every VM cannot be removed
        (PROTECTED_PACKAGES), nor what one of the gameplan's logins needs."""
        self._defining("uninstall")
        for package in packages:
            if purge:
                if package in self._uninstall:
                    self._uninstall.remove(package)
                if package not in self._purge:
                    self._purge.append(package)
            elif package not in self._uninstall and package not in self._purge:
                self._uninstall.append(package)

    def _require(self, *packages):
        """Install packages a login needs, and keep them from being removed."""
        self.install(*packages)
        self._required.update(packages)

    def add_setup(self, *commands):
        """Shell commands run as root when the sandbox is built, after the class's own."""
        self._defining("add_setup")
        self._setup.extend(commands)

    def add_service(self, *units):
        """systemd units enabled and running when the sandbox is built."""
        self._defining("add_service")
        self._services.extend(unit for unit in units if unit not in self._services)

    def set_ssh_port(self, port):
        """sshd answers on `port` in the sandbox networks instead of 22: open it with a firewall
        rule, and leave 22 closed, for a worm to find it (or a file to tell). sshd still listens
        on 22, for the host's own access over the management NIC, which gameplan rules do not
        reach. Every login over SSH (keys, passwords, TOTP) is on this port then."""
        self._defining("set_ssh_port")
        self.ssh_port = port

    def _setup_commands(self):
        """The setup commands as the sandbox is built with them: the SSH port first."""
        if self.ssh_port == 22:
            return list(self._setup)
        port = (f"set -e; printf 'Port 22\\nPort {self.ssh_port}\\n' > {SSH_PORT_CONF}; "
                "sshd -t; systemctl reload ssh")  # fmt: skip
        return [port, *self._setup]

    def _gameplan(self, what):
        if self._plan is None:
            raise GameplanError(f"{self!r}.{what}(): assign the VM to self.<name> first")
        return self._plan.gameplan

    def add_user(self, user, *, shell=None, groups=None, sudo=None):
        """A user on this VM, made when the sandbox is built: no password, no key. Logins give
        it one (add_ssh_key_login, ...); files can be its own (write_file owner=). The options
        are Gameplan.add_user's."""
        self._gameplan("add_user").add_user(self, user, shell=shell, groups=groups, sudo=sudo)

    def write_file(self, path, content, mode=0o644, owner=DEFAULT_USER, at_reset=True):
        """A file on this VM, made for every run (Gameplan.write_file): text, or a document,
        archive or vault (gameplan/content.py). Returns its handle."""
        return self._gameplan("write_file").write_file(self, path, content, mode=mode, owner=owner, at_reset=at_reset)

    def watch_file(self, path, user=DEFAULT_USER):
        """A path execute() may read_file() besides the files written with write_file (those it
        may always). Declared here so that a read names it by number: no path of the gameplan
        reaches the database. `~` is `user`'s home."""
        self._defining("watch_file")
        normalised = _readable_path(path, user, "watch_file")
        watched = self._gameplan("watch_file")._plan.watched
        if (self, normalised) not in watched:
            watched.append((self, normalised))

    # -- in execute() ----------------------------------------------------------

    def read_file(self, path, user=DEFAULT_USER):
        """What the file is now (a FileState: exists, size, sha256), not its content: a file
        written with write_file, or one declared with watch_file. `~` is `user`'s home."""
        runner = self._runtime("read_file")
        normalised = _readable_path(path, user, "read_file")
        readable = self._plan.readable()
        if (self.name, normalised) not in readable:
            raise GameplanError(f"read_file: {self.name} {normalised} is neither written with write_file nor declared "
                                f"with {self.name}.watch_file() in __init__")  # fmt: skip
        return runner.read_file(self, readable.index((self.name, normalised)), normalised)

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


def _description(text):
    """(the description as teams read it, or a problem with it). Indentation and the blank
    lines around it go (inspect.cleandoc), so a triple-quoted string in the class body works."""
    if not isinstance(text, str) or len(text) > LIMITS["description"] or "\x00" in text:
        return "", f"description is text of at most {LIMITS['description']} characters"
    if SECRET_MARKER.search(text):
        return "", "description: teams read it on the dashboard, so it cannot hold the run's secrets"
    return inspect.cleandoc(text), None


def _readable_path(path, user, what):
    normalised = _file_path(path, user)
    if normalised is None:
        raise GameplanError(f"{what}: {path!r} is not a path a gameplan can read (absolute or ~/..., no .., not /etc/worms)")
    return normalised


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
        self.logins: dict[str, list] = {}  # VM name -> PasswordLogins on it (their password set every run)
        self.users: dict[str, list] = {}  # VM name -> users made when the sandbox is built
        self.accounts: dict[tuple, dict] = {}  # (VM name, user) -> {shell, groups, sudo} as add_user gave them
        self.ssh_password: dict[str, list] = {}  # VM name -> users who may log in over SSH with a password
        self.watched: list[tuple] = []  # (VM, path) execute() may read besides the files it writes
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

    def readable(self):
        """[(VM name, path)] execute() may read_file(), numbered by their place: the files
        written, then those watched."""
        out = []
        for name, path in [*((h.vm.name, h.path) for h in self.files), *((vm.name, p) for vm, p in self.watched)]:
            if (name, path) not in out:
                out.append((name, path))
        return out

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
        description, problem = _description(getattr(self.gameplan, "description", ""))
        if problem:
            errors.append(problem)

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
                "install": list(vm._install), "uninstall": list(vm._uninstall), "purge": list(vm._purge),
                "files": [{"path": p, "content": c} for p, c in sorted(vm._files.items())],
                "setup": vm._setup_commands(), "services": list(vm._services), "ssh_port": vm.ssh_port,
                "users": list(self.users.get(vm.name, [])),
                "accounts": {user: self._account(vm, user) for user in self.users.get(vm.name, [])},
                "ssh_password_users": list(self.ssh_password.get(vm.name, [])),
                "firewall": [],
            })  # fmt: skip
            errors += self._login_problems(vm)
            errors += self._unreachable_files(vm)
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
                entry = {"id": secret.id, "kind": "ssh_key", "type": secret.type}
                if secret.passphrase is not None:
                    entry["passphrase"] = secret.passphrase
                secrets.append(entry)
            elif isinstance(secret, TOTP):
                secrets.append({"id": secret.id, "kind": "totp"})
            elif isinstance(secret, Password):
                secrets.append({"id": secret.id, "kind": "password", "length": secret.length, "charset": secret.charset})
            else:
                entry = {"id": secret.id, "kind": "password", "user": secret.user}
                if secret.charset is not None:
                    entry.update(length=secret.length, charset=secret.charset)
                secrets.append(entry)
        known_secrets = {(s.id, f) for s in self.secrets for f in _fields(s)}

        setup = []
        for op, vm, thing, user in self.setup:
            entry = {"op": op, "vm": vm.name, "secret": thing.id}
            if op in ("private_key", "public_key"):
                entry.update(user=user, name=thing.name)
            setup.append(entry)
        files = []
        for handle in self.files:
            for text in _content.strings(handle.content):
                for owner_id, field in SECRET_MARKER.findall(text):
                    if (owner_id, field) not in known_secrets:
                        errors.append(f"{handle!r}: its content refers to a secret this gameplan did not generate")
            files.append({
                "id": handle.id, "vm": handle.vm.name, "path": handle.path, "content": handle.content,
                "mode": f"{handle.mode:04o}", "owner": handle.owner, "at_reset": handle.at_reset,
            })  # fmt: skip
        for vm in vms:
            for text in [*vm["setup"], *(f["content"] for f in vm["files"])]:
                if isinstance(text, str) and SECRET_MARKER.search(text):
                    errors.append(f"{vm['name']}: the run's secrets go into files written with self.write_file, "
                                  "not into what the sandbox is built with")  # fmt: skip
        if errors:
            raise GameplanError("\n".join(dict.fromkeys(errors)))
        return {
            "format": 1,
            "class": type(self.gameplan).__name__,
            "round_duration": duration,
            "description": description,
            "entrypoint": self.entrypoint.name,
            "networks": [
                {"name": n.name, "bridge": n.bridge, "cidr": str(n.cidr), "vms": [vm.name for vm in n.vms]}
                for n in self.networks
            ],
            "vms": vms,
            "secrets": secrets,
            "setup": setup,
            "files": files,
            "reads": [{"id": i, "vm": name, "path": path} for i, (name, path) in enumerate(self.readable())],
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

    def _account(self, vm, user):
        """A user's settings on a VM, with the defaults filled in."""
        settings = self.accounts.get((vm.name, user), {})
        return {
            "shell": settings.get("shell") or DEFAULT_SHELL,
            "groups": list(settings.get("groups", [])),
            "sudo": list(settings.get("sudo") or []),
        }

    def _login_users(self, vm):
        """The users something logs in as on the VM: with a key (also with a TOTP code), or with a
        password (SSH, telnet, FTP, su)."""
        users = {login.user for login in self.logins.get(vm.name, [])}
        return users | {user for op, on, _, user in self.setup if op == "public_key" and on is vm}

    def _login_problems(self, vm):
        """A user that something logs in as needs a shell: login(1), sshd, su and vsftpd's PAM
        all refuse nologin."""
        with_login = self._login_users(vm)
        return [
            f"{vm.name}: {user} has a login, so its shell cannot be {NOLOGIN}"
            for user in self.users.get(vm.name, [])
            if user in with_login and self._account(vm, user)["shell"] == NOLOGIN
        ]

    def _unreachable_files(self, vm):
        """A file (or private key) in the home of a user nothing logs in as on the VM: a home is
        0700 on the image, so no worm could read it. Not on the entry point (the worm starts
        there as root), nor where root logs in (a key: sshd lets root in without a password) or
        a user that logs in can sudo: debian (the image's rule, without a password), a sudo rule,
        or the group sudo with a password the worm has. Root's files, and files outside /home,
        are the gameplan's business."""
        if vm is self.entrypoint:
            return []
        logins = self._login_users(vm)
        passwords = {login.user for login in self.logins.get(vm.name, [])}
        for user in logins:
            account = self._account(vm, user)
            if user in ("root", IMAGE_USER) or account["sudo"] or ("sudo" in account["groups"] and user in passwords):
                return []
        placed = [(handle.path, handle.owner) for handle in self.files if handle.vm is vm]
        placed += [(f"{_home(user)}/.ssh/{key.name}", user)
                   for op, on, key, user in self.setup if op == "private_key" and on is vm]  # fmt: skip
        homes = {IMAGE_USER, *self.users.get(vm.name, [])}
        out = []
        for path, owner in placed:
            parts = path.split("/")
            # Whose home it is in, not whose file: only that user passes the home's 0700.
            home = parts[2] if len(parts) > 3 and parts[1] == "home" and parts[2] in homes else None
            if owner != "root" and home is not None and home not in logins:
                out.append(f"{vm.name}: no worm can read {path}: it is in {home}'s home (0700), and nothing logs in "
                           f"there as {home}, nor as a user with sudo (the users that log in: "
                           f"{', '.join(sorted(logins)) or 'none'})")  # fmt: skip
        return out

    def _vm_problems(self, vm):
        out = []
        name = vm.name
        for field, (low, high) in (("cores", LIMITS["cores"]), ("memory", LIMITS["memory"]), ("disk", LIMITS["disk"])):
            value = getattr(vm, field)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                out.append(f"{name}: {field} is a whole number in [{low}, {high}]")
        if vm.image not in IMAGES:
            out.append(f"{name}: the image is one of {', '.join(IMAGES)}")
        for package in [*vm._install, *vm._uninstall, *vm._purge]:
            if not isinstance(package, str) or not PACKAGE.fullmatch(package):
                out.append(f"{name}: {package!r} is not a Debian package name")
        for package in [p for p in [*vm._uninstall, *vm._purge] if isinstance(p, str)]:
            if package in PROTECTED_PACKAGES:
                out.append(f"{name}: {package} cannot be removed: every sandbox VM needs it (the host's access, "
                           "Ansible, the worm, scoring)")  # fmt: skip
            elif package in vm._required:
                out.append(f"{name}: {package} cannot be removed: a login of the gameplan needs it")
        for unit in vm._services:
            if not isinstance(unit, str) or not SERVICE.fullmatch(unit):
                out.append(f"{name}: {unit!r} is not a systemd unit name")
        for command in vm._setup:
            if not isinstance(command, str) or not command.strip() or len(command) > LIMITS["command_size"]:
                out.append(f"{name}: a setup command is a non-empty string of at most {LIMITS['command_size']} characters")
        port = vm.ssh_port
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            out.append(f"{name}: the SSH port is a number in [1, 65535]")
        else:
            for unit in vm._services:
                for low, high in SERVICE_PORTS.get(unit, []):
                    if low <= port <= high:
                        out.append(f"{name}: SSH cannot be on port {port}: {unit} uses it")
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
    # What teams read about the round on the dashboard (the header's round timer links to it):
    # plain text, a blank line between paragraphs. Published as it is, before the round, so
    # nothing in it may give away more than the round should; "" shows none.
    description = ""

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

    def generate_ssh_key(self, name="id_ed25519", type="ed25519", passphrase=None):
        """A key pair, new for every run. `name` is its file name in ~/.ssh.

        With `passphrase` the private key is encrypted with it (OpenSSH's own format, as
        `ssh-keygen -p` writes it): ssh asks for it before the key logs in. The passphrase is
        text: fixed, for a worm to guess from hints ("rex2019"), or the run's own, from
        generate_password() or a login's pwd, or both (f"rex{pin}"). `key.passphrase` is that
        text, so a note can tell it."""
        plan = self._plan
        plan.defining("generate_ssh_key")
        reserved = ("authorized_keys", "known_hosts", "config", AGENT_SOCKET)
        if not isinstance(name, str) or not KEY_NAME.fullmatch(name) or name in reserved:
            raise GameplanError(f"generate_ssh_key: {name!r} is not a key file name")
        if type not in KEY_TYPES:
            raise GameplanError(f"generate_ssh_key: the type is one of {', '.join(KEY_TYPES)}")
        if passphrase is not None:
            passphrase = self._passphrase(passphrase)
        key = SSHKey(f"key{len(plan.secrets)}", name, type, passphrase)
        plan.secrets.append(key)
        return key

    def _passphrase(self, text):
        """A key's passphrase, checked: one line of text, the run's secrets in it known and no key."""
        if not isinstance(text, str):
            raise GameplanError("generate_ssh_key: the passphrase is text (a generate_password() or a login's pwd too)")
        if not text or len(text) > PASSPHRASE_SIZE or any(c in text for c in "\x00\r\n"):
            raise GameplanError(f"generate_ssh_key: the passphrase is one line of 1-{PASSPHRASE_SIZE} characters "
                                "(leave it out for none)")  # fmt: skip
        known = {(s.id, f) for s in self._plan.secrets if not isinstance(s, SSHKey) for f in _fields(s)}
        for owner, field in SECRET_MARKER.findall(text):
            if (owner, field) not in known or field not in PASSPHRASE_FIELDS:
                raise GameplanError("generate_ssh_key: the passphrase may hold this gameplan's passwords and TOTP "
                                    "secrets, not a key")  # fmt: skip
        return str(text)

    def generate_login(self, user=None, length=None, charset=None):
        """A user and a password, new for every run (the user name stays). Put it on VMs with a
        login: add_ssh_pwd_login, add_telnet_login, add_ftp_login, add_local_login; `login.user`
        and `login.pwd` can go into a file's content.

        By default the password is long and random. With `length` and/or `charset` (as for
        generate_password: alnum, digits, hex) it is as weak as asked, for a worm to guess:
        generate_login("pi", length=4, charset="digits")."""
        return self._generate_login("generate_login", user, length, charset)

    def generate_ssh_pwd_login(self, user=None, length=None, charset=None):
        """generate_login under its first name: the login is not only for SSH."""
        return self._generate_login("generate_ssh_pwd_login", user, length, charset)

    def _generate_login(self, what, user, length, charset):
        plan = self._plan
        plan.defining(what)
        user = user or f"user{1 + sum(isinstance(s, PasswordLogin) for s in plan.secrets)}"
        if not isinstance(user, str) or not USER.fullmatch(user) or user in RESERVED_USERS:
            raise GameplanError(f"{what}: {user!r} is not a user name a gameplan can create")
        if any(isinstance(s, PasswordLogin) and s.user == user for s in plan.secrets):
            raise GameplanError(f"{what}: {user} exists already")
        if length is not None or charset is not None:
            length, charset = self._password_strength(what, 16 if length is None else length, charset or "alnum")
        login = PasswordLogin(f"login{len(plan.secrets)}", user, length, charset)
        plan.secrets.append(login)
        return login

    @staticmethod
    def _password_strength(what, length, charset):
        if charset not in PASSWORD_CHARSETS:
            raise GameplanError(f"{what}: the charset is one of {', '.join(PASSWORD_CHARSETS)}")
        if isinstance(length, bool) or not isinstance(length, int) or not 4 <= length <= 64:
            raise GameplanError(f"{what}: the length is a whole number in [4, 64]")
        return length, charset

    def generate_password(self, length=16, charset="alnum"):
        """A password of no user, new for every run: for a vault, a zip, a document. It is a
        string (a marker the host fills in), so f-strings and `+` work.

        charset: alnum (letters and digits), digits (a PIN), hex."""
        plan = self._plan
        plan.defining("generate_password")
        length, charset = self._password_strength("generate_password", length, charset)
        password = Password(f"pw{len(plan.secrets)}", length, charset)
        plan.secrets.append(password)
        return password

    def generate_totp(self, account, issuer="worms", digits=6, period=30, algorithm="SHA1"):
        """A TOTP secret, new for every run: what an authenticator app holds for `account`.
        Give it to a login (add_ssh_totp_login) and put it into a file (`totp.uri`,
        `totp.secret`, TotpExport, a KeePass Entry's otp)."""
        plan = self._plan
        plan.defining("generate_totp")
        for value, what in ((account, "account"), (issuer, "issuer")):
            if not isinstance(value, str) or not TOTP_LABEL.fullmatch(value):
                raise GameplanError(f"generate_totp: the {what} is 1-64 letters, digits and ._@+- (no spaces)")
        if digits not in (6, 7, 8) or period not in (15, 30, 60) or algorithm not in _content.TOTP_ALGORITHMS:
            raise GameplanError("generate_totp: digits 6-8, a period of 15, 30 or 60 s, algorithm "
                                f"{', '.join(_content.TOTP_ALGORITHMS)}")  # fmt: skip
        totp = TOTP(f"totp{len(plan.secrets)}", account, issuer, digits, period, algorithm)
        plan.secrets.append(totp)
        return totp

    def _owner(self, vm, user, what):
        if user in ("root", IMAGE_USER) or user in self._plan.users.get(vm.name, []):
            return user
        raise GameplanError(f"{what}: {user!r} is not a user of {vm.name} (root, {IMAGE_USER}, or one added to it first)")

    def add_user(self, vms, user, *, shell=None, groups=None, sudo=None):
        """A user on each VM, made when the sandbox is built: locked, no key. The logins below
        make theirs (and a login's user is made by the login); add one yourself to own files,
        for a login you build, or to give it these:

            shell   /bin/bash (default), /bin/sh, /bin/rbash (restricted), or /usr/sbin/nologin
                    for a user nothing logs in as
            groups  supplementary groups, made if the VM lacks them. `sudo`: sudo with the
                    user's own password (a login's password, say)
            sudo    True: sudo to anyone, no password. A list of commands: those as root, no
                    password (["/usr/bin/find", "/usr/bin/vim /etc/hosts"]): a step up for a
                    worm that knows its GTFOBins

        Called again for the same user, the groups add up; a different shell or sudo rule is an
        error."""
        plan = self._plan
        plan.defining("add_user")
        if not isinstance(user, str) or not USER.fullmatch(user) or user in RESERVED_USERS:
            raise GameplanError(f"add_user: {user!r} is not a user name a gameplan can create")
        if shell is not None and shell not in SHELLS:
            raise GameplanError(f"add_user: the shell is one of {', '.join(SHELLS)}")
        groups = self._user_groups(groups)
        sudo = self._sudo_rule(sudo)
        for vm in _as_list(vms, "add_user"):
            name = plan.known(vm, "add_user").name
            users = plan.users.setdefault(name, [])
            if user not in users:
                users.append(user)
            settings = plan.accounts.setdefault((name, user), {"shell": None, "groups": [], "sudo": None})
            for field, value in (("shell", shell), ("sudo", sudo)):
                if value is not None:
                    if settings[field] is not None and settings[field] != value:
                        raise GameplanError(f"add_user: {user} on {name} has another {field} already ({settings[field]})")
                    settings[field] = value
            settings["groups"] += [group for group in groups if group not in settings["groups"]]

    @staticmethod
    def _user_groups(groups):
        if groups is None:
            return []
        if isinstance(groups, str):
            groups = [groups]
        if not isinstance(groups, (list, tuple)) or len(groups) > LIMITS["groups"]:
            raise GameplanError(f"add_user: groups is a list of at most {LIMITS['groups']} group names")
        for group in groups:
            if not isinstance(group, str) or not USER.fullmatch(group):
                raise GameplanError(f"add_user: {group!r} is not a group name (lowercase letters, digits, _ and -)")
            if group == TOTP_GROUP:
                raise GameplanError(f"add_user: {TOTP_GROUP} is add_ssh_totp_login's (its members need a code)")
        return list(dict.fromkeys(groups))

    @staticmethod
    def _sudo_rule(sudo):
        """None (no rule), ["ALL"], or the commands."""
        if sudo is None or sudo is False:
            return None
        if sudo is True:
            return ["ALL"]
        if isinstance(sudo, str):
            sudo = [sudo]
        if not isinstance(sudo, (list, tuple)) or not sudo or len(sudo) > LIMITS["sudo_commands"]:
            raise GameplanError(f"add_user: sudo is True, or a list of at most {LIMITS['sudo_commands']} commands")
        for command in sudo:
            if not isinstance(command, str) or len(command) > 200 or not SUDO_COMMAND.fullmatch(command):
                raise GameplanError(f"add_user: sudo {command!r}: a command is an absolute path and its arguments "
                                    "(letters, digits, ._+/=:@*-), no commas or quotes")  # fmt: skip
        return list(dict.fromkeys(sudo))

    def add_private_ssh_key(self, vms, key, user=DEFAULT_USER, add_to_root=False):
        """The private key, as ~/.ssh/<name> of `user` (debian unless given: /home/debian/.ssh),
        0600, and held by `user`'s ssh-agent on the VM: `ssh`, `scp` and `sftp` run as `user` offer
        it whatever its file name, with no -i, and so does anything given SSH_AUTH_SOCK=
        ~/.ssh/agent.sock (a login shell has it). The agent is a service of the VM: it starts at
        every boot and loads the keys again from their files. With add_to_root=True the key is
        also root's, the same way (/root/.ssh/<name>, root's ssh-agent): the worm, which starts as
        root on the entry VM, offers it with a plain `ssh`. A key with a passphrase is only the
        file: the agent would hold it unlocked, and the passphrase is for the worm to find."""
        if not isinstance(add_to_root, bool):
            raise GameplanError("add_private_ssh_key: add_to_root is True or False")
        self._add_key("private_key", vms, key, user)
        if add_to_root and user != "root":
            self._add_key("private_key", vms, key, "root")

    def add_public_ssh_key(self, vms, key, user=DEFAULT_USER):
        """The public key, in ~/.ssh/authorized_keys of `user` (debian unless given): the private
        key logs in as it."""
        self._add_key("public_key", vms, key, user)

    def _add_key(self, op, vms, key, user):
        plan = self._plan
        plan.defining(f"add_{op.replace('_key', '')}_ssh_key")
        if not isinstance(key, SSHKey) or key not in plan.secrets:
            raise GameplanError(f"add_{op}: give a key from self.generate_ssh_key()")
        for vm in _as_list(vms, op):
            plan.known(vm, op)
            plan.setup.append((op, vm, key, self._owner(vm, user, op)))

    def _login(self, login, what):
        plan = self._plan
        if login is None:
            logins = [s for s in plan.secrets if isinstance(s, PasswordLogin)]
            if len(logins) != 1:
                raise GameplanError(f"{what}: name the login (there is not exactly one)")
            login = logins[0]
        if not isinstance(login, PasswordLogin) or login not in plan.secrets:
            raise GameplanError(f"{what}: give a login from self.generate_login()")
        return login

    def _set_password(self, vm, login):
        """The login's user on the VM, its password set every run (once, whatever uses it)."""
        plan = self._plan
        self.add_user(vm, login.user)
        on_vm = plan.logins.setdefault(vm.name, [])
        if login not in on_vm:
            on_vm.append(login)
            plan.setup.append(("password", vm, login, login.user))

    def add_ssh_pwd_login(self, vms, login=None):
        """Create the login's user on each VM and let it log in over SSH with its password."""
        plan = self._plan
        plan.defining("add_ssh_pwd_login")
        login = self._login(login, "add_ssh_pwd_login")
        for vm in _as_list(vms, "add_ssh_pwd_login"):
            plan.known(vm, "add_ssh_pwd_login")
            allowed = plan.ssh_password.setdefault(vm.name, [])
            if login.user in allowed:
                raise GameplanError(f"add_ssh_pwd_login: {login.user} is on {vm.name} already")
            self._set_password(vm, login)
            allowed.append(login.user)

    def write_file(self, vm, path=None, content=None, mode=0o644, owner=DEFAULT_USER, at_reset=True):
        """In __init__: declare a file, written into the VM at the start of every run (before the
        worm), or with at_reset=False only when execute() writes it. `~` is the owner's home,
        which only the owner can enter (0700): give the file to the user the worm arrives as on
        that VM (the spec refuses a file in a home no login reaches).

        `content` is text, or a document, archive or vault (gameplan/content.py: Doc, Docx, Ods,
        Zip, KeePass, TotpExport), made on the host. Either may hold the run's secrets
        (login.pwd, key.private, totp.secret, a generate_password(), ...). Returns its handle.

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
        try:
            described = _content.spec_of(content)
            problems = _content.check(described)
        except GameplanError as error:
            raise GameplanError(f"write_file {normalised}: {error}") from None
        if isinstance(described, str) and len(described.encode()) > LIMITS["file_size"]:
            problems.append(f"the content is text of at most {LIMITS['file_size']} bytes")
        if problems:
            raise GameplanError(f"write_file {normalised}: " + "; ".join(problems))
        if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
            raise GameplanError("write_file: mode is a permission like 0o644 (no setuid, setgid or sticky bit)")
        handle = FileHandle(len(plan.files), vm, normalised, described, mode, owner, bool(at_reset))
        plan.files.append(handle)
        return handle

    # -- logins built from the methods above ----------------------------------------------

    def add_ssh_key_login(self, vms, key, user=DEFAULT_USER):
        """`user` on each VM (debian unless given; made if it is new) logs in over SSH with the
        key's private half. Put that half somewhere the worm can find it: add_private_ssh_key, or a
        file."""
        plan = self._plan
        plan.defining("add_ssh_key_login")
        for vm in _as_list(vms, "add_ssh_key_login"):
            if user not in ("root", IMAGE_USER):
                self.add_user(vm, user)
            self.add_public_ssh_key(vm, key, user)

    def add_ssh_totp_login(self, vms, key, totp, user):
        """`user` on each VM (made if it is new) logs in over SSH with the key *and* the TOTP's
        current code (keyboard-interactive, pam_google_authenticator). Both have to be found:
        put the private key in a file, and the TOTP's secret in another (TotpExport, totp.uri).
        Only this user needs the code; the host's own access and the other logins do not.
        The VM needs libpam-google-authenticator in the apt cache."""
        plan = self._plan
        plan.defining("add_ssh_totp_login")
        if not isinstance(totp, TOTP) or totp not in plan.secrets:
            raise GameplanError("add_ssh_totp_login: give a TOTP from self.generate_totp()")
        if totp.algorithm != "SHA1" or totp.digits != 6:
            raise GameplanError("add_ssh_totp_login: pam_google_authenticator takes SHA1 and 6 digits")
        if user in ("root", IMAGE_USER):
            raise GameplanError(f"add_ssh_totp_login: give the login its own user (not {user}: the host logs in as it)")
        for vm in _as_list(vms, "add_ssh_totp_login"):
            self.add_ssh_key_login(vm, key, user)
            vm._require("libpam-google-authenticator")
            if TOTP_SETUP not in vm._setup:
                vm.add_setup(TOTP_SETUP)
            vm.add_setup(f"usermod -a -G {TOTP_GROUP} {user}")
            self.write_file(vm, "~/.google_authenticator", f'{totp.secret}\n" STEP_SIZE {totp.period}\n'
                            f'" WINDOW_SIZE {TOTP_WINDOW}\n" TOTP_AUTH\n', mode=0o400, owner=user)  # fmt: skip

    def add_telnet_login(self, vms, login=None):
        """The login's user on each VM, with its password, over telnet (port 23, inetutils
        telnetd under inetd). Only the users of telnet logins get in this way. Open the port
        with firewall rules, as for any service."""
        plan = self._plan
        plan.defining("add_telnet_login")
        login = self._login(login, "add_telnet_login")
        for vm in _as_list(vms, "add_telnet_login"):
            plan.known(vm, "add_telnet_login")
            self._set_password(vm, login)
            vm._require("inetutils-telnetd", "inetutils-inetd")
            if TELNET_SETUP not in vm._setup:
                vm.add_setup(TELNET_SETUP)
            vm.add_setup(f"grep -qx {login.user} {TELNET_USERS} || echo {login.user} >> {TELNET_USERS}")
            vm.add_service("inetutils-inetd")

    def add_ftp_login(self, vms, login=None):
        """The login's user on each VM, with its password, over FTP (vsftpd on port 21, passive
        mode on FTP_PASSIVE_PORTS): read and write wherever the user may, not chrooted. A worm
        can fetch a file this way, or put one, ~/.ssh/authorized_keys for instance. Only the
        users of FTP logins get in. Open 21 and FTP_PASSIVE_PORTS with firewall rules."""
        plan = self._plan
        plan.defining("add_ftp_login")
        login = self._login(login, "add_ftp_login")
        for vm in _as_list(vms, "add_ftp_login"):
            plan.known(vm, "add_ftp_login")
            if vm._files.setdefault("/etc/vsftpd.conf", FTP_CONF) != FTP_CONF:
                raise GameplanError(f"add_ftp_login: {vm.name} writes its own /etc/vsftpd.conf")
            self._set_password(vm, login)
            vm._require("vsftpd")
            # vsftpd reads its user list when it starts.
            vm.add_setup(f"grep -qx {login.user} {FTP_USERS} || echo {login.user} >> {FTP_USERS}; systemctl restart vsftpd")
            vm.add_service("vsftpd")

    def add_local_login(self, vms, login=None):
        """The login's user on each VM, with its password, and no way in from outside: the
        password works for `su` (and for sudo, if the user is in the group sudo). A step for a
        worm that is on the VM already and found the password."""
        plan = self._plan
        plan.defining("add_local_login")
        login = self._login(login, "add_local_login")
        for vm in _as_list(vms, "add_local_login"):
            plan.known(vm, "add_local_login")
            self._set_password(vm, login)

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
