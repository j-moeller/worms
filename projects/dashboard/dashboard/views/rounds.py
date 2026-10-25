"""Round runs (staff only): the list. One run's progression (replayed by the
TOPOLOGY player) is served by views/progression.py.
"""

from core.models.rounds import RoundRun
from dashboard.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest
from django.shortcuts import redirect


@login_required
def rounds(request: HttpRequest):
    if not request.user.is_staff:
        return redirect("index")
    runs = (
        RoundRun.objects.select_related("round", "team")
        .order_by("-round__round_start", "position")
    )
    return render_with_base_info(
        request,
        "rounds.html",
        {
            "runs": [
                {
                    "uuid": str(run.uuid),
                    "round": run.round.name,
                    "round_id": run.round_id,
                    "team": run.team.name,
                    "state": run.state,
                    "position": run.position,
                }
                for run in runs
            ]
        },
    )

