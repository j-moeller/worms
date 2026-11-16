"""The round page (/round/): the round the header's timer counts down to, the current one or else
the next (docs/08-user-guide.md). Its window, the duration per team, and the description its
gameplan's author wrote for teams (Gameplan.description).

The description is the one of the version the round runs (core/models/rounds.py,
round_gameplan): the pinned one once its first attempt starts, else its file's current one, as
the orchestrator recorded it (gameplan_versions). The dashboard never reads a gameplan's code
(docs/07-security.md). A gameplan without one, or a round without a gameplan yet, shows the
round's facts only.
"""

from __future__ import annotations

from core.models.rounds import round_gameplan
from core.models.worms import Upload
from dashboard.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest


def duration_text(seconds: int) -> str:
    """3600 -> "1 h", 5400 -> "1 h 30 min", 90 -> "1 min 30 s"."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{hours} h"] if hours else []
    if minutes:
        parts.append(f"{minutes} min")
    if secs or not parts:
        parts.append(f"{secs} s")
    return " ".join(parts)


@login_required
def round_info(request: HttpRequest):
    state = request.competition_state
    rnd = state.current_round or state.next_round
    context = {"round": rnd, "running": state.current_round is not None}
    if rnd is not None:
        context["description"] = round_gameplan(rnd)["description"]
        context["duration"] = duration_text(rnd.team_duration)
        if state.next_round is not None and request.team is not None:
            # What takes part (orchestrator/rounds/planner.py): the team's latest Live upload
            # from before the start, however old.
            upload = Upload.objects.filter(team=request.team, devbox=False).order_by("-created_at", "-id").first()
            context["upload"] = upload
            context["upload_before_last_round"] = bool(
                upload and state.prev_round and state.prev_round.round_start and upload.created_at < state.prev_round.round_start
            )
    return render_with_base_info(request, "round_info.html", context)
