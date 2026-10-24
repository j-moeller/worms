"""Round runs (staff only): the list, and one run's timeline as JSON.

The timeline is assembled by orchestrator.rounds.timeline (a read-only module
usable from either Django app) and replayed by the round player on the
TOPOLOGY page (templates/topology.html).
"""

from core.models.rounds import RoundRun
from dashboard.views.topology import live_address_index
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
                    "round_id": run.round_id,
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
    # The far end of the sandbox's flows (scoring, logging, DNS, the private
    # internet) is resolved against the latest Proxmox snapshot.
    return JsonResponse(timeline.build(run, infra=live_address_index()))
