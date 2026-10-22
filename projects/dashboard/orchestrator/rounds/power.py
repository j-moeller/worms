"""Power control of sandbox VMs for the game plan (stop/start), through the
Proxmox API with the orchestrator's token.

Least privilege: the token needs only VM.PowerMgmt and VM.Audit on the pool
`sandbox` (ansible/roles/host/round_agent sets up that ACL), so it cannot
touch any other VM, create anything, or change configuration. On top of that,
only VMs registered for a run are accepted, and the VM's name in Proxmox must
match before anything is done: a VMID that has been reused since (sandboxes
are rebuilt for every run) is refused instead of switching off another VM.
"""

from __future__ import annotations

from django.conf import settings


class PowerError(RuntimeError):
    pass


class ProxmoxPower:
    def __init__(self, api=None):
        self._api = api

    def api(self):
        if self._api is None:
            from proxmoxer import ProxmoxAPI

            config = settings.PROXMOX
            self._api = ProxmoxAPI(
                config["host"],
                port=config["port"],
                user=config["user"],
                token_name=config["token_name"],
                token_value=config["token_value"],
                verify_ssl=config["verify_ssl"],
                timeout=10,
            )
        return self._api

    def _qemu(self, vm):
        if vm.run_id is None or vm.proxmox_vm_id is None:
            raise PowerError(f"{vm.vm_identifier} is not a registered sandbox VM")
        qemu = self.api().nodes(settings.PROXMOX["node"]).qemu(vm.proxmox_vm_id)
        current = qemu.status.current.get()
        if current.get("name") != vm.vm_identifier:
            raise PowerError(
                f"VMID {vm.proxmox_vm_id} is {current.get('name')!r}, not {vm.vm_identifier!r}: refusing"
            )
        return qemu, current.get("status")

    def stop(self, vm):
        """Hard stop (the worm cannot delay a shutdown)."""
        qemu, status = self._qemu(vm)
        if status != "stopped":
            qemu.status.stop.post()

    def start(self, vm):
        qemu, status = self._qemu(vm)
        if status != "running":
            qemu.status.start.post()

    def status(self, vm):
        return self._qemu(vm)[1]
