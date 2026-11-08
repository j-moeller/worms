"""Orchestrator REST API.

One caller, the dashboard (its own bearer token, settings.ORCHESTRATOR_TOKENS
"dashboard"): it pushes a team's devbox reset. Token-authenticated (no
session, hence CSRF-exempt), JSON responses. Nothing here touches Proxmox;
the round agent rebuilds the devbox from the host job.
"""

import hmac
from functools import wraps

from core.locking import serialized_resource
from core.models.rounds import HostJob
from core.models.team import Team
from django.conf import settings
from django.db.models import Max
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from orchestrator.rounds.devbox import provisioned_team_specs, request_rebuild


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
# Devbox reset (dashboard) -- docs/04-dev-environment.md
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

    job, enqueued = request_rebuild(team)
    if job is not None and not enqueued:
        return JsonResponse(
            {
                "team": str(team.uuid),
                "devbox_version": team.devbox_version,
                "job": str(job.uuid),
                "rebuild_enqueued": False,
                "detail": "a rebuild is already in progress",
            }
        )
    return JsonResponse(
        {
            "team": str(team.uuid),
            "devbox_version": team.devbox_version,
            "job": str(job.uuid) if job else None,
            "rebuild_enqueued": job is not None,
        }
    )


@require_token("dashboard")
@require_POST
def devbox_reconfigure(request: HttpRequest, team_uuid):
    """Re-push every team's SSH keys to the devbox VMs and the gateway without
    rebuilding any VM (docs/04-dev-environment.md): enqueued when a team adds or removes a
    key, or leaves. `team_uuid` is the team that triggered it (for logging and
    de-duplication); the job itself carries the whole competition's teams,
    because the gateway's authorized_keys is rewritten wholesale.

    Does not bump the version or touch tokens. Coalesces only when no later
    rebuild/provision would overwrite that job's key snapshot."""
    team = Team.objects.filter(uuid=team_uuid).first()
    if team is None:
        return JsonResponse({"error": "no such team"}, status=404)

    # The gateway is rewritten for the entire competition. Serialize callers
    # from different teams as well as the agent's queued->running transition.
    with serialized_resource("devbox-jobs", "queue"):
        teams = provisioned_team_specs(team.competition)
        if not teams:
            return JsonResponse({"job": None, "reconfigure_enqueued": False, "detail": "no devbox provisioned yet"})
        competition_teams = list(Team.objects.filter(competition=team.competition).values_list("uuid", flat=True))
        # Updating an older job in place must not put the latest keys before
        # a queued full-stack snapshot that would reinstall the removed key.
        barrier = HostJob.objects.filter(
            kind__in=[HostJob.Kind.DEVBOX_REBUILD, HostJob.Kind.DEVBOX_PROVISION],
            state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING],
        ).aggregate(latest=Max("id"))["latest"] or 0
        queued = HostJob.objects.filter(
            id__gt=barrier,
            kind=HostJob.Kind.DEVBOX_RECONFIGURE,
            state=HostJob.State.QUEUED,
            params__team_uuid__in=[str(uuid) for uuid in competition_teams],
        ).order_by("id")
        # Preserve main's immutable snapshots; the agent may skip superseded
        # queued jobs. Only an identical snapshot after the barrier can cover
        # this request without a new job.
        pending = next((job for job in queued if job.params.get("teams") == teams), None)
        if pending is not None:
            return JsonResponse({"job": str(pending.uuid), "reconfigure_enqueued": False,
                                 "detail": "the current keys are already queued"})
        running = HostJob.objects.filter(
            id__gt=barrier,
            kind=HostJob.Kind.DEVBOX_RECONFIGURE, state=HostJob.State.RUNNING,
            params__team_uuid__in=[str(uuid) for uuid in competition_teams],
        ).order_by("-id").first()
        if running is not None and running.params.get("teams") == teams:
            return JsonResponse({"job": str(running.uuid), "reconfigure_enqueued": False,
                                 "detail": "the current keys are already being applied"})
        job = HostJob.objects.create(
            kind=HostJob.Kind.DEVBOX_RECONFIGURE,
            params={"team_uuid": str(team.uuid), "teams": teams},
        )
    return JsonResponse({"job": str(job.uuid), "reconfigure_enqueued": True})
