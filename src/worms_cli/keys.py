"""`worms status keys`: are every team's devbox SSH keys where they belong? (docs/04-dev-environment.md)

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

import subprocess
from concurrent.futures import ThreadPoolExecutor

from worms_cli import output

GATEWAY = "devbox-ssh-gateway"
GATEWAY_FILE = "/home/team/.ssh/authorized_keys"
DEVBOX_FILE = "/root/.ssh/authorized_keys_worms_team"
ROLES = ("a", "b", "c")
PUSH_KINDS = ("devbox_reconfigure", "devbox_provision", "devbox_rebuild")
MAX_BYTES = 1024 * 1024
HEADERS = ["Team", "Keys", "SSH gateway", "A", "B", "C"]
LEGEND = ("● ✓  every key the team has, and no other     ✗ n missing / n old  a key not there yet / one removed, still there\n"
          "○ off  the VM is stopped (not checked)     ? not readable     — no such VM")  # fmt: skip


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
    def __init__(self, config):
        from worms_cli.vms import Catalogue

        self.catalogue = Catalogue(config)

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

    def rows(self, teams) -> tuple[list[list[str]], list[str]]:
        """(table rows, the teams whose keys are not in place, with where) for [(name, slug, {fingerprints}, has devbox)]."""
        vms = {vm["name"]: vm for vm in self.catalogue.vms()}
        wanted = {(GATEWAY, GATEWAY_FILE): vms.get(GATEWAY)}
        for _, slug, _, has_devbox in teams:
            if has_devbox:
                for role in ROLES:
                    name = f"devbox-{slug}-{role}"
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
            a = vms.get(f"devbox-{slug}-a")
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
                body, why = got[(f"devbox-{slug}-{role}", DEVBOX_FILE)]
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


def status(config) -> int:
    """Print the overview; 1 if a team's keys are not in place everywhere they can be checked."""
    from core.models.devbox import Devbox, DevboxSSHKey
    from core.models.rounds import HostJob
    from core.models.team import Team
    from orchestrator.rounds.devbox import team_slug

    from worms_cli import queries

    competition = queries._require_competition()
    teams = []
    for team in Team.objects.filter(competition=competition).order_by("name"):
        expected = {k.key_fingerprint for k in DevboxSSHKey.objects.filter(team=team)}
        teams.append((team.name, team_slug(team), expected, Devbox.objects.filter(team=team).exists()))
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
        print(f"last key push: {last.kind} #{last.id} {state} {when}" + (f": {last.error[:120]}" if last.error else ""))
    if waiting is not None:
        print(output.color(f"a key push is {waiting.state} (#{waiting.id}, {waiting.kind}): the files change once it has run", "yellow"))
    if wrong:
        print(output.status_cell("error", f"not in place: {'; '.join(wrong)}"))
        return 1
    print(output.status_cell("ok", "every team's keys are in place (stopped VMs not checked)"))
    return 0
