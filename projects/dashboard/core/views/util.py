from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.models.competition import Competition, Round
from core.models.team import Team
from core.models.user import User
from core.models.worms import Upload
from core.views.decorators import get_current_competition, get_users_current_team
from django.conf import settings
from django.http import HttpRequest
from django.shortcuts import render
from django.utils import timezone


@dataclass
class CompetitionState:
    key: str
    label: str
    detail: str
    target: datetime | None = None
    warning: bool = False


def get_competition_state(
    competition: Competition,
    team: Optional[Team] = None,
) -> CompetitionState:
    now = timezone.now()

    # ---------------------------------------------------------
    # 1. TEAM REGISTRATION
    # ---------------------------------------------------------

    if competition.registration_is_open:
        if team is None:
            return CompetitionState(
                key="registration",
                label="TEAM REGISTRATION",
                detail="Registration required",
                target=competition.registration_ends_at,
                warning=True,
            )

        return CompetitionState(
            key="registration",
            label="TEAM REGISTRATION",
            detail=f"Registered as {team.name}",
            target=competition.registration_ends_at,
            warning=False,
        )

    # ---------------------------------------------------------
    # 2. COMPETITION ROUNDS
    # ---------------------------------------------------------

    rounds = list(
        Round.objects.filter(
            round_start__gte=competition.registration_ends_at,
        ).order_by("round_start")
    )

    # ---------------------------------------------------------
    # 2a. CURRENT ROUND
    # ---------------------------------------------------------

    current_round = next(
        (round for round in rounds if round.round_start < now < round.round_end),
        None,
    )

    if current_round is not None:
        return CompetitionState(
            key="round",
            label="ROUND IN PROGRESS",
            detail="Competition round currently running",
            target=current_round.round_end,
            warning=False,
        )

    # ---------------------------------------------------------
    # 2b. NEXT ROUND
    # ---------------------------------------------------------

    next_round_index = next(
        (index for index, round in enumerate(rounds) if now < round.round_start),
        None,
    )

    if next_round_index is not None:
        next_round = rounds[next_round_index]

        # No team means the user cannot have an upload that
        # belongs to a team yet.
        if team is None:
            needs_upload = True

        else:
            # The upload belongs to a USER, but the competition
            # state belongs to a TEAM.
            #
            # Therefore find the latest upload by ANY member
            # of this team.
            latest_upload = (
                Upload.objects.filter(
                    user__team_memberships__team=team,
                )
                .order_by("-created_at")
                .first()
            )

            # For the first round there is no previous round.
            # In that case, an upload must simply exist.
            if next_round_index == 0:
                needs_upload = latest_upload is None

            else:
                previous_round = rounds[next_round_index - 1]

                needs_upload = (
                    latest_upload is None
                    or latest_upload.created_at < previous_round.round_start
                )

        return CompetitionState(
            key="next_round",
            label="NEXT ROUND",
            detail="Round about to begin" if needs_upload else "Round about to begin",
            target=next_round.round_start,
            warning=needs_upload,
        )

    # ---------------------------------------------------------
    # 3. COMPETITION FINISHED
    # ---------------------------------------------------------

    return CompetitionState(
        key="finished",
        label="COMPETITION FINISHED",
        detail="All competition rounds completed",
        target=None,
        warning=False,
    )


def render_with_base_info(
    request: HttpRequest, template, context=None, *args, **kwargs
):
    if context is None:
        context = {}

    user = request.user
    competition = get_current_competition()

    if not user.is_authenticated or competition is None:
        team = None
    else:
        team = get_users_current_team(user, competition)

    context["user"] = user
    context["user_team"] = team
    context["competition_state"] = get_competition_state(
        competition=competition,
        team=team,
    )

    if competition is None:
        context["is_registration_period"] = False
    else:
        context["is_registration_period"] = competition.registration_is_open

    context["debug"] = settings.DEBUG
    if settings.DEBUG:
        context["available_users"] = User.objects.order_by("username")

    return render(request, template, context, *args, **kwargs)
