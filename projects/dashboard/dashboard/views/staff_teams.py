"""Staff: teams of the current competition and their members.

Staff create, rename and resize teams and move people in and out of them.
Building a new team's devbox is infrastructure work and stays on the console
(`worms devbox provision`, docs/devbox.md); this page says when one is
missing. A team is deleted here only while nothing refers to it; a team with
history (uploads, runs, access tokens, a devbox) is deleted with `worms teams
delete`, which removes it with everything it owns.
"""

from __future__ import annotations

from core.models.devbox import Devbox, DevboxSSHKey
from core.models.rounds import RoundRun
from core.models.team import AccessTokens, Team, TeamMembership
from core.models.user import User
from core.models.worms import Upload
from dashboard.forms import AddMemberForm, TeamForm
from dashboard.membership import remove_member
from dashboard.views.decorators import refuse, staff_required
from dashboard.views.staff import delete_unreferenced
from dashboard.views.util import render_with_base_info
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Max
from django.http import HttpRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

DEVBOX_HINT = "Build its devbox from the console: worms devbox provision."


def _team(request, uuid):
    return get_object_or_404(Team, uuid=uuid, competition=request.competition)


def team_uploads(team):
    return Upload.objects.filter(user__team_memberships__team=team)


@staff_required
def teams(request: HttpRequest):
    competition = request.competition
    if competition is None:
        return refuse(request, "No current competition.", 500)
    form = TeamForm(competition=competition)
    if request.method == "POST":
        form = TeamForm(request.POST, competition=competition)
        if form.is_valid():
            team = Team.objects.create(
                name=form.cleaned_data["name"],
                max_members=form.cleaned_data["max_members"],
                competition=competition,
                development_competition=competition.development_competition,
            )
            messages.success(request, f"Team {team.name} created. {DEVBOX_HINT}")
            return redirect("staff_team", uuid=team.uuid)

    rows = (
        Team.objects.filter(competition=competition)
        .annotate(members=Count("team_memberships", distinct=True))
        .select_related("devbox")
        .order_by("name")
    )
    teams_list = []
    for team in rows:
        latest = team_uploads(team).aggregate(latest=Max("created_at"))["latest"]
        try:
            devbox = team.devbox
        except Devbox.DoesNotExist:
            devbox = None
        teams_list.append({"team": team, "latest_upload": latest, "devbox": devbox})
    return render_with_base_info(request, "staff/teams.html", {
        "teams": teams_list,
        "form": form,
        "without_devbox": sum(1 for row in teams_list if row["devbox"] is None),
    })  # fmt: skip


@staff_required
def team_detail(request: HttpRequest, uuid):
    team = _team(request, uuid)
    form = TeamForm(initial={"name": team.name, "max_members": team.max_members}, competition=team.competition, team=team)
    if request.method == "POST":
        form = TeamForm(request.POST, competition=team.competition, team=team)
        if form.is_valid():
            team.name = form.cleaned_data["name"]
            team.max_members = form.cleaned_data["max_members"]
            team.save(update_fields=["name", "max_members"])
            messages.success(request, f"Saved {team.name}.")
            return redirect("staff_team", uuid=team.uuid)
    devbox = Devbox.objects.filter(team=team).prefetch_related("vms").first()
    return render_with_base_info(request, "staff/team.html", {
        "team": team,
        "form": form,
        "members": team.users.order_by("username"),
        "add_form": AddMemberForm(competition=team.competition),
        "full": team.users.count() >= team.max_members,
        "devbox": devbox,
        "devbox_hint": DEVBOX_HINT,
        "uploads": team_uploads(team).select_related("user", "artifact").order_by("-created_at")[:10],
        "runs": RoundRun.objects.filter(team=team).select_related("round").order_by("-round__round_start"),
        "history": _history(team),
    })  # fmt: skip


def _history(team) -> list[str]:
    """What keeps a team from being deleted here (it refers to the team)."""
    reasons = []
    if team_uploads(team).exists():
        reasons.append("uploads")
    if RoundRun.objects.filter(team=team).exists():
        reasons.append("round runs")
    if AccessTokens.objects.filter(team=team).exists():
        reasons.append("access tokens")
    if Devbox.objects.filter(team=team).exists():
        reasons.append("a devbox")
    return reasons


@staff_required
@require_POST
def team_add_member(request: HttpRequest, uuid):
    team = _team(request, uuid)
    form = AddMemberForm(request.POST, competition=team.competition)
    if not form.is_valid():
        messages.error(request, "Choose a user who is not in a team yet.")
    elif team.users.count() >= team.max_members:
        messages.error(request, f"{team.name} is full ({team.max_members} members); raise its limit first.")
    else:
        TeamMembership.objects.create(team=team, user=form.cleaned_data["user"])
        messages.success(request, f"{form.cleaned_data['user'].username} joined {team.name}.")
    return redirect("staff_team", uuid=team.uuid)


@staff_required
@require_POST
def team_remove_member(request: HttpRequest, uuid, user_uuid):
    team = _team(request, uuid)
    user = get_object_or_404(User, uuid=user_uuid, team_memberships__team=team)
    remove_member(team, user)
    messages.success(request, f"{user.username} left {team.name}; their devbox keys are removed.")
    return redirect("staff_team", uuid=team.uuid)


@staff_required
@require_POST
def team_delete(request: HttpRequest, uuid):
    team = _team(request, uuid)
    reasons = _history(team)
    if not reasons:
        with transaction.atomic():
            TeamMembership.objects.filter(team=team).delete()
            DevboxSSHKey.objects.filter(team=team).delete()
            if not delete_unreferenced(Team, team.pk):
                transaction.set_rollback(True)
                reasons = ["records that refer to it"]
    if reasons:
        messages.error(
            request,
            f"{team.name} has {', '.join(reasons)} and is kept. To remove it with everything it owns: "
            f"worms teams delete {team.name!r} (console).",
        )
        return redirect("staff_team", uuid=team.uuid)
    messages.success(request, f"Team {team.name} deleted; its members have no team now.")
    return redirect("staff_teams")
