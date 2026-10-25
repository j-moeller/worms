"""Team membership changes and the devbox calls they trigger, shared by the
students' own join/leave (dashboard/views/teams.py) and the staff pages
(dashboard/views/staff_teams.py, staff_users.py).

A member who leaves or is removed loses the devbox SSH keys they registered,
and the remaining keys are re-pushed (docs/devbox.md), so their access ends.
The re-push goes through the orchestrator (the dashboard cannot enqueue host
jobs) and is best effort: the next provision or reset repeats it. Building a
devbox for a new team is infrastructure work and is not done from here: the
operator runs `worms devbox provision` (docs/devbox.md).
"""

from __future__ import annotations

from core.models.devbox import Devbox, DevboxSSHKey
from core.models.team import TeamMembership
from dashboard import orchestrator_client
from django.db import transaction


def remove_member(team, user) -> int:
    """Remove `user` from `team` with their devbox keys; re-push the keys if
    any were removed. Returns the number of keys removed."""
    with transaction.atomic():
        TeamMembership.objects.filter(team=team, user=user).delete()
        removed, _ = DevboxSSHKey.objects.filter(team=team, created_by=user).delete()
    if removed and Devbox.objects.filter(team=team).exists():
        request_devbox_reconfigure(team)
    return removed


def request_devbox_reconfigure(team) -> str | None:
    """Ask the orchestrator to re-push every team's SSH keys. Returns an error
    message, or None on success."""
    try:
        orchestrator_client.post(f"/devbox/{team.uuid}/reconfigure/")
    except orchestrator_client.OrchestratorError as exc:
        return str(exc)
    return None

