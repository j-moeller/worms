from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from core import clock
from core.models.competition import ApplicationSetting, Competition, Round
from core.models.team import TeamMembership
from dashboard import themes
from django.conf import settings
from django.http import HttpRequest
from django.shortcuts import redirect
from django.urls import reverse

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
        # A round without dates is always active (Round.is_active).
        if round.round_start is not None and now < round.round_start:
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


# The team API (dashboard/api.py) is token-authenticated and must not create
# or touch a session: an anonymous call would otherwise insert a session row
# per request (a database-write amplifier for anyone on the internet).
API_PREFIX = "/api/"


class UserMetadataMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest):
        if request.path.startswith(API_PREFIX):
            return self.get_response(request)
        request.time_now = clock.now()
        request.theme = themes.resolve(request)
        request.theme_css = themes.stylesheet(request.theme)
        request.themes = list(themes.THEMES.items())
        # Default rather than require the key: EnvironmentTransitionMiddleware
        # only sets it for authenticated users, so reading it must not assume it
        # exists and must not itself create a session (docs/security-assessment.md, D1).
        request.is_dev_environment = request.session.get("is_dev_environment", False)

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
        # Only authenticated users have a dev/prod view to toggle. Touching the
        # session for anonymous requests would write a django_session row for
        # every cookie-less request (e.g. a crawler or a flood), filling the
        # shared database disk for no reason (docs/security-assessment.md, D1).
        if request.path.startswith(API_PREFIX) or not request.user.is_authenticated:
            return self.get_response(request)
        # Switching between the two views is a POST (views/environment.py),
        # never a side effect of a GET.
        request.session.setdefault("is_dev_environment", False)

        return self.get_response(request)


class ForcePasswordChangeMiddleware:
    """Hold a student on the change-password page until the one-time password
    from create_users is replaced (user.must_change_password). Everything but
    the change-password page itself, logout, and static files is redirected."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and getattr(user, "must_change_password", False)
        ):
            allowed = {
                reverse("change_password"),
                reverse("logout"),
            }
            static_prefix = "/" + settings.STATIC_URL.lstrip("/")
            path = request.path
            if path not in allowed and not path.startswith(static_prefix):
                return redirect("change_password")

        return self.get_response(request)
