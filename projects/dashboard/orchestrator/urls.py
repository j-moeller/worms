from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.urls import path
from django.views.decorators.http import require_POST
from proxmoxer import ProxmoxAPI

# Proxmox connection details come from settings.PROXMOX, which is populated from
# the environment (systemd EnvironmentFile rendered from the encrypted vault).
# No credentials are hardcoded here.
PROXMOX_NODE = settings.PROXMOX["node"]

# IDEA:
# - The rounds in the database are pre-scheduled, i.e., they exist and have
# a fixed date. What we do not have is:
#   - The generated sandbox VM network (using a Python script)
#   - The sandbox VMs created, initialized, deployed (using Tofu + Ansible)
#   - The firewall (using Tofu)
#   - The database entries (using the orchestrator)
#
# - The orchestrator receives the output of a Tofu sandbox deployment and
#   generates database entries from that.
# - The orchestrator deploys the uploaded worms to the VMs (rsync)
# - The orchestrator schedules the start of the worms (SSH + "at")


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


@require_POST
def start(request: HttpRequest, id: int):
    try:
        task = proxmox().nodes(PROXMOX_NODE).qemu(id).status.start.post()
        return JsonResponse({"status": "starting", "task": task})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_POST
def stop(request: HttpRequest, id: int):
    try:
        task = proxmox().nodes(PROXMOX_NODE).qemu(id).status.stop.post()
        return JsonResponse({"status": "stopping", "task": task})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_POST
def status(request: HttpRequest, id: int):
    try:
        vm_status = proxmox().nodes(PROXMOX_NODE).qemu(id).status.current.get()

        return JsonResponse(
            {
                "id": id,
                "status": vm_status["status"],
            }
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_POST
def reset(request: HttpRequest, id: int):
    try:
        task = proxmox().nodes(PROXMOX_NODE).qemu(id).status.reset.post()
        return JsonResponse(
            {
                "status": "resetting",
                "task": task,
            }
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


urlpatterns = [
    path("start/<int:id>/", start, name="start"),
    path("stop/<int:id>/", stop, name="stop"),
    path("status/<int:id>/", status, name="status"),
    path("reset/<int:id>/", reset, name="reset"),
]
