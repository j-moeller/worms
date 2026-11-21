"""`worms key`: are every team's devbox SSH keys where they belong? `worms key sync`:
put them there. (docs/04-dev-environment.md)

The dashboard holds each team's keys (devbox_ssh_keys); the round agent pushes them, with every
devbox_reconfigure, devbox_provision and devbox_rebuild job, to two places:

    the SSH gateway (devbox-ssh-gateway)  /home/team/.ssh/authorized_keys: one confined line per
                                          key, ending in team=<slug>; none for a team without VM A
    the team's devbox VMs A, B and C      /root/.ssh/authorized_keys_worms_team

This reads those files over the management SSH, as `worms login` does (the WORMS key in its own
agent, never forwarded), and compares their keys with the database's by fingerprint (type and
blob, as devbox_ssh_keys.key_fingerprint). The VMs are assumed rooted by their team: what they
return is only parsed for keys and counted, never printed, and a team can only misreport its own.
"""

from __future__ import annotations

import base64
import ipaddress
import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor

from worms_cli import output
from worms_cli.teams import vm_name

GATEWAY = "devbox-ssh-gateway"
GATEWAY_FILE = "/home/team/.ssh/authorized_keys"
DEVBOX_FILE = "/root/.ssh/authorized_keys_worms_team"
ROLES = ("a", "b", "c")
PUSH_KINDS = ("devbox_reconfigure", "devbox_provision", "devbox_rebuild")
MAX_BYTES = 1024 * 1024
HEADERS = ["Team", "Keys", "SSH gateway", "A", "B", "C"]
LEGEND = ("● ✓  every key the team has, and no other     ✗ n missing / n old  a key not there yet / one removed, still there\n"
          "○ off  the VM is stopped (not checked)     ? not readable     — no such VM\n"
          "A, B, C: devbox-<slug>-a, -b, -c (`worms show <team>`, `worms login <team>/a`); "
          "SSH gateway: devbox-ssh-gateway, its lines for the team")  # fmt: skip


# `worms key sync`: what is written, by one ssh command per VM, the keys on its stdin (never
# in a command line). A devbox VM: its team's key file, and sshd's drop-in that reads it should it
# be missing. The gateway: the whole file, as the ssh_gateway role writes it (authorized_keys.j2).
KEY_TYPE = re.compile(r"(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521)|sk-[a-z0-9@.-]+)")
SSHD_DROP_IN = "/etc/ssh/sshd_config.d/20-worms-devbox.conf"
DEVBOX_SCRIPT = (
    f"install -d -m 700 /root/.ssh && install -m 600 /dev/stdin {DEVBOX_FILE}"
    f" && if ! grep -qs authorized_keys_worms_team {SSHD_DROP_IN}; then"
    f" echo \"AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys_worms_team\" > {SSHD_DROP_IN}"
    " && systemctl reload ssh; fi"
)
DEVBOX_WRITE = f"sudo sh -c {shlex.quote(DEVBOX_SCRIPT)}"  # the round agent runs it too (handlers.devbox_rebuild)
GATEWAY_WRITE = f"sudo install -m 644 -o root -g root /dev/stdin {GATEWAY_FILE}"
# The internal CA a devbox VM must trust: a worm reaches scoring and logging over verified HTTPS. A VM
# rolled back to its first-boot snapshot lacks it (role ca_distribute installs it in a configuration).
CA_FILE = "ansible/pki/worms/ca.crt"  # in the checkout, and in the host's workspace
CA_SCRIPT = "install -m 644 /dev/stdin /usr/local/share/ca-certificates/worms-internal-ca.crt && update-ca-certificates"
CA_WRITE = f"sudo sh -c {shlex.quote(CA_SCRIPT)}"
GATEWAY_HEADER = "# WORMS devbox SSH gateway authorized_keys (`worms key sync`, role ssh_gateway). DO NOT EDIT."
GATEWAY_LINE = 'restrict,port-forwarding,command="/usr/sbin/nologin",permitopen="{ip}:22" {key} team={slug}'


