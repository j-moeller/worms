"""Round timeline for the animated network view (staff only).

The data model is assembled by orchestrator.rounds.timeline (a read-only
module usable from either Django app). These views only serve it as JSON; the
animation itself is front-end (a later task).
"""

from core.models.rounds import RoundRun
from dashboard.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.cache import never_cache

from orchestrator.rounds import timeline


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
                    "team": run.team.name,
                    "state": run.state,
                    "position": run.position,
                }
                for run in runs
            ]
        },
    )


@never_cache
@login_required
def round_timeline_data(request: HttpRequest, uuid):
    if not request.user.is_staff:
        return JsonResponse({"error": "Forbidden"}, status=403)
    run = get_object_or_404(
        RoundRun.objects.select_related("round", "team").prefetch_related(
            "virtual_machines", "events__vm"
        ),
        uuid=uuid,
    )
    return JsonResponse(timeline.build(run))
