"""Orchestrator REST API.

Callers (each with its own bearer token, settings.ORCHESTRATOR_TOKENS):

- dashboard: power control of *sandbox* VMs (VMs registered in the
  virtual_machines table for a round). Nothing else on Proxmox can be touched.

All endpoints are token-authenticated (no session, hence CSRF-exempt) and
return JSON. VMs are identified by their uuid only; database ids and Proxmox
VMIDs never cross this API (the orchestrator maps a VM uuid to its Proxmox VMID
itself).
"""

import hmac
from functools import wraps

from core.models.devbox import Devbox
from core.models.rounds import HostJob
from core.models.team import Team
from core.models.vm import VirtualMachine
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from proxmoxer import ProxmoxAPI


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
# Sandbox VM power control (dashboard)
# ---------------------------------------------------------------------------


def proxmox():
    config = settings.PROXMOX
    return ProxmoxAPI(
        config["host"],
        port=config["port"],
        user=config["user"],
        token_name=config["token_name"],
        token_value=config["token_value"],
        verify_ssl=config["verify_ssl"],
    )


def sandbox_vm(view):
    """Resolve a VM uuid to a registered sandbox VM; nothing else can be controlled."""

    @wraps(view)
    def wrapper(request: HttpRequest, vm_uuid):
        vm = VirtualMachine.objects.filter(uuid=vm_uuid, proxmox_vm_id__isnull=False).first()
        if vm is None:
            return JsonResponse({"error": "not a sandbox VM"}, status=404)
        try:
            return view(request, vm)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=502)

    return wrapper


def qemu(vm: VirtualMachine):
    return proxmox().nodes(settings.PROXMOX["node"]).qemu(vm.proxmox_vm_id)


@require_token("dashboard")
@require_POST
@sandbox_vm
def start(request: HttpRequest, vm: VirtualMachine):
    return JsonResponse({"vm": str(vm.uuid), "status": "starting", "task": qemu(vm).status.start.post()})


@require_token("dashboard")
@require_POST
@sandbox_vm
def stop(request: HttpRequest, vm: VirtualMachine):
    return JsonResponse({"vm": str(vm.uuid), "status": "stopping", "task": qemu(vm).status.stop.post()})


@require_token("dashboard")
@require_POST
@sandbox_vm
def reset(request: HttpRequest, vm: VirtualMachine):
    return JsonResponse({"vm": str(vm.uuid), "status": "resetting", "task": qemu(vm).status.reset.post()})


@require_token("dashboard")
@require_GET
@sandbox_vm
def status(request: HttpRequest, vm: VirtualMachine):
    return JsonResponse({"vm": str(vm.uuid), "status": qemu(vm).status.current.get()["status"]})


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
