import uuid

from core.models.competition import Competition
from core.models.team import Team, TeamMembership
from core.models.user import User
from dashboard.views.decorators import (
    require_competition,
    require_competition_registration_active,
    user_not_in_team_required,
)
from dashboard.views.util import render_with_base_info
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count
from django.http import HttpRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST


@login_required
@require_competition_registration_active
def team_registration(request: HttpRequest, competition: Competition):
    """The competition's teams with their occupancy; join or leave while
    registration is open."""
    teams = (
        Team.objects.filter(competition=competition)
        .annotate(member_count=Count("team_memberships"))
        .order_by("name")
    )
    mine = set(
        TeamMembership.objects.filter(user=request.user, team__competition=competition).values_list("team_id", flat=True)
    )
    rows = [
        {"team": team, "count": team.member_count, "full": team.member_count >= team.max_members, "mine": team.pk in mine}
        for team in teams
    ]
    return render_with_base_info(request, "team_registration.html", {"rows": rows})


@login_required
@require_POST
@user_not_in_team_required
@require_competition_registration_active
def join_team(request: HttpRequest, uuid: uuid.UUID, competition: Competition):
    with transaction.atomic():
        # Lock the user, then the team: two joins at once can neither overfill
        # a team nor put one person into two teams of the competition
        # (docs/security-assessment.md, D4).
        User.objects.select_for_update().get(pk=request.user.pk)
        team = get_object_or_404(Team.objects.select_for_update(), uuid=uuid, competition=competition)
        if TeamMembership.objects.filter(user=request.user, team__competition=competition).exists():
            messages.error(request, "You are already in a team.")
        elif team.users.count() < team.max_members:
            TeamMembership.objects.create(user=request.user, team=team)
            messages.success(request, f"You joined {team.name}.")
        else:
            messages.error(request, f"{team.name} is full.")

        return redirect("team_registration")


@login_required
@require_POST
@require_competition_registration_active
def leave_team(request: HttpRequest, uuid, competition: Competition):
    from dashboard.membership import remove_member

    team = get_object_or_404(Team, uuid=uuid, competition=competition)
    if not team.users.filter(pk=request.user.pk).exists():
        messages.error(request, f"You are not in {team.name}.")
        return redirect("team_registration")
    # Removes the keys this member registered and re-pushes the rest, so the
    # departing member loses devbox access (docs/security-assessment.md).
    remove_member(team, request.user)
    messages.success(request, f"You left {team.name}.")
    return redirect("team_registration")
