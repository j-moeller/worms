from datetime import timedelta

from core.models.topology import TopologySnapshot
from dashboard.topology import analyze
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


def analyzed(snapshot: TopologySnapshot) -> dict:
    key = (snapshot.pk, snapshot.collected_at)
    if key not in _cache:
        _cache.clear()
        _cache[key] = analyze(snapshot.data)
    return _cache[key]


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

    snapshot = TopologySnapshot.objects.filter(source="proxmox").first()
    if snapshot is None or snapshot.collected_at is None:
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
            **analyzed(snapshot),
            "collected_at": snapshot.collected_at.isoformat(),
            "attempted_at": snapshot.attempted_at.isoformat(),
            "collection_error": snapshot.error,
            "stale": timezone.now() - snapshot.collected_at > STALE_AFTER,
        }
    )
