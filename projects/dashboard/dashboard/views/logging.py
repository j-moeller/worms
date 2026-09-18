from core.models.competition import Competition
from core.models.logging import Logging
from core.models.team import Team
from dashboard.views.decorators import user_in_team_required
from dashboard.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404, render


@login_required
@user_in_team_required
def logs(request: HttpRequest, team: Team, *, competition: Competition):
    events = (
        Logging.objects.filter(team=team)
        .select_related("round", "logging_attempt")
        .order_by("-created_at")
    )

    rounds = []

    for event in events:
        if not rounds or rounds[-1]["round"].pk != event.round.pk:
            rounds.append(
                {
                    "round": event.round,
                    "events": [],
                }
            )

        rounds[-1]["events"].append(event)

    return render_with_base_info(
        request,
        "logs.html",
        {"rounds": rounds},
    )


@login_required
@user_in_team_required
def logging_event_data(
    request: HttpRequest, uuid, team: Team, *, competition: Competition
):
    event = get_object_or_404(
        Logging.objects.select_related("logging_attempt"),
        logging_attempt__uuid=uuid,
        team=team,
    )

    return JsonResponse(event.logging_attempt.log_data)
