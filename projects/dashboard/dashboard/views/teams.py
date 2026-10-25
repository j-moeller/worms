import uuid

from core.models.competition import Competition
from core.models.team import Team, TeamMembership
from dashboard.views.decorators import (
    require_competition,
    require_competition_registration_active,
    user_not_in_team_required,
)
from dashboard.views.util import render_with_base_info
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST


@login_required
@require_competition_registration_active
def team_registration(request: HttpRequest, competition: Competition):
    return render_with_base_info(request, "team_registration.html")


@login_required
@require_POST
@user_not_in_team_required
@require_competition_registration_active
def join_team(request: HttpRequest, uuid: uuid.UUID, competition: Competition):
    with transaction.atomic():
        team = get_object_or_404(Team, uuid=uuid)
        if team.users.count() < team.max_members:
            team.users.add(request.user)
            messages.success(request, f"You joined {team.name}.")
        else:
            messages.error(request, f"{team.name} is full.")

        return redirect("team_registration")


@login_required
@require_POST
@require_competition_registration_active
def leave_team(request: HttpRequest, uuid, competition: Competition):
    from core.models.devbox import Devbox, DevboxSSHKey
    from dashboard import orchestrator_client

    team = get_object_or_404(Team, uuid=uuid)
    with transaction.atomic():
        team.users.remove(request.user)
        # Remove the keys this member registered: they should not keep devbox
        # access after leaving (docs/security-assessment.md).
        removed, _ = DevboxSSHKey.objects.filter(team=team, created_by=request.user).delete()
    messages.success(request, f"You left {team.name}.")

    # Re-push the remaining keys so the departing member's access is revoked on
    # the gateway and the VMs (best effort; the next provision/reset also does).
    if removed and Devbox.objects.filter(team=team).exists():
        try:
            orchestrator_client.post(f"/devbox/{team.uuid}/reconfigure/")
        except orchestrator_client.OrchestratorError:
            pass

    return redirect("team_registration")
