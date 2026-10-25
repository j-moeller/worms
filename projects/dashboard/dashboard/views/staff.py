"""Staff administration on the dashboard: the competition (this page), users
(staff_users.py), teams (staff_teams.py) and rounds (staff_rounds.py).

Only database records are managed here -- accounts, teams, the registration
window, round definitions -- with the dashboard's own, column-scoped
database role (setup_db_permissions). Nothing here builds or changes
infrastructure: the sandboxes of a round are built by the round agent on the
Proxmox host when the round starts (docs/rounds.md), devboxes are provisioned
from the console (`worms devbox provision`), and the platform itself with
`worms build`.
"""

from __future__ import annotations

from core import clock
from core.models.competition import Competition, Round
from core.models.team import Team, TeamMembership
from core.models.user import User
from dashboard.forms import CompetitionForm
from dashboard.views.decorators import refuse, staff_required
from dashboard.views.util import render_with_base_info
from django.contrib import messages
from django.db import IntegrityError, connection, transaction
from django.http import HttpRequest
from django.shortcuts import redirect
from django.views.decorators.http import require_POST


def delete_unreferenced(model, pk) -> bool:
    """Delete one row only if nothing refers to it. The dashboard's role has
    no delete rights on the tables a cascade would touch (captures, runs,
    tokens, ...), so instead of Django's cascade this deletes the row alone
    and lets the foreign keys refuse it (checked at once, not at commit).
    Returns False if something still refers to it."""
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                if connection.vendor == "postgresql":
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                cursor.execute(f'DELETE FROM "{model._meta.db_table}" WHERE id = %s', [pk])
    except IntegrityError:
        return False
    return True


def competition_summary(competition) -> dict:
    """Counts for the staff home page (what `worms status` showed for the
    competition, from the database only)."""
    now = clock.now()
    teams = Team.objects.filter(competition=competition)
    in_team = TeamMembership.objects.filter(team__competition=competition).values("user")
    students = User.objects.filter(is_staff=False, is_superuser=False, is_active=True)
    rounds = list(Round.objects.filter(competition=competition).order_by("round_start"))
    return {
        "teams": teams.count(),
        "empty_teams": teams.filter(team_memberships__isnull=True).count(),
        "students": students.count(),
        "without_team": students.exclude(pk__in=in_team).count(),
        "one_time": students.filter(must_change_password=True).count(),
        "never_logged_in": students.filter(last_login__isnull=True).count(),
        "staff": User.objects.filter(is_staff=True, is_active=True).count(),
        "rounds": len(rounds),
        "upcoming": [r for r in rounds if r.round_start and r.round_start > now],
        "active": next((r for r in rounds if r.round_start and r.is_active(now)), None),
    }


@staff_required
def competition(request: HttpRequest):
    """The current competition: its name and registration window, its
    development competition, and what still needs doing."""
    comp = request.competition
    if comp is None:
        return refuse(request, "No current competition (manage.py init creates one).", 500)
    if request.method == "POST":
        form = CompetitionForm(request.POST)
        if form.is_valid():
            comp.name = form.cleaned_data["name"]
            comp.registration_starts_at = form.cleaned_data["registration_starts_at"]
            comp.registration_ends_at = form.cleaned_data["registration_ends_at"]
            comp.save(update_fields=["name", "registration_starts_at", "registration_ends_at"])
            messages.success(request, "Competition saved.")
            return redirect("staff_competition")
    else:
        form = CompetitionForm(initial={
            "name": comp.name,
            "registration_starts_at": comp.registration_starts_at,
            "registration_ends_at": comp.registration_ends_at,
        })  # fmt: skip
    summary = competition_summary(comp)
    dev = comp.development_competition
    return render_with_base_info(request, "staff/competition.html", {
        "form": form,
        "comp": comp,
        "dev_competition": dev,
        "teams_without_dev": Team.objects.filter(competition=comp, development_competition__isnull=True).count(),
        "summary": summary,
        "registration_open": comp.is_registration_period(),
    })  # fmt: skip


@staff_required
@require_POST
def development_competition(request: HttpRequest):
    """Create the competition's development competition (the Dev environment:
    devbox progression windows) if it has none, and give it to every team of
    the competition that has none."""
    comp = request.competition
    if comp is None:
        return refuse(request, "No current competition.", 500)
    with transaction.atomic():
        dev = comp.development_competition
        if dev is None:
            dev = Competition.objects.create(name=f"{comp.name} · dev", is_dev_competition=True)
            comp.development_competition = dev
            comp.save(update_fields=["development_competition"])
        updated = Team.objects.filter(competition=comp, development_competition__isnull=True).update(
            development_competition=dev
        )
    messages.success(request, f"Development competition {dev.name!r}; {updated} team(s) linked to it.")
    return redirect("staff_competition")
