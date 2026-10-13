from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from core import clock
from core.models.competition import ApplicationSetting, Competition, Round
from core.models.team import TeamMembership
from django.conf import settings
from django.http import HttpRequest

# Before competition (now < registration_start)
# Registration open (registration_start <= now < registration_end)
# Before first round (registration_end <= now < rounds[0].start)
# During round (rounds[i].start <= now < rounds[i].end)
# Between round (rounds[i].end <= now < rounds[i+1].end)
# After last round / after competition (rounds[-1].end <= now)


@dataclass
class CompetitionState:
    state: str
    current_round: Optional[Round] = None
    current_round_idx: Optional[int] = None

    next_round: Optional[Round] = None
    next_round_idx: Optional[int] = None
    prev_round: Optional[Round] = None
    prev_round_idx: Optional[int] = None


def get_competition_state(
    now: datetime, competition: Optional[Competition], rounds: List[Round]
):
    if competition is None:
        return CompetitionState("invalid")

    if (
        competition.registration_starts_at is None
        or competition.registration_ends_at is None
    ):
        return CompetitionState("dev_round")

    if now < competition.registration_starts_at:
        return CompetitionState("pre_registration")

    if competition.is_registration_period():
        return CompetitionState("registration")

    for round_id, round in enumerate(rounds):
        if now < round.round_start:
            if round_id > 0:
                prev_round_idx = round_id - 1
                prev_round = rounds[prev_round_idx]
            else:
                prev_round = prev_round_idx = None

            return CompetitionState(
                "pre_round",
                next_round=rounds[round_id],
                next_round_idx=round_id,
                prev_round=prev_round,
                prev_round_idx=prev_round_idx,
            )
        if round.is_active(now):
            return CompetitionState(
                "round", current_round=round, current_round_idx=round_id
            )

    return CompetitionState("finished")


class UserMetadataMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest):
        request.time_now = clock.now()
        request.is_dev_environment = request.session["is_dev_environment"]

        request.competition = ApplicationSetting.get_current_competition()
        request.team = TeamMembership.get_users_team(request.user, request.competition)

        if request.team is None:
            request.dev_competition = None
        else:
            request.dev_competition = request.team.development_competition

        if request.is_dev_environment:
            request.active_competition = request.dev_competition
            request.rounds = Round.objects.filter(
                competition=request.dev_competition
            ).order_by("round_start")
            request.competition_state = get_competition_state(
                request.time_now, request.dev_competition, request.rounds
            )
        else:
            request.active_competition = request.competition
            request.rounds = Round.objects.filter(
                competition=request.competition
            ).order_by("round_start")
            request.competition_state = get_competition_state(
                request.time_now, request.competition, request.rounds
            )

        request.is_debug_mode = settings.DEBUG

        return self.get_response(request)


class EnvironmentTransitionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest):
        request.session.setdefault("is_dev_environment", False)

        env_toggle = request.GET.get("live")
        if env_toggle is not None:
            request.session["is_dev_environment"] = env_toggle == "0"

        return self.get_response(request)
