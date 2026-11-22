"""Power control of sandbox VMs for a gameplan's execute() (start, stop, shutdown, suspend,
resume), through the Proxmox API with the orchestrator's token. Each action is seen done:
the VM's state afterwards, or PowerError.

Least privilege: the token needs only VM.PowerMgmt and VM.Audit on the pool
`sandbox` (ansible/roles/host/round_agent sets up that ACL), so it cannot
touch any other VM, create anything, or change configuration. On top of that,
only VMs registered for a run are accepted, and the VM's name in Proxmox must
match before anything is done: a VMID that has been reused since (sandboxes
are rebuilt for every run) is refused instead of switching off another VM.

Every API call is tried again on a transient error (the connection, HTTP 5xx) and then is a
PowerError, whatever proxmoxer raised: the conductor counts a PowerError as its own failure
(the attempt runs again), never the worm's. Proxmox's word on a VM's state is the one thing a
worm that is root in the VM cannot fake (QEMU's state, not the guest's). What a root guest can
do to that state is power off, or halt its QEMU (a KVM internal error): both are stopped here,
and an action that meets them is VMStopped, never a PowerError.
"""

from __future__ import annotations

import time

from django.conf import settings

# QEMU's run states (Proxmox's qmpstatus) that only a reset leaves: the guest's own doing (its
# poweroff, which QEMU ends at once under -no-shutdown; a KVM internal error; a panic). Such a VM
# runs no code and never will again: stopped, as far as anyone asks (`halted` inside this module).
HALTED = {"shutdown", "internal-error", "guest-panicked"}
# Up and able to run: S3 sleep (Proxmox disables it, machine versions >= 9.2+pve1) is still live,
# and QEMU (>= 9) pauses and resumes it like a running VM. Every other state is paused: by us
# (qm suspend), by QEMU on a disk I/O error, or by a task on the host (migrating, saving its RAM).
LIVE = {None, "running", "suspended"}


class PowerError(RuntimeError):
    pass


class VMStopped(PowerError):
    """An action that met a VM stopped (or halted by its guest): a suspend or resume of it, or a
    start it came out of stopped again. It was powered off meanwhile, from inside (the worm) or
    on the host: who did it is the host's to say (an observe job). ran: the VM was let run
    before it stopped (a start or resume went through), so its own guest may have stopped it."""

    def __init__(self, message, ran=False):
        super().__init__(message)
        self.ran = ran


def _transient(error) -> bool:
    """A dropped or refused connection (requests' errors are OSErrors), or the server's 5xx."""
    status = getattr(error, "status_code", None)
    return isinstance(error, OSError) or (isinstance(status, int) and status >= 500)


