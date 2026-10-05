from django.http import HttpRequest, JsonResponse
from django.urls import path
from django.views.decorators.http import require_POST
from proxmoxer import ProxmoxAPI

PROXMOX_HOST = "localhost"  # TODO
USERNAME = "orchestrator@pam"  # TODO
TOKEN_NAME = "orchestrator-prov"  # TODO
TOKEN_VALUE = "de2d1953-33a2-49e3-924a-9d040a7e6f1e"  # TODO # PASSWORD
PROXMOX_NODE = "worms-pve"

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
    return ProxmoxAPI(
        PROXMOX_HOST,
        user=USERNAME,
        token_name=TOKEN_NAME,
        token_value=TOKEN_VALUE,
        verify_ssl=False,
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
