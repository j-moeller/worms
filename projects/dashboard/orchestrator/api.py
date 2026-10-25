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

from orchestrator.rounds.devbox import provisioned_team_specs


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
        # Serialise resets of this team and never pile up rebuilds: if one is
        # already queued or running, report it instead of enqueuing another and
        # bumping the version again (docs/security-assessment.md, D2).
        Team.objects.select_for_update().get(pk=team.pk)
        existing = HostJob.objects.filter(
            kind=HostJob.Kind.DEVBOX_REBUILD,
            state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING],
            params__team_uuid=str(team.uuid),
        ).first()
        if existing is not None:
            return JsonResponse(
                {
                    "team": str(team.uuid),
                    "devbox_version": team.devbox_version,
                    "job": str(existing.uuid),
                    "rebuild_enqueued": False,
                    "detail": "a rebuild is already in progress",
                }
            )
        Team.objects.filter(pk=team.pk).update(devbox_version=F("devbox_version") + 1)
        team.refresh_from_db(fields=["devbox_version"])
        devbox = Devbox.objects.filter(team=team).first()
        job = None
        if devbox is not None:
            # The agent regenerates the whole devbox stack and re-pushes every
            # team's SSH keys, so the job carries the full teams list (with
            # keys), not just this team -- otherwise the rebuild fails
            # validation and the VMs and keys are never updated.
            job = HostJob.objects.create(
                kind=HostJob.Kind.DEVBOX_REBUILD,
                params={
                    "team_uuid": str(team.uuid),
                    "devbox_version": team.devbox_version,
                    "teams": provisioned_team_specs(team.competition),
                },
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
    rebuilding any VM (docs/devbox.md): enqueued when a team adds or removes a
    key, or leaves. `team_uuid` is the team that triggered it (for logging and
    de-duplication); the job itself carries the whole competition's teams,
    because the gateway's authorized_keys is rewritten wholesale.

    Does not bump the version or touch tokens. Coalesced: if a reconfigure is
    already queued or running, no second one is enqueued."""
    team = Team.objects.filter(uuid=team_uuid).first()
    if team is None:
        return JsonResponse({"error": "no such team"}, status=404)

    with transaction.atomic():
        Team.objects.select_for_update().get(pk=team.pk)
        pending = HostJob.objects.filter(
            kind=HostJob.Kind.DEVBOX_RECONFIGURE,
            state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING],
        ).first()
        if pending is not None:
            return JsonResponse({"job": str(pending.uuid), "reconfigure_enqueued": False, "detail": "a reconfigure is already queued"})
        teams = provisioned_team_specs(team.competition)
        if not teams:
            # No devbox provisioned yet: keys will be installed at provisioning.
            return JsonResponse({"job": None, "reconfigure_enqueued": False, "detail": "no devbox provisioned yet"})
        job = HostJob.objects.create(
            kind=HostJob.Kind.DEVBOX_RECONFIGURE,
            params={"team_uuid": str(team.uuid), "teams": teams},
        )
    return JsonResponse({"job": str(job.uuid), "reconfigure_enqueued": True})