class ProxmoxPower:
    # How long a VM may take to reach the state an action asks for (seconds), beyond a
    # shutdown's own timeout.
    SETTLE = 60
    # An API call: tried this often, this many seconds apart, while the error is transient.
    TRIES, RETRY_DELAY = 3, 2

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

    def _call(self, what, request):
        """One API call, tried again on a transient error; then, as any other error at once, a
        PowerError that says what was asked."""
        for attempt in range(1, self.TRIES + 1):
            try:
                return request()
            except Exception as error:  # noqa: BLE001  (proxmoxer's, requests', the JSON's)
                if attempt < self.TRIES and _transient(error):
                    self._sleep(self.RETRY_DELAY)
                    continue
                tried = f" (tried {attempt} times)" if attempt > 1 else ""
                raise PowerError(f"{what}: {type(error).__name__}: {str(error)[:300]}{tried}") from None

    def _current(self, qemu, name):
        return self._call(f"the state of {name}", qemu.status.current.get)

    def _lookup(self, vmid, name):
        qemu = self.api().nodes(settings.PROXMOX["node"]).qemu(vmid)
        current = self._current(qemu, name)
        if current.get("name") != name:
            raise PowerError(f"VMID {vmid} is {current.get('name')!r}, not {name!r}: refusing")
        return qemu, current

    def _qemu(self, vm):
        if vm.run_id is None or vm.proxmox_vm_id is None:
            raise PowerError(f"{vm.vm_identifier} is not a registered sandbox VM")
        return self._lookup(vm.proxmox_vm_id, vm.vm_identifier)

    @staticmethod
    def _state(current):
        """running, paused (suspended in memory, or held by QEMU or the host), stopped, or halted
        (QEMU up, its guest halted for good: HALTED)."""
        if current.get("status") != "running":
            return "stopped"
        qmp = current.get("qmpstatus")
        if qmp in LIVE:
            return "running"
        return "halted" if qmp in HALTED else "paused"

    def _post(self, vm, qemu, action, want, **params):
        """Ask for `action`: its answer (a task's UPID), or None when the request failed and was
        done all the same (its answer lost, and tried again it finds the VM busy or there
        already): the VM's state decides."""
        try:
            return self._call(f"{action} {vm.vm_identifier}", lambda: getattr(qemu.status, action).post(**params))
        except PowerError:
            state = self._state(self._current(qemu, vm.vm_identifier))
            if state == want:
                return None
            if state in ("stopped", "halted") and action in ("suspend", "resume"):
                raise VMStopped(f"{vm.vm_identifier} is stopped: it cannot {action}", ran=action == "resume") from None
            raise

    def _task_ended(self, vm, upid):
        """Whether the task (a start) has ended; PowerError if it failed (it did not start it)."""
        task = self._call(f"the start task of {vm.vm_identifier}",
                          self.api().nodes(settings.PROXMOX["node"]).tasks(upid).status.get)  # fmt: skip
        if task.get("status") != "stopped":
            return False
        if task.get("exitstatus") != "OK":
            raise PowerError(f"start {vm.vm_identifier}: the task failed: {str(task.get('exitstatus'))[:300]}")
        return True

    def _settle(self, vm, qemu, want, seconds, stopped_fails=False, started_by=None):
        """Until the VM is `want`. stopped_fails (a suspend or resume): a VM that is stopped
        never gets there, VMStopped at once. started_by (a start's task): once that task has
        ended the VM ran, and found stopped it has stopped again (VMStopped, ran), which a guest
        can do faster than this looks (a worm on its disk powering off as it boots)."""
        deadline = self._monotonic() + seconds
        while True:
            state = self._state(self._current(qemu, vm.vm_identifier))
            if state == want:
                return
            if state in ("stopped", "halted") and stopped_fails:
                raise VMStopped(f"{vm.vm_identifier} is stopped, not {want}", ran=want == "running")
            if state in ("stopped", "halted") and started_by is not None and self._task_ended(vm, started_by):
                # Looked at again: before the task's end it may not have run yet.
                if self._state(self._current(qemu, vm.vm_identifier)) in ("stopped", "halted"):
                    raise VMStopped(f"{vm.vm_identifier} was started and is stopped again", ran=True)
                continue
            if self._monotonic() >= deadline:
                raise PowerError(f"{vm.vm_identifier} is {state}, not {want}, {seconds} s after the request")
            self._sleep(1)

    def stop(self, vm, wait=False):
        """Hard stop (the worm cannot delay a shutdown). A halted VM's QEMU is ended too."""
        qemu, current = self._qemu(vm)
        if current.get("status") != "stopped":
            self._post(vm, qemu, "stop", "stopped")
        if wait:
            self._settle(vm, qemu, "stopped", self.SETTLE)

    def start(self, vm, wait=False):
        """A halted VM is stopped first: only a reset leaves its state, and a start of a VM whose
        QEMU is up does nothing. VMStopped (ran) if it stops again before it is seen running."""
        qemu, current = self._qemu(vm)
        state = self._state(current)
        if state == "halted":
            self._post(vm, qemu, "stop", "stopped")
            self._settle(vm, qemu, "stopped", self.SETTLE)
            state = "stopped"
        task = self._post(vm, qemu, "start", "running") if state == "stopped" else None
        if wait:
            self._settle(vm, qemu, "running", self.SETTLE, started_by=task if isinstance(task, str) else None)

    def shutdown(self, vm, timeout=60):
        """Ask the guest to shut down; Proxmox powers it off after `timeout` seconds if it does
        not (a worm that is root on it can ignore the request). A halted VM has no guest to ask:
        stopped at once."""
        qemu, current = self._qemu(vm)
        state = self._state(current)
        if state == "halted":
            self._post(vm, qemu, "stop", "stopped")
        elif state != "stopped":
            self._post(vm, qemu, "shutdown", "stopped", timeout=int(timeout), forceStop=1)
        self._settle(vm, qemu, "stopped", int(timeout) + self.SETTLE)

    def suspend(self, vm):
        """Pause the VM in memory (qm suspend, not to disk). VMStopped if it is stopped."""
        self._paused_or_running(vm, "suspend", "paused")

    def resume(self, vm):
        """VMStopped if it is stopped."""
        self._paused_or_running(vm, "resume", "running")

    def _paused_or_running(self, vm, action, want):
        qemu, current = self._qemu(vm)
        state = self._state(current)
        if state in ("stopped", "halted"):
            # Before this request let it run (ran False): whether its guest could have stopped it
            # is what the VM was before, which the caller knows.
            raise VMStopped(f"{vm.vm_identifier} is stopped: it cannot {action}")
        if (action == "suspend" and state != "paused") or (action == "resume" and state == "paused"):
            self._post(vm, qemu, action, want)
        self._settle(vm, qemu, want, self.SETTLE, stopped_fails=True)

    @classmethod
    def _seen(cls, current):
        """running, paused or stopped: a halted VM is stopped to everyone outside this module."""
        state = cls._state(current)
        return "stopped" if state == "halted" else state

    def status(self, vm):
        return self._seen(self._qemu(vm)[1])

    def status_of(self, vmid, name):
        """running, paused or stopped: a VM of the pool by its VMID, its name checked. Read
        only, for the sandbox's gateway, which no run registers."""
        return self._seen(self._lookup(vmid, name)[1])