def canonical(public_key: str) -> str | None:
    """`type blob` of a team's key, or None if it is not one: a known type, base64 that decodes,
    and a blob that names the same type (RFC 4253). Its comment is left out, so nothing the team
    typed reaches a file but the key itself."""
    parts = str(public_key).split()
    if len(parts) < 2 or not KEY_TYPE.fullmatch(parts[0]):
        return None
    try:
        raw = base64.b64decode(parts[1], validate=True)
    except ValueError:
        return None
    size = int.from_bytes(raw[:4], "big") if len(raw) >= 4 else 0
    if raw[4:4 + size] != parts[0].encode():
        return None
    return f"{parts[0]} {base64.b64encode(raw).decode()}"


def _fingerprint(key_type: str, blob: str) -> str | None:
    from core.models.devbox import DevboxSSHKey

    try:
        return DevboxSSHKey.fingerprint(f"{key_type} {blob}")
    except (ValueError, TypeError):
        return None


def gateway_keys(text: str) -> dict[str, set[str]]:
    """{team slug: fingerprints} of the gateway's file: lines `restrict,... type blob [...] team=<slug>`."""
    found: dict[str, set[str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].startswith("restrict") and parts[-1].startswith("team="):
            fp = _fingerprint(parts[1], parts[2])
            if fp:
                found.setdefault(parts[-1][len("team="):], set()).add(fp)
    return found


def devbox_keys(text: str) -> set[str]:
    """Fingerprints of a devbox VM's team key file: lines `type blob [comment]`."""
    found = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not parts[0].startswith("#"):
            fp = _fingerprint(parts[0], parts[1])
            if fp:
                found.add(fp)
    return found


def cell(expected: set[str], found: set[str]) -> tuple[bool, str]:
    """(in place, the table's cell) for one place of one team's keys."""
    missing, old = len(expected - found), len(found - expected)
    if not missing and not old:
        return True, output.status_cell("ok", "✓")
    return False, output.status_cell("error", ", ".join(x for x in (f"{missing} missing" if missing else "",
                                                                     f"{old} old" if old else "") if x))  # fmt: skip


class KeyCheck:
    def __init__(self, config, catalogue=None):
        from worms_cli.vms import Catalogue

        self.catalogue = catalogue or Catalogue(config)

    def read(self, vm: dict | None, path: str) -> tuple[str | None, str]:
        """(the file's text, "") or (None, why not): "off", "absent", "unreadable"."""
        if vm is None:
            return None, "absent"
        if vm.get("status") != "running":
            return None, "off"
        try:
            argv, env = self.catalogue.ssh_argv(vm, f"sudo -n cat {path}", interactive=False)
            proc = subprocess.run(argv, env=env, capture_output=True, timeout=40)
        except Exception:  # noqa: BLE001  (a timeout, no address in the inventories: unreadable all the same)
            return None, "unreadable"
        if proc.returncode != 0:
            # No such file: never pushed there (an empty list is written as an empty file).
            return ("", "") if b"No such file" in proc.stderr[:4096] else (None, "unreadable")
        return proc.stdout[:MAX_BYTES].decode(errors="replace"), ""

    def write(self, vm: dict | None, command: str, text: str) -> str:
        """"" if `text` is written on `vm` by `command`, else why not: "off", "absent", "failed"."""
        if vm is None:
            return "absent"
        if vm.get("status") != "running":
            return "off"
        try:
            argv, env = self.catalogue.ssh_argv(vm, command, interactive=False)
            proc = subprocess.run(argv, env=env, input=text.encode(), capture_output=True, timeout=60)
        except Exception:  # noqa: BLE001  (a timeout, no address in the inventories)
            return "failed"
        return "" if proc.returncode == 0 else "failed"

    def rows(self, teams) -> tuple[list[list[str]], list[str]]:
        """(table rows, the teams whose keys are not in place, with where) for [(name, slug, {fingerprints}, has devbox)]."""
        vms = {vm["name"]: vm for vm in self.catalogue.vms()}
        wanted = {(GATEWAY, GATEWAY_FILE): vms.get(GATEWAY)}
        for _, slug, _, has_devbox in teams:
            if has_devbox:
                for role in ROLES:
                    name = vm_name(slug, role)
                    wanted[(name, DEVBOX_FILE)] = vms.get(name)
        with ThreadPoolExecutor(max_workers=8) as pool:
            got = dict(zip(wanted, pool.map(lambda item: self.read(item[1], item[0][1]), wanted.items())))
        gateway_text, gateway_problem = got[(GATEWAY, GATEWAY_FILE)]
        gateway = gateway_keys(gateway_text) if gateway_text is not None else None
        rows, wrong = [], []
        for name, slug, expected, has_devbox in teams:
            label = f"{name} ({slug})"
            if not has_devbox:
                rows.append([label, str(len(expected)), output.status_cell("n/a", "no devbox"), "", "", ""])
                continue
            problems, cells = [], []
            a = vms.get(vm_name(slug, "a"))
            if gateway is None:
                ok, text = gateway_problem in ("off", "absent"), self._unread(gateway_problem)
            elif a is None:
                ok, text = True, output.status_cell("n/a", "— no VM A")  # the gateway has no line for it, by design
            else:
                ok, text = cell(expected, gateway.get(slug, set()))
            cells.append(text)
            if not ok:
                problems.append("gateway")
            for role in ROLES:
                body, why = got[(vm_name(slug, role), DEVBOX_FILE)]
                ok, text = (cell(expected, devbox_keys(body)) if body is not None else (why in ("off", "absent"), self._unread(why)))
                cells.append(text)
                if not ok:
                    problems.append(role.upper())
            rows.append([label, str(len(expected)), *cells])
            if problems:
                wrong.append(f"{name} ({', '.join(problems)})")
        return rows, wrong

    @staticmethod
    def _unread(why: str) -> str:
        return {"off": output.status_cell("stopped", "off"), "absent": output.status_cell("n/a", "—")}.get(
            why, output.status_cell("unknown", "not readable"))


def _teams():
    """[(name, slug, [key texts], has a devbox)] of the current competition, by name."""
    from core.models.devbox import Devbox, DevboxSSHKey
    from core.models.team import Team
    from orchestrator.rounds.devbox import team_slug

    from worms_cli import queries

    competition = queries._require_competition()
    return [
        (team.name, team_slug(team), list(DevboxSSHKey.objects.filter(team=team).order_by("id").values_list("public_key", flat=True)),
         Devbox.objects.filter(team=team).exists())
        for team in Team.objects.filter(competition=competition).order_by("name")
    ]  # fmt: skip


def status(config) -> int:
    """Print the overview; 1 if a team's keys are not in place everywhere they can be checked."""
    from core.models.devbox import DevboxSSHKey
    from core.models.rounds import HostJob

    from worms_cli import queries

    teams = [(name, slug, {DevboxSSHKey.fingerprint(k) for k in texts}, has) for name, slug, texts, has in _teams()]
    if not teams:
        print("No teams (or no current competition).")
        return 0
    rows, wrong = KeyCheck(config).rows(teams)
    print(output.table(rows, headers=HEADERS))
    print(output.color(LEGEND, "dim"))
    pushes = HostJob.objects.filter(kind__in=PUSH_KINDS)
    last = pushes.exclude(state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING]).order_by("-id").first()
    waiting = pushes.filter(state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING]).order_by("id").first()
    if last is not None:
        when = queries._when(last.finished_at or last.requested_at)
        state = output.status_cell("ok" if last.state == HostJob.State.DONE else "error", last.state)
        print(f"last key push by the round agent: {last.kind} #{last.id} {state} {when}" + (f": {last.error[:120]}" if last.error else ""))
    if waiting is not None:
        print(output.color(f"a key push is {waiting.state} (#{waiting.id}, {waiting.kind}): the files change once it has run", "yellow"))
    if wrong:
        print(output.status_cell("error", f"not in place: {'; '.join(wrong)}") + output.color("  (`worms key sync` copies them)", "dim"))
        return 1
    print(output.status_cell("ok", "every team's keys are in place (stopped VMs not checked)"))
    return 0


