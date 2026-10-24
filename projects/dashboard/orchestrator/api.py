"""Orchestrator REST API.

One caller, the dashboard (its own bearer token, settings.ORCHESTRATOR_TOKENS
"dashboard"): it pushes a team's devbox reset. Token-authenticated (no
session, hence CSRF-exempt), JSON responses. Nothing here touches Proxmox;
the round agent rebuilds the devbox from the host job.
"""

import hmac
from functools import wraps

from core.models.devbox import Devbox
from core.models.rounds import HostJob
from core.models.team import Team
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST


def require_token(scope):
    """Allow the request only with the bearer token configured for `scope`."""

    def decorator(view):
        @csrf_exempt
        @wraps(view)
        def wrapper(request: HttpRequest, *args, **kwargs):
            expected = settings.ORCHESTRATOR_TOKENS.get(scope, "")
            header = request.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else ""
            if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
                return JsonResponse({"error": "unauthorized"}, status=401)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Devbox reset (dashboard) -- docs/devbox.md
# ---------------------------------------------------------------------------


@require_token("dashboard")
@require_POST
def devbox_reset(request: HttpRequest, team_uuid):
    """Reset a team's devbox: bump its version (which immediately clears the
    dashboard's dev score and logs, without deleting any records) and enqueue a
    rebuild host job for the round agent (it rebuilds the VMs and rewrites their
    tokens). Idempotent-ish: each call is one more rebuild at a new version.

    This is the only devbox mutation the dashboard can trigger; it cannot touch
    any other team's devbox or any infrastructure VM.
    """
    team = Team.objects.filter(uuid=team_uuid).first()
    if team is None:
        return JsonResponse({"error": "no such team"}, status=404)

    with transaction.atomic():
        Team.objects.filter(pk=team.pk).update(devbox_version=F("devbox_version") + 1)
        team.refresh_from_db(fields=["devbox_version"])
        devbox = Devbox.objects.filter(team=team).first()
        job = None
        if devbox is not None:
            job = HostJob.objects.create(
                kind=HostJob.Kind.DEVBOX_REBUILD,
                params={"team_uuid": str(team.uuid), "devbox_version": team.devbox_version},
            )
    return JsonResponse(
        {
            "team": str(team.uuid),
            "devbox_version": team.devbox_version,
            "job": str(job.uuid) if job else None,
            "rebuild_enqueued": job is not None,
        }
    )
