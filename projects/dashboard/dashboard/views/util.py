from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.models.competition import Competition, Round
from core.models.scoring import FlagCapture
from core.models.team import Team, TeamMembership
from core.models.user import User
from core.models.worms import Upload
from dashboard.middleware import CompetitionState
from django.conf import settings
from django.db.models import BooleanField, Exists, OuterRef, Value
from django.db.models.aggregates import Count
from django.http import HttpRequest
from django.shortcuts import render


@dataclass
class CompetitionPresentation:
    key: str
    label: str
    detail: str
    target: datetime | None = None
    warning: bool = False
    # The time left until `target`, rendered on the server (so it reads right
    # without JavaScript; enhance.js keeps it ticking).
    remaining: str = ""


def format_remaining(delta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{days}d {clock}" if days else clock


def get_current_round(rounds: list[Round], now) -> Round | None:
    """Return the currently active round, if there is one."""
    return next(
        (round for round in rounds if round.is_active(now)),
        None,
    )


def get_teams(
    competition: Competition,
    request: HttpRequest,
) -> list[Team]:
    """Return all teams with their leaderboard metadata."""
    if request.user.is_authenticated:
        user_team_membership = TeamMembership.objects.filter(
            team=OuterRef("pk"),
            user=request.user,
        )

        is_user_team = Exists(user_team_membership)
    else:
        is_user_team = Value(False, output_field=BooleanField())

    if request.session.get("is_dev_environment"):
        team_filter = Team.objects.filter(development_competition=competition)
    else:
        team_filter = Team.objects.filter(competition=competition)

    return list(
        team_filter.annotate(
            is_user_team=is_user_team,
            flag_capture_count=Count("flag_captures"),
        ).order_by("name")
    )


def get_rounds(competition: Competition) -> list[Round]:
    """Return competition rounds ordered chronologically."""
    return list(Round.objects.filter(competition=competition).order_by("round_start"))


def get_flag_captures(
    competition: Competition,
) -> list[FlagCapture]:
    """Return all flag captures in chronological order."""
    return list(
        FlagCapture.objects.filter(round__competition=competition)
        .select_related(
            "team",
            "flag_capture_attempt",
            "round",
        )
        .order_by("flag_capture_attempt__created_at")
    )


def get_competition_state_presentation(
    state: CompetitionState,
    competition: Competition,
    team: Optional[Team] = None,
) -> CompetitionPresentation:
    if state.state == "invalid":
        return CompetitionPresentation(
            key="invalid",
            label="",
            detail="No competition right now",
            target=None,
            warning=False,
        )

    elif state.state == "dev_round":
        return CompetitionPresentation(
            key="round",
            label=f"ROUND IN PROGRESS (DEV)",
            detail="Competition round currently running",
            target=None,
            warning=False,
        )

    elif state.state == "pre_registration":
        return CompetitionPresentation(
            key="pre_registration",
            label="TEAM REGISTRATION IN",
            detail="Registration about to begin",
            target=competition.registration_starts_at,
            warning=False,
        )

    elif state.state == "registration":
        if team is None:
            return CompetitionPresentation(
                key="registration",
                label="TEAM REGISTRATION",
                detail="Registration required",
                target=competition.registration_ends_at,
                warning=True,
            )

        return CompetitionPresentation(
            key="registration",
            label="TEAM REGISTRATION",
            detail=f"Registered as {team.name}",
            target=competition.registration_ends_at,
            warning=False,
        )

    elif state.state == "round":
        assert state.current_round is not None
        assert state.current_round_idx is not None
        return CompetitionPresentation(
            key="round",
            label=f"ROUND IN PROGRESS ({state.current_round.name})",
            detail="Competition round currently running",
            target=state.current_round.round_end,
            warning=False,
        )

    elif state.state == "pre_round":
        assert state.next_round is not None
        assert state.next_round_idx is not None

        # No team means the user cannot have an upload that
        # belongs to a team yet.
        if team is None:
            needs_upload = True

        else:
            # The team's latest upload (made for this team, by any member).
            latest_upload = (
                Upload.objects.filter(
                    team=team,
                )
                .order_by("-created_at")
                .first()
            )

            # For the first round there is no previous round.
            # In that case, an upload must simply exist.
            if state.next_round_idx == 0:
                needs_upload = latest_upload is None
            else:
                assert state.prev_round is not None
                assert state.prev_round_idx is not None
                needs_upload = (
                    latest_upload is None
                    or latest_upload.created_at < state.prev_round.round_start
                )

        return CompetitionPresentation(
            key="next_round",
            label=f"NEXT ROUND ({state.next_round.name})",
            detail="Round about to begin",
            target=state.next_round.round_start,
            warning=needs_upload,
        )

    # ---------------------------------------------------------
    # 3. COMPETITION FINISHED
    # ---------------------------------------------------------

    elif state.state == "finished":
        return CompetitionPresentation(
            key="finished",
            label="COMPETITION FINISHED",
            detail="All competition rounds completed",
            target=None,
            warning=False,
        )

    assert False, f"Unknown competition state: '{state.state}'"


def render_with_base_info(
    request: HttpRequest,
    template,
    context=None,
    *args,
    **kwargs,
):
    if context is None:
        context = {}

    if settings.DEBUG:
        request.available_users = User.objects.order_by("username")

    request.state_presentation = get_competition_state_presentation(
        request.competition_state, request.active_competition, request.team
    )
    if request.state_presentation.target:
        request.state_presentation.remaining = format_remaining(
            request.state_presentation.target - request.time_now
        )

    return render(request, template, context, *args, **kwargs)
