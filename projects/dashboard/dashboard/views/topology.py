"""The TOPOLOGY view (staff only): the infrastructure and the round player.

    /topology/                  the page (templates/topology.html)
    /topology/data/             the live infrastructure model (dashboard/infra_view.py)
    /topology/runs/             rounds of the current competition and their runs
                                (the round / team tabs)
    /rounds/<uuid>/timeline/    one run's timeline (orchestrator/rounds/timeline.py,
                                served by views/rounds.py)

The dashboard never contacts Proxmox: the orchestrator stores a read-only
snapshot (`manage.py collect_topology`) and this reads it from the database.
Addresses and firewall rules are sensitive, hence staff only.
"""

import re
from datetime import timedelta

from core.models.competition import ApplicationSetting
from core.models.rounds import RoundRun
from core.models.team import Team
from core.models.topology import TopologySnapshot
from dashboard.infra_view import address_index, analyze
from dashboard.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache

# The orchestrator refreshes the snapshot every few minutes (systemd timer);
# anything older is flagged in the UI.
STALE_AFTER = timedelta(minutes=15)

# Analysing a snapshot is deterministic, so keep the last result per worker.
_cache = {}


def latest_snapshot():
    snapshot = TopologySnapshot.objects.filter(source="proxmox").first()
    if snapshot is None or snapshot.collected_at is None:
        return snapshot, None
    key = (snapshot.pk, snapshot.collected_at)
    if key not in _cache:
        _cache.clear()
        _cache[key] = analyze(snapshot.data)
    return snapshot, _cache[key]


TEAM_SLUG = re.compile(r"^t(\d+)$")  # orchestrator provision_devboxes.team_slug


def with_team_names(model: dict) -> dict:
    """The devbox groups labelled with their team's name (VM names carry only
    the slug `t<team id>`). Returns a copy; the cached model is shared."""
    ids = {}
    for group in model["groups"]:
        match = TEAM_SLUG.match(group.get("team") or "")
        if match:
            ids[group["team"]] = int(match.group(1))
    names = dict(Team.objects.filter(id__in=ids.values()).values_list("id", "name")) if ids else {}
    groups = []
    for group in model["groups"]:
        name = names.get(ids.get(group.get("team")))
        groups.append({**group, "team_name": name, "label": f"Devbox {name}" if name else group["label"]})
    return {**model, "groups": groups}


def live_address_index() -> dict:
    """Address -> infrastructure node of the latest snapshot (for timelines)."""
    _, model = latest_snapshot()
    return address_index(model) if model else {}


@login_required
def topology(request: HttpRequest):
    # Firewall rules and addressing are sensitive: staff only.
    if not request.user.is_staff:
        return redirect("index")

    return render_with_base_info(request, "topology.html")


@never_cache
@login_required
def topology_data(request: HttpRequest):
    if not request.user.is_staff:
        return JsonResponse({"error": "Forbidden"}, status=403)

    snapshot, model = latest_snapshot()
    if model is None:
        return JsonResponse(
            {
                "error": "No topology collected yet. Run `manage.py collect_topology` "
                "on the orchestrator (or check its collect-topology timer).",
                "collection_error": snapshot.error if snapshot else "",
            },
            status=503,
        )

    return JsonResponse(
        {
            **with_team_names(model),
            "collected_at": snapshot.collected_at.isoformat(),
            "attempted_at": snapshot.attempted_at.isoformat() if snapshot.attempted_at else None,
            "collection_error": snapshot.error,
            "stale": timezone.now() - snapshot.collected_at > STALE_AFTER,
        }
    )


@never_cache
@login_required
def topology_runs(request: HttpRequest):
    """The round tabs (rounds of the current competition, newest first) and,
    per round, the team tabs (its runs, in evaluation order)."""
    if not request.user.is_staff:
        return JsonResponse({"error": "Forbidden"}, status=403)

    competition = ApplicationSetting.get_current_competition()
    runs = RoundRun.objects.select_related("round", "team").order_by("-round__round_start", "round_id", "position")
    if competition is not None:
        runs = runs.filter(round__competition=competition)

    rounds = {}
    for run in runs:
        entry = rounds.setdefault(
            run.round_id,
            {
                "id": run.round_id,
                "name": run.round.name,
                "round_start": run.round.round_start.isoformat() if run.round.round_start else None,
                "round_end": run.round.round_end.isoformat() if run.round.round_end else None,
                "runs": [],
            },
        )
        entry["runs"].append(
            {
                "uuid": str(run.uuid),
                "team": run.team.name,
                "state": run.state,
                "position": run.position,
                "worm_start_at": run.worm_start_at.isoformat() if run.worm_start_at else None,
            }
        )
    return JsonResponse({"competition": competition.name if competition else None, "rounds": list(rounds.values())})
