"""Power control of sandbox VMs for a gameplan's execute() (start, stop, shutdown, suspend,
resume), through the Proxmox API with the orchestrator's token. Each action is seen done:
the VM's state afterwards, or PowerError.

Least privilege: the token needs only VM.PowerMgmt and VM.Audit on the pool
`sandbox` (ansible/roles/host/round_agent sets up that ACL), so it cannot
touch any other VM, create anything, or change configuration. On top of that,
only VMs registered for a run are accepted, and the VM's name in Proxmox must
match before anything is done: a VMID that has been reused since (sandboxes
are rebuilt for every run) is refused instead of switching off another VM.
"""

from __future__ import annotations

import time

from django.conf import settings


class PowerError(RuntimeError):
    pass


class ProxmoxPower:
    # How long a VM may take to reach the state an action asks for (seconds), beyond a
    # shutdown's own timeout.
    SETTLE = 60

    def __init__(self, api=None, sleep=time.sleep, monotonic=time.monotonic):
        self._api = api
        self._sleep, self._monotonic = sleep, monotonic

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

    @staticmethod
    def _state(current):
        """running, paused (suspended in memory) or stopped."""
        status = current.get("status")
        if status == "running" and current.get("qmpstatus") == "paused":
            return "paused"
        return status

    def _settle(self, vm, qemu, want, seconds):
        deadline = self._monotonic() + seconds
        while True:
            state = self._state(qemu.status.current.get())
            if state == want:
                return
            if self._monotonic() >= deadline:
                raise PowerError(f"{vm.vm_identifier} is {state}, not {want}, {seconds} s after the request")
            self._sleep(1)

    def stop(self, vm, wait=False):
        """Hard stop (the worm cannot delay a shutdown)."""
        qemu, status = self._qemu(vm)
        if status != "stopped":
            qemu.status.stop.post()
        if wait:
            self._settle(vm, qemu, "stopped", self.SETTLE)

    def start(self, vm, wait=False):
        qemu, status = self._qemu(vm)
        if status != "running":
            qemu.status.start.post()
        if wait:
            self._settle(vm, qemu, "running", self.SETTLE)

    def shutdown(self, vm, timeout=60):
        """Ask the guest to shut down; Proxmox powers it off after `timeout` seconds if it does
        not (a worm that is root on it can ignore the request)."""
        qemu, status = self._qemu(vm)
        if status != "stopped":
            qemu.status.shutdown.post(timeout=int(timeout), forceStop=1)
        self._settle(vm, qemu, "stopped", int(timeout) + self.SETTLE)

    def suspend(self, vm):
        """Pause the VM in memory (qm suspend, not to disk)."""
        qemu, _ = self._qemu(vm)
        if self._state(qemu.status.current.get()) != "paused":
            qemu.status.suspend.post()
        self._settle(vm, qemu, "paused", self.SETTLE)

    def resume(self, vm):
        qemu, _ = self._qemu(vm)
        if self._state(qemu.status.current.get()) == "paused":
            qemu.status.resume.post()
        self._settle(vm, qemu, "running", self.SETTLE)

    def status(self, vm):
        qemu, _ = self._qemu(vm)
        return self._state(qemu.status.current.get())
