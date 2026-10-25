import json

from core.models.logging import Logging
from core.models.team import Team
from dashboard.views.decorators import user_in_team_required
from dashboard.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404

PAGE_SIZE = 50


def pretty(log_data) -> str:
    """The worm's log entry as indented JSON text. The template escapes it:
    it is attacker-controlled (written by the worm via the logging API)."""
    return json.dumps(log_data, indent=2, ensure_ascii=False)


@login_required
@user_in_team_required
def logs(request: HttpRequest, team: Team):
    """The team's log entries, newest first, a page at a time, each with its
    data inline (an expandable block: no JavaScript needed)."""
    events = (
        Logging.objects.filter(team=team, round__competition=request.active_competition)
        .select_related("round", "logging_attempt")
        .order_by("-created_at")
    )
    page = Paginator(events, PAGE_SIZE).get_page(request.GET.get("page"))

    rounds = []
    for event in page.object_list:
        if not rounds or rounds[-1]["round"].pk != event.round_id:
            rounds.append({"round": event.round, "events": []})
        rounds[-1]["events"].append({"event": event, "data": pretty(event.logging_attempt.log_data)})

    return render_with_base_info(request, "logs.html", {"rounds": rounds, "page": page})


@login_required
@user_in_team_required
def logging_event_data(request: HttpRequest, uuid, team: Team):
    """One log entry's data as raw JSON (linked from the logs page)."""
    event = get_object_or_404(
        Logging.objects.select_related("logging_attempt"),
        logging_attempt__uuid=uuid,
        team=team,
    )
    return JsonResponse(event.logging_attempt.log_data, safe=False)
