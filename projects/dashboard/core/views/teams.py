from django.db import transaction

from core.models.competition import Competition
from core.models.team import Team
from core.views.decorators import (
    get_users_current_team,
    require_competition,
    require_competition_registration_active,
)
from core.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST


@login_required
@require_competition
@require_competition_registration_active
def team_registration(request: HttpRequest, competition: Competition):
    return render_with_base_info(
        request,
        "team_registration.html",
        {
            "user": request.user,
            "competition": competition,
        },
    )


@login_required
@require_POST
@require_competition
@require_competition_registration_active
def join_team(request: HttpRequest, uuid, competition: Competition):
    with transaction.atomic():
        if get_users_current_team(request.user, competition) is not None:
            return redirect("team_registration")

        team = get_object_or_404(Team, uuid=uuid)
        if team.users.count() < team.max_members:
            team.users.add(request.user)

        return redirect("team_registration")


@login_required
@require_POST
@require_competition
@require_competition_registration_active
def leave_team(request: HttpRequest, uuid, competition):
    team = get_object_or_404(Team, uuid=uuid)
    team.users.remove(request.user)

    return redirect("team_registration")
