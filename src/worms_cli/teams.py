"""Which team is which: the team map that `worms status`, `login`, `show`, `log`, `rsync` and
`keys` share.

A team's devbox is four VMs named after the team's slug, `t<team id>`
(orchestrator/rounds/devbox.py, team_slug):

    devbox-t2-a, -b, -c   its three VMs (A is the one the SSH gateway forwards to)
    devbox-t2-gw          its NAT gateway (not the shared SSH gateway, devbox-ssh-gateway)

The map comes from the database first (teams, devboxes, devbox_vms: names and slugs). When the
database cannot be reached it is read off the VMs there are: their names, and the inventory's
`devbox_team` (opentofu/stacks/devbox/outputs.tf). Then a team has its slug and no name.

A target names a VM the way an operator thinks of it: its own name, an alias (`ssh-gateway`), or
`<team>/<role>`: `alpha/gw`, `t2/a` (resolve).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ROLES = ("a", "b", "c", "gw")
ROLE_HELP = {"a": "VM A (SSH entry)", "b": "VM B", "c": "VM C", "gw": "its NAT gateway"}
DEVBOX_VM = re.compile(r"devbox-(t\d+)-(a|b|c|gw)")
# A team names itself on the dashboard: only such a name is offered to type (handle) or to tab
# completion (write_cache; completion.SAFE), where a shell may expand it.
SAFE_NAME = re.compile(r"[A-Za-z0-9._+-]+")
SHARED_GATEWAY = "devbox-ssh-gateway"


@dataclass(frozen=True)
class Team:
    slug: str  # t<team id>: the devbox VMs' names
    name: str = ""  # the team's name; "" when only the VMs say the team is there
    devbox: bool = True  # it has a devbox (the database's row, or its VMs)

    @property
    def label(self) -> str:
        """`alpha (t2)`, or `t2` when the name is not known."""
        return f"{self.name} ({self.slug})" if self.name else self.slug

    @property
    def handle(self) -> str:
        """What `<team>/<role>` is best typed with: the name if it is a safe word, else the slug."""
        return self.name if SAFE_NAME.fullmatch(self.name) else self.slug


def vm_name(slug: str, role: str) -> str:
    return f"devbox-{slug}-{role}"


def of_vm(name: str) -> tuple[str, str] | None:
    """(slug, role) of a team's devbox VM by its name, or None."""
    match = DEVBOX_VM.fullmatch(name)
    return (match.group(1), match.group(2)) if match else None


def _from_database(config) -> dict[str, Team] | None:
    """slug -> Team from the database (the current competition's teams, and every team with a
    devbox), or None when it cannot be reached."""
    try:
        from worms_cli import django_setup

        django_setup.connected(config)
        from core.models.competition import ApplicationSetting
        from core.models.devbox import Devbox
        from core.models.team import Team as TeamRow
        from django.db.models import Q
        from orchestrator.rounds.devbox import team_slug

        competition = ApplicationSetting.get_current_competition()
        with_devbox = set(Devbox.objects.values_list("team_id", flat=True))
        rows = TeamRow.objects.filter(Q(competition=competition) | Q(id__in=with_devbox)).order_by("id")
        return {team_slug(t): Team(team_slug(t), t.name, t.id in with_devbox) for t in rows}
    except Exception:  # noqa: BLE001  (not configured, not reachable, tables not there yet: the VMs say it)
        return None


CACHE = "~/.worms/team-map.json"  # what tab completion knows of the teams (worms_cli/completion.py)


def write_cache(teams: dict[str, Team]) -> None:
    """The team map as the database has it, for tab completion, which reads no database. Best
    effort: a TAB without team names is no reason for a command to fail."""
    import json
    import os
    from pathlib import Path

    path = Path(CACHE).expanduser()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        # A name that is no safe word is left out: tab completion offers the slug instead.
        tmp.write_text(json.dumps({"teams": [{"slug": t.slug, "name": t.name if SAFE_NAME.fullmatch(t.name) else ""}
                                             for t in teams.values() if SAFE_NAME.fullmatch(t.slug)]}))  # fmt: skip
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


def team_map(config, vm_names=(), hosts=None, database=True) -> dict[str, Team]:
    """slug -> Team: the database's teams, and every team whose devbox VMs there are (by their
    names, `vm_names`, and the inventory's `devbox_team`, `hosts`) though the database lacks it."""
    teams = _from_database(config) if database else None
    if teams is not None:
        write_cache(teams)
    teams = teams or {}
    found = {of_vm(name)[0] for name in vm_names if of_vm(name)}
    found |= {str(host.get("devbox_team")) for host in (hosts or {}).values() if host.get("devbox_team")}
    for slug in sorted(found - set(teams)):
        teams[slug] = Team(slug)
    return dict(sorted(teams.items(), key=lambda item: int(item[0][1:]) if item[0][1:].isdigit() else 0))


def find(teams: dict[str, Team], text: str) -> Team | None:
    """The team `text` names, by the one rule every command uses (queries.team too): its name,
    then its name in another case, then its slug t<id>. ValueError when a name fits two teams."""
    for same in (lambda name: name == text, lambda name: name.casefold() == text.casefold()):
        named = [t for t in teams.values() if t.name and same(t.name)]
        if len(named) > 1:
            raise ValueError(f"{text!r} names {len(named)} teams ({', '.join(t.label for t in named)}): use its slug")
        if named:
            return named[0]
    return teams.get(text)


def target(teams: dict[str, Team], text: str) -> str | None:
    """The VM name `<team>/<role>` means (`alpha/gw`, `t2/a`), or None if `text` is no such
    target. ValueError for a team or a role that is not there."""
    team_text, slash, role = text.rpartition("/")
    if not slash or not team_text:
        return None
    if role not in ROLES:
        raise ValueError(f"{text!r}: a team's VM is <team>/{'|'.join(ROLES)}")
    team = find(teams, team_text)
    if team is None:
        raise ValueError(f"{text!r}: no team {team_text!r} (`worms status devboxes` lists them)")
    return vm_name(team.slug, role)
