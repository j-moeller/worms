"""Database-backed queries and mutations for the CLI, on top of the dashboard's
own models and scoring (imported after django_setup.setup()). The database is
the single source of truth; nothing here reimplements schema or scoring.
"""

from __future__ import annotations


# -- status ------------------------------------------------------------------


def competition_overview() -> dict:
    """Current competition, its rounds, the active/next round and team count."""
    from core import clock
    from core.models.competition import ApplicationSetting, Round
    from core.models.team import Team

    now = clock.now()
    competition = ApplicationSetting.get_current_competition()
    if competition is None:
        return {"competition": None}

    rounds = list(Round.objects.filter(competition=competition).order_by("round_start"))
    active = next((r for r in rounds if r.is_active(now)), None)
    upcoming = next((r for r in rounds if r.round_start and r.round_start > now), None)
    # is_registration_period() assumes both dates are set; a freshly created
    # default competition has none (no registration window yet).
    registration = (
        competition.registration_starts_at is not None
        and competition.registration_ends_at is not None
        and competition.is_registration_period()
    )
    return {
        "competition": competition,
        "now": now,
        "rounds": rounds,
        "n_rounds": len(rounds),
        "active_round": active,
        "next_round": upcoming,
        "n_teams": Team.objects.filter(competition=competition).count(),
        "registration": registration,
    }


def run_overview() -> dict:
    """Round-orchestration run states (docs/rounds.md), if any."""
    from core.models.rounds import HostJob, RoundRun

    by_state: dict[str, int] = {}
    for run in RoundRun.objects.values_list("state", flat=True):
        by_state[run] = by_state.get(run, 0) + 1
    active = (
        RoundRun.objects.exclude(
            state__in=[RoundRun.State.FINISHED, RoundRun.State.FAILED, RoundRun.State.SKIPPED, RoundRun.State.PENDING]
        )
        .select_related("round", "team")
        .first()
    )
    return {
        "by_state": by_state,
        "active": active,
        "queued_jobs": HostJob.objects.filter(state=HostJob.State.QUEUED).count(),
    }


# -- teams --------------------------------------------------------------------


def team_rows() -> list[dict]:
    """Every team of the current competition with membership and the members'
    login status (have they ever logged in / changed their one-time password)."""
    from core.models.competition import ApplicationSetting
    from core.models.team import Team

    competition = ApplicationSetting.get_current_competition()
    if competition is None:
        return []

    rows = []
    teams = (
        Team.objects.filter(competition=competition)
        .prefetch_related("users")
        .order_by("name")
    )
    for team in teams:
        members = list(team.users.all())
        rows.append(
            {
                "name": team.name,
                "uuid": str(team.uuid),
                "n_members": len(members),
                "max_members": team.max_members,
                "members": [
                    {
                        "username": u.username,
                        "logged_in": u.last_login is not None,
                        "must_change_password": u.must_change_password,
                    }
                    for u in members
                ],
                "devbox_version": getattr(team, "devbox_version", None),
            }
        )
    return rows


def user_rows() -> list[dict]:
    """Every user with team assignment and login status."""
    from core.models.competition import ApplicationSetting
    from core.models.team import TeamMembership
    from core.models.user import User

    competition = ApplicationSetting.get_current_competition()
    team_of = {}
    if competition is not None:
        for membership in TeamMembership.objects.filter(
            team__competition=competition
        ).select_related("team"):
            team_of[membership.user_id] = membership.team.name

    rows = []
    for user in User.objects.order_by("username"):
        rows.append(
            {
                "username": user.username,
                "is_staff": user.is_staff,
                "logged_in": user.last_login is not None,
                "must_change_password": user.must_change_password,
                "team": team_of.get(user.id),
            }
        )
    return rows


def create_team(name: str) -> str:
    from core.models.competition import ApplicationSetting
    from core.models.team import Team

    competition = _require_competition()
    if Team.objects.filter(competition=competition, name=name).exists():
        raise ValueError(f"a team named {name!r} already exists in {competition.name!r}")
    Team.objects.create(name=name, competition=competition)
    return f"created team {name!r} in {competition.name!r}"


def rename_team(old: str, new: str) -> str:
    from core.models.team import Team

    competition = _require_competition()
    team = _get_team(competition, old)
    if Team.objects.filter(competition=competition, name=new).exclude(pk=team.pk).exists():
        raise ValueError(f"a team named {new!r} already exists")
    team.name = new
    team.save(update_fields=["name"])
    return f"renamed {old!r} -> {new!r}"


def delete_team(name: str) -> str:
    """Delete a team. Memberships are removed (cascade); the user accounts
    themselves are untouched."""
    competition = _require_competition()
    team = _get_team(competition, name)
    n_members = team.users.count()
    team.delete()
    return f"deleted team {name!r} ({n_members} membership(s) removed; users kept)"


def _require_competition():
    from core.models.competition import ApplicationSetting

    competition = ApplicationSetting.get_current_competition()
    if competition is None:
        raise ValueError("no current competition (run `worms status` / `manage.py init`)")
    return competition


def _get_team(competition, name):
    from core.models.team import Team

    team = Team.objects.filter(competition=competition, name=name).first()
    if team is None:
        raise ValueError(f"no team named {name!r} in {competition.name!r}")
    return team


# -- leaderboard --------------------------------------------------------------


def leaderboard() -> dict:
    """The production leaderboard, scored with the dashboard's own functions."""
    from core import clock
    from core.models.competition import ApplicationSetting, Round
    from core.models.scoring import FlagCapture
    from core.models.team import Team
    from dashboard.views.leaderboard import add_places, add_round_winners, score_teams
    from django.db.models import Count

    competition = ApplicationSetting.get_current_competition()
    if competition is None:
        return {"competition": None, "teams": []}

    now = clock.now()
    teams = list(
        Team.objects.filter(competition=competition)
        .annotate(flag_capture_count=Count("flag_captures"))
        .order_by("name")
    )
    rounds = list(Round.objects.filter(competition=competition).order_by("round_start"))
    captures = list(
        FlagCapture.objects.filter(round__competition=competition)
        .select_related("team", "flag_capture_attempt", "round")
        .order_by("flag_capture_attempt__created_at")
    )
    current_round = next((r for r in rounds if r.is_active(now)), None)

    score_teams(teams=teams, rounds=rounds, captures=captures, current_round=current_round)
    teams.sort(key=lambda t: (-t.total_points, t.name))
    add_places(teams)
    add_round_winners(teams, rounds, captures)

    return {"competition": competition, "current_round": current_round, "teams": teams}