def team_cells(config, slug: str, catalogue=None) -> dict[str, str]:
    """`worms show <team>`: one team's keys where they belong, as `worms key` reads them:
    {"a", "b", "c": its VMs' cells, "gateway": the SSH gateway's lines for it, "keys": how many}.
    Empty for a team without a devbox; a cell says so when the database cannot be reached."""
    try:
        from worms_cli import django_setup

        django_setup.connected(config)
        from core.models.devbox import DevboxSSHKey

        entry = next((t for t in _teams() if t[1] == slug), None)
    except Exception as exc:  # noqa: BLE001  (no database: say so in the cells, show the rest)
        unknown = output.status_cell("unknown", f"no database ({str(exc).splitlines()[0][:40]})")
        return {"a": unknown, "b": unknown, "c": unknown, "gateway": unknown}
    if entry is None or not entry[3]:
        return {}
    name, slug, texts, has_devbox = entry
    rows, _ = KeyCheck(config, catalogue).rows([(name, slug, {DevboxSSHKey.fingerprint(k) for k in texts}, has_devbox)])
    _, count, gateway, a, b, c = rows[0]
    return {"keys": count, "gateway": gateway, "a": a, "b": b, "c": c}


def sync(config) -> int:
    """`worms key sync`: copy every team's SSH keys from the database to where they are used,
    over the management SSH, then show them (`worms key`). Nothing else: no round agent, no
    Ansible, no OpenTofu, no internet. A stopped VM is skipped (the next copy, or its reset, writes it)."""
    teams = _teams()
    if not teams:
        print("No teams (or no current competition).")
        return 0
    check = KeyCheck(config)
    vms = {vm["name"]: vm for vm in check.catalogue.vms()}
    hosts = check.catalogue.hosts()
    gateway, writes = [GATEWAY_HEADER], []
    ca = (config.repo_root / CA_FILE).read_text()
    for name, slug, texts, has_devbox in teams:
        if not has_devbox:
            continue
        keys = [k for k in map(canonical, texts) if k]
        if len(keys) != len(texts):
            output.warn(f"{name}: {len(texts) - len(keys)} key(s) are not SSH public keys and are left out")
        # The gateway lets a key forward to its team's VM A only; without one, no line (role ssh_gateway).
        a = hosts.get(vm_name(slug, "a"), {}).get("devbox_ssh_ip")
        try:
            a = str(ipaddress.ip_address(a)) if a and vm_name(slug, "a") in vms else None
        except ValueError:
            a = None
        gateway += [GATEWAY_LINE.format(ip=a, key=key, slug=slug) for key in keys] if a else []
        for role in ROLES:
            writes.append((vm_name(slug, role), DEVBOX_WRITE, "".join(f"{key}\n" for key in keys)))
            writes.append((vm_name(slug, role), CA_WRITE, ca))
    writes.append((GATEWAY, GATEWAY_WRITE, "\n".join(gateway) + "\n"))
    print(f"copying the keys of {sum(1 for t in teams if t[3])} team(s), and the internal CA, to their devboxes ...", flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda w: (w[0], check.write(vms.get(w[0]), w[1], w[2])), writes))
    for vm, why in sorted(set(results)):  # each VM once (its keys and the CA)
        if why == "off":
            print(output.status_cell("stopped", f"{vm}: stopped, skipped"))
        elif why == "failed":
            print(output.status_cell("error", f"{vm}: could not write (`worms login {vm}`)"))
    trusted = sorted({vm for (vm, why), (_, command, _) in zip(results, writes) if command == CA_WRITE and not why})
    if trusted:
        print(output.status_cell("ok", f"the internal CA is trusted on {len(trusted)} devbox VM(s)"))
    print()
    return status(config)
