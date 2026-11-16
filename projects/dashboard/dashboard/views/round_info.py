"""The round page (/round/): the round the header's timer counts down to, the current one or else
the next (docs/08-user-guide.md). Its window, the duration per team, and the description its
gameplan's author wrote for teams (Gameplan.description).

The description comes from the `gameplans` table, where the orchestrator puts it from its copy
of the file: the dashboard never reads a gameplan (docs/07-security.md). A gameplan without one,
or a round without a gameplan yet, shows the round's facts only.
"""

from __future__ import annotations

from core.models.rounds import GameplanFile
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
        context["description"] = GameplanFile.description_of(rnd.gameplan)
        context["duration"] = duration_text(rnd.team_duration)
    return render_with_base_info(request, "round_info.html", context)
