from django.urls import path
from orchestrator import api

# The orchestrator controls sandbox VM power via proxmoxer (and collects the
# topology for the dashboard, manage.py collect_topology). Callers identify VMs
# by uuid only.

urlpatterns = [
    # Dashboard (token scope "dashboard"): sandbox VMs only, by VM uuid.
    path("vms/<uuid:vm_uuid>/start/", api.start, name="start"),
    path("vms/<uuid:vm_uuid>/stop/", api.stop, name="stop"),
    path("vms/<uuid:vm_uuid>/status/", api.status, name="status"),
    path("vms/<uuid:vm_uuid>/reset/", api.reset, name="reset"),
]
