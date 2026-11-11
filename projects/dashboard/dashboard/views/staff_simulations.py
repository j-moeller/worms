"""Staff: Simulations -- how a gameplan plays out, before a round uses it (docs/08-user-guide.md).

    /staff/simulations/                     the page: the round player (templates/staff/simulations.html)
    /staff/simulations/gameplans/           the gameplans and the durations per team the rounds give
                                            them (the player's tabs)
    /staff/simulations/<file>/?duration=N   one simulation, as the round player's document

The gameplans are on the orchestrator and the node only, never here (docs/07-security.md). The
orchestrator simulates the attempt (orchestrator/rounds/simulations.py), keeps the document and
hands it out through its API; this view asks for it and passes it on. The document is a run's
kind of record: the sandbox and what happened when, no path, file content, log line or problem
of the gameplan.
"""

from __future__ import annotations

from core.models.competition import Round
from core.models.rounds import GameplanFile
from dashboard import orchestrator_client
from dashboard.views.decorators import staff_required
from dashboard.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache

from orchestrator.rounds.simulations import parse_duration

TIMEOUT = 28  # the orchestrator gives a simulation 20 s; gunicorn ends this worker at 30


def _gameplans(competition):
    """[{file, valid, checked_at, rounds: [{id, name, team_duration}]}]: every gameplan the
    orchestrator lists, and the rounds of the competition that use it."""
    rounds = {}
    if competition is not None:
        for r in Round.objects.filter(competition=competition).exclude(gameplan="").order_by("round_start", "id"):
            rounds.setdefault(r.gameplan, []).append({"id": str(r.uuid), "name": r.name, "team_duration": r.team_duration})
    return [
        {"file": g.filename, "valid": g.valid, "checked_at": g.checked_at.isoformat() if g.checked_at else None,
         "rounds": rounds.get(g.filename, [])}
        for g in GameplanFile.objects.order_by("filename")
    ]  # fmt: skip


@staff_required
def simulations(request: HttpRequest):
    return render_with_base_info(request, "staff/simulations.html", {"gameplans": _gameplans(request.competition)})


def _staff_json(request):
    if not request.user.is_staff:
        return JsonResponse({"error": "Forbidden"}, status=403)
    return None


@never_cache
@login_required
def simulation_gameplans(request: HttpRequest):
    refused = _staff_json(request)
    if refused:
        return refused
    return JsonResponse({"gameplans": _gameplans(request.competition)})


@never_cache
@login_required
def simulation_document(request: HttpRequest, filename):
    refused = _staff_json(request)
    if refused:
        return refused
    # Only a name the orchestrator listed goes back to it.
    if not GameplanFile.objects.filter(filename=filename).exists():
        return JsonResponse({"error": "no such gameplan"}, status=404)
    try:
        duration = parse_duration(request.GET.get("duration"))
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    try:
        doc = orchestrator_client.post(f"/gameplans/{filename}/simulation/?duration={duration}", timeout=TIMEOUT)
    except orchestrator_client.OrchestratorError as error:
        return JsonResponse({"error": f"The orchestrator could not simulate it: {error}"}, status=502)
    return JsonResponse(doc)
