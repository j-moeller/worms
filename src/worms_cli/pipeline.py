"""The build/operate pipeline: every step the top-level Makefile used to run,
now driven from the CLI. Each step runs the real tools (tofu, ansible-playbook,
the generator scripts, qm) through a Runner: on this machine (your laptop, or
the node), and the node's own part (sandbox and devbox stacks) on the node
(worms_cli/session.py).

A component is a list of named steps (Step). The first one that fails ends the
run: nothing after it runs.

Two conditions are entered before the first step that needs them and left after
the last one, and left at once when a step fails:

  DEVBOXES_OFF  no team VM runs: refused while a sandbox exists; the devbox VMs
                that run are shut down, and started again when it is left.
  INTERNET      the internet window (outbound internet for cloud-init, packages,
                the caches). ALWAYS closed again: on success, failure or
                interrupt; a failed close is loud.

The service VMs (ephemeral, private internet) have a snapshot, `clean`, that a
reset between rounds and every new round's sandbox build return them to. A
build or provision first rolls them back to it, so it configures clean VMs, and
then takes it again, so the snapshot holds what it changed.

OpenTofu gets its Proxmox credentials from the vault, through the Runner
(TF_VAR_* in the tofu process's environment only); see worms_cli/secrets.py.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from typing import Callable

from worms_cli import output
from worms_cli.build import BuildError, Runner

INTERNET_FLAG = "/run/worms/internet-allowed"

# The VMs a reset returns to their snapshot: the services a worm can talk to. The
# persistent VMs hold the records, and the team VMs are the teams' own.
SERVICE_POOLS = ("ephemeral", "private_internet")
TEAM_POOLS = ("sandbox", "devbox")
SERVICE_SNAPSHOT = "clean"

# The conditions a step can need, in the order they are entered.
DEVBOXES_OFF = "devbox VMs off"
INTERNET = "internet window"
SCOPES = (DEVBOXES_OFF, INTERNET)

# Stacks that get `tofu init` (order matters only for readability).
INIT_STACKS = [
    "platform",
    "firewall",
    "infrastructure/persistent",
    "infrastructure/ephemeral",
    "sandbox",
    "devbox",
    "private_internet",
]

# The configure playbook sequence.
CONFIGURE_PLAYBOOKS = [
    "WaitForVMs.yml",
    "SetupDNS.yml",
    "UpdateVMs.yml",
    "HardenSSH.yml",
    "CreateCA.yml",
    "DistributeCA.yml",
    "ConfigureNginx.yml",
    "ConfigureNginxDjango.yml",
    "SetupDbAndDashboard.yml",
    "SetupSandboxApi.yml",
    "SetupNetworkMonitor.yml",
    "SetupPrivateInternet.yml",
    "SetupAudit.yml",
    "SetupRoundAgent.yml",
]


def _stopping(cleanup: str) -> None:
    """Said before the cleanup that runs after a failed step, so that its commands do not
    read as the build going on. The step's own error ends the build: `build stopped: ...`."""
    output.error(f"a step failed, stopping the build: {cleanup} first")


@dataclass(frozen=True)
class Step:
    name: str
    run: Callable[[], object]
    scope: tuple = ()  # of SCOPES: entered before the step, left after the last that needs it
    on_failure: Callable[[], None] | None = None  # cleanup of this step's own failure


class Pipeline:
    def __init__(self, config, ansible_args: str = ""):
        self.runner = Runner(config)
        # Extra args appended to every ansible-playbook call.
        self.ansible_args = ansible_args
        # Ansible caches facts for the run (ansible.cfg); start without any,
        # and drop them again whenever tofu may have rebuilt VMs.
        self._facts_fresh = False
        # What later steps need from earlier ones.
        self.data = {
            "services_before": None,  # service vmids before the rollback
            "services_reset": None,  # the ones it rolled back to their snapshot
            "devboxes_off": [],  # devbox vmids this run shut down, to start again
            # Whether the service VMs lost what is newer than their snapshot: the
            # devbox map on scoring and logging (src/manage.py writes it again).
            "services_rolled_back": False,
        }

    @property
    def services_rolled_back(self) -> bool:
        return self.data["services_rolled_back"]

    @services_rolled_back.setter
    def services_rolled_back(self, value: bool) -> None:
        self.data["services_rolled_back"] = value

    def _ansible(self, playbook: str, *args: str, vault: bool = True, user_args: bool = True):
        if not self._facts_fresh:
            self.runner.sh("rm -rf .facts", cwd="ansible", quiet=True)
            self._facts_fresh = True
        extra = list(args)
        if self.ansible_args and user_args:
            extra.append(self.ansible_args)
        self.runner.ansible(playbook, *extra, vault=vault)

    # -- running steps -----------------------------------------------------------

    def plan(self, method_name: str) -> list[Step]:
        """The steps of a step (STEPS) or a whole build: its own plan, or the method as one step."""
        builder = getattr(self, f"_plan_{method_name}", None)
        return builder() if builder else [Step(method_name.replace("_", " "), getattr(self, method_name))]

    def run_step(self, step: str) -> None:
        """`worms step <step>`."""
        self._execute(self.plan(STEPS[step][0]))

    def _execute(self, steps: list[Step]) -> None:
        """Run the steps in order, each in the conditions it needs; on a failure, leave
        those at once and raise: nothing after the failed step runs."""
        active: list[str] = []
        try:
            for step in steps:
                self._set_scopes(active, step.scope)
                try:
                    step.run()
                except BaseException:
                    if step.on_failure:
                        step.on_failure()
                    raise
            self._set_scopes(active, ())
        except BaseException:
            self._leave_after_failure(active)
            raise

    def _set_scopes(self, active: list, wanted: tuple) -> None:
        for scope in reversed(list(active)):
            if scope not in wanted:
                active.remove(scope)
                self._leave(scope)
        for scope in SCOPES:
            if scope in wanted and scope not in active:
                self._enter(scope)
                active.append(scope)

    def _enter(self, scope: str) -> None:
        if scope == DEVBOXES_OFF:
            self._devboxes_off()
        else:
            self.internet_allow()

    def _leave(self, scope: str) -> None:
        if scope == DEVBOXES_OFF:
            self._devboxes_on()
        else:
            self._close_window()

    def _leave_after_failure(self, active: list) -> None:
        """After a failed step: close the window and start the devbox VMs again. A window
        that does not close is the louder error, and replaces the step's."""
        still_open = None
        for scope in reversed(active):
            if scope == INTERNET:
                _stopping("closing the internet window")
                try:
                    self._close_window()
                except BuildError as exc:
                    still_open = exc
            else:
                try:
                    self._devboxes_on()
                except BuildError as exc:
                    output.error(str(exc))
        active.clear()
        if still_open is not None:
            raise still_open

    # -- primitives ----------------------------------------------------------

    def secrets(self):
        self.runner.secrets()

    def init(self):
        for stack in INIT_STACKS:
            self.runner.tofu(stack, "init", "-input=false")

    def firewall_migrate(self):
        # One-time state migration for deployments made before the firewall
        # stack (moves the firewall resources out of the platform state). No-op
        # on fresh installs and once migrated. A platform without a state has
        # nothing to move; any other failure of `tofu state list` stops it.
        script = (
            "if [ -e ../platform/terraform.tfstate ]; then "
            "resources=$(tofu -chdir=../platform state list) || exit 1; "
            "for r in $(printf '%s\\n' \"$resources\" | "
            "grep -E '^proxmox_(virtual_environment_cluster_firewall|node_firewall)'); do "
            "tofu state mv -state=../platform/terraform.tfstate -state-out=terraform.tfstate \"$r\" \"$r\" || exit 1; "
            "done; "
            "fi; "
            "if [ -n \"$(tofu state list 2>/dev/null)\" ] && ! tofu output -json security_groups >/dev/null 2>&1; then "
            "echo 'firewall: writing outputs (refresh-only)'; "
            "tofu apply -refresh-only -auto-approve -input=false || exit 1; "
            "fi"
        )
        self.runner.sh(script, cwd="opentofu/stacks/firewall", tofu=True)

    def _apply(self, stack: str):
        self._facts_fresh = False
        self.runner.tofu(stack, "apply", "-auto-approve", "-parallelism=1", "-input=false")

    def _destroy(self, stack: str):
        self._facts_fresh = False
        # One at a time, like _apply: VMs destroyed in parallel fight over the storage
        # lock, and Proxmox gives up on their disks with only a warning. The volume
        # left behind then blocks whichever new VM gets that ID.
        self.runner.tofu(stack, "destroy", "-auto-approve", "-parallelism=1")

    def platform(self):
        self.firewall_migrate()
        self._apply("platform")

    def firewall(self):
        self.firewall_migrate()
        self._apply("firewall")

    def host_network(self):
        self.inventory()
        self._ansible("SetupHostNetwork.yml")

    def _plan_infrastructure(self) -> list[Step]:
        # A failed apply still regenerates the inventory, so it matches the state.
        return [
            *(Step(f"apply {stack}", partial(self._apply, stack), on_failure=self._inventory_after_failed_apply)
              for stack in ("private_internet", "infrastructure/persistent", "infrastructure/ephemeral")),  # fmt: skip
            Step("inventory", self.inventory),
        ]

    def _inventory_after_failed_apply(self) -> None:
        _stopping("regenerating the inventory to match the state")
        try:
            self.inventory()
        except BuildError as exc:  # the apply's error is the one to report
            output.error(f"regenerating the inventory failed too: {exc}")

    def inventory(self):
        self.runner.python("scripts/generate_inventory.py")

    def show_config(self):
        self.runner.python("scripts/generate_inventory.py", "--show")

    def _plan_configure(self) -> list[Step]:
        return [
            Step("inventory", self.inventory),
            *(Step(playbook, partial(self._ansible, playbook)) for playbook in CONFIGURE_PLAYBOOKS),
        ]

    def offline(self):
        if self.runner.flag_exists(INTERNET_FLAG):
            raise BuildError(f"internet access is still allowed ({INTERNET_FLAG}): run `worms internet disable`")
        self._ansible("SetupDNS.yml")
        self._ansible("SetupPrivateInternet.yml")

    # Without the user's Ansible arguments: Internet.yml runs on the node, so a
    # `--limit <vm>` would silently skip it, and the window would neither open
    # nor close.
    def internet_allow(self):
        self._ansible("Internet.yml", "-e", "internet=allowed", user_args=False)

    def internet_deny(self):
        self._ansible("Internet.yml", "-e", "internet=denied", user_args=False)

    def _close_window(self) -> None:
        try:
            self.internet_deny()
        except BuildError as exc:
            raise BuildError("!!! INTERNET ACCESS IS STILL ALLOWED: run `worms internet disable` -- " + str(exc)) from exc

    def online(self, step):
        """Run `step` with internet access allowed, then deny it again whatever
        happens (success, failure, interrupt). A failed deny is loud."""
        self._execute([Step("online", step, scope=(INTERNET,))])

    # -- composite targets ---------------------------------------------------

    def build_all(self):
        """The full pipeline: secrets -> init -> platform -> firewall ->
        host-network -> [devboxes off] services to their snapshot -> [internet]
        infrastructure + configure [/internet] -> offline -> services snapshotted
        [/devboxes off] -> show-config."""
        self._execute(self._plan_build_all())

    def _plan_build_all(self) -> list[Step]:
        return [
            # First: every later step needs the vault, and says so if it lacks something.
            Step("secrets", self.secrets),
            Step("init", self.init),
            Step("platform", self.platform),
            Step("firewall", self.firewall),
            Step("host network", self.host_network),
            *self._on_clean_services(self._plan_infrastructure() + self._plan_configure()),
            Step("show config", self._setup_complete),
        ]

    def _setup_complete(self) -> None:
        self.show_config()
        print(output.color("== setup complete (internet access denied) ==", "green"))

    def _on_clean_services(self, steps: list[Step]) -> list[Step]:
        """`steps` under the internet window, on service VMs rolled back to their
        snapshot, which is then taken again: what they changed is kept, and
        nothing a team VM did. The devbox VMs are off from the rollback to the new
        snapshot. A service VM without a snapshot that was there before gets none:
        team VMs may have reached it."""
        return [
            Step("roll the service VMs back to their snapshot", self._roll_back_for_build, scope=(DEVBOXES_OFF,)),
            *(replace(step, scope=(DEVBOXES_OFF, INTERNET)) for step in steps),
            Step("offline: DNS and the caches", self.offline, scope=(DEVBOXES_OFF,)),
            Step("snapshot the service VMs", self._snapshot_after_build, scope=(DEVBOXES_OFF,)),
        ]

    def _roll_back_for_build(self, recreated=()) -> None:
        """`recreated`: VMs this build recreates, which count as new (their snapshot is taken again)."""
        self.data["services_before"] = [vm["vmid"] for vm in self._service_vms() if vm["name"] not in recreated]
        self.data["services_reset"] = sorted(self._roll_back_services())

    def _snapshot_after_build(self) -> None:
        before, reset = set(self.data["services_before"] or ()), set(self.data["services_reset"] or ())
        self._snapshot_services(lambda vm: vm["vmid"] in reset or vm["vmid"] not in before)

    # -- the service VMs: reset, snapshot, rebuild ------------------------------

    def devboxes_exist(self) -> bool:
        try:
            return any(vm.get("pool") == "devbox" for vm in self.runner.vms())
        except BuildError as exc:
            output.warn(f"cannot tell whether devbox VMs exist ({exc}): their map is written again to be sure")
            return True

    def _service_vms(self) -> list[dict]:
        """The service VMs; during a build of some components (build_components), only theirs."""
        only = self.data.get("service_names")
        return sorted((vm for vm in self.runner.vms() if vm.get("pool") in SERVICE_POOLS and (only is None or vm["name"] in only)),
                      key=lambda vm: vm["vmid"])  # fmt: skip

    def _refuse_vms_in(self, pools: tuple, why: str) -> None:
        names = sorted(vm["name"] for vm in self.runner.vms() if vm.get("pool") in pools)
        if names:
            raise BuildError(f"{why} ({len(names)} VM(s): {', '.join(names)})")

    def _wait_for(self, vms: list[dict]) -> None:
        if vms:
            self._facts_fresh = False
            # A VM's group in the inventory is its name with underscores.
            limit = ",".join(vm["name"].replace("-", "_") for vm in vms)
            self._ansible("WaitForVMs.yml", "--limit", limit, user_args=False)

    def _take_snapshot(self, vm: dict) -> None:
        # Shut down first: these VMs run no guest agent, so a snapshot of a running
        # one would hold only what a power cut leaves on the disk.
        vmid = vm["vmid"]
        if vm.get("status") == "running":
            self.runner.node_sh(f"qm shutdown {vmid} --timeout 120 --forceStop 1")
        self.runner.node_sh(f"qm snapshot {vmid} {SERVICE_SNAPSHOT} --description {shlex.quote(f'worms {self.runner.commit()}')}")
        self.runner.node_sh(f"qm start {vmid}")

    def _devboxes_off(self) -> None:
        """No team VM runs while the internet window is open (docs/IMPORTANT): it
        is refused while a sandbox exists, and the devbox VMs that run are shut
        down meanwhile and started again once it is closed. Teams lose their
        devbox for that long."""
        self._refuse_vms_in(("sandbox",), "Refusing to open the internet window: sandbox VMs exist (a round is on)")
        running = [vm for vm in self.runner.vms() if vm.get("pool") == "devbox" and vm.get("status") == "running"]
        if running:
            print(output.color(f"shutting down {len(running)} devbox VM(s) while internet access is allowed", "dim"))
        for vm in running:
            # Noted first: one that is off when a later shutdown fails is started again too.
            self.data["devboxes_off"] = sorted({*self.data["devboxes_off"], vm["vmid"]})
            self.runner.node_sh(f"qm shutdown {vm['vmid']} --timeout 120 --forceStop 1")

    def _devboxes_on(self) -> None:
        """Start the devbox VMs this run shut down, unless the window is still open."""
        off = list(self.data["devboxes_off"])
        if not off:
            return
        start = f"qm start {' '.join(str(vmid) for vmid in off)}"
        if self.runner.flag_exists(INTERNET_FLAG):
            raise BuildError(
                f"the devbox VMs stay off while internet access is allowed: start them after `worms internet disable` ({start})"
            )
        failed = [vmid for vmid in off if self.runner.node_sh(f"qm start {vmid}", check=False) != 0]
        if failed:  # `qm start` also fails on a VM that runs already
            running = {vm["vmid"] for vm in self.runner.vms() if vm.get("status") == "running"}
            failed = [vmid for vmid in failed if vmid not in running]
        self.data["devboxes_off"] = failed
        if failed:
            raise BuildError(f"devbox VM(s) {', '.join(map(str, failed))} did not start again: `qm start <vmid>` on the node")

    def _roll_back_services(self) -> set:
        """The service VMs that have a snapshot back to it, running. Returns their vmids."""
        clean = [vm for vm in self._service_vms() if self.runner.snapshot(vm, SERVICE_SNAPSHOT)]
        for vm in clean:
            self.runner.node_sh(f"qm rollback {vm['vmid']} {SERVICE_SNAPSHOT} --start 1")
        self._wait_for(clean)
        self.services_rolled_back = self.services_rolled_back or bool(clean)
        return {vm["vmid"] for vm in clean}

    def _snapshot_services(self, clean) -> None:
        """Take the snapshot again of the service VMs `clean(vm)` vouches for; say
        which are left without one."""
        vms = self._service_vms()
        taken = [vm for vm in vms if clean(vm)]
        for vm in taken:
            if self.runner.snapshot(vm, SERVICE_SNAPSHOT):
                self.runner.node_sh(f"qm delsnapshot {vm['vmid']} {SERVICE_SNAPSHOT}")
            self._take_snapshot(vm)
        self._wait_for(taken)
        left = sorted(vm["name"] for vm in vms if vm not in taken)
        if left:
            output.warn(
                f"no snapshot of {', '.join(left)}: up before this build without one, so team VMs may have reached "
                "them. A reset needs one on every service VM: `worms build services --rebuild`"
            )

    def snapshot_services(self):
        """Make the service VMs' present state the one a reset returns to. Team
        VMs are assumed malicious and can reach these services, so never with any
        of them around; earlier exposure is for the operator to rule out."""
        self._refuse_vms_in(TEAM_POOLS, "Refusing to snapshot: team VMs can reach the services, so they may not be clean")
        self._snapshot_services(lambda vm: True)

    def round_reset_services(self):
        """Return the service VMs to their snapshot, by hand: the round agent does
        it itself before it builds a round's sandbox. Nothing is rebuilt and no
        internet is needed, so devbox VMs can stay. What only lives on these VMs
        goes with it: the devbox map, which `worms reset services` then has the round agent
        write again (src/manage.py)."""
        self._refuse_vms_in(("sandbox",), "Refusing to reset during a round: sandbox VMs exist")
        vms = self._service_vms()
        snapshots = {vm["name"]: self.runner.snapshot(vm, SERVICE_SNAPSHOT) for vm in vms}
        missing = sorted(name for name, snapshot in snapshots.items() if not snapshot)
        if missing or not vms:
            raise BuildError(
                f"nothing was reset: no snapshot on {', '.join(missing) or 'any service VM'}. "
                "Make one with `worms build services --rebuild`"
            )
        oldest = min(snapshots.values(), key=lambda snapshot: snapshot["snaptime"])
        taken = datetime.fromtimestamp(oldest["snaptime"]).isoformat(sep=" ", timespec="minutes")
        print(output.color(f"resetting {len(vms)} VMs to their snapshot of {taken} ({oldest.get('description', '').strip()})", "dim"))
        for vm in vms:
            self.runner.node_sh(f"qm rollback {vm['vmid']} {SERVICE_SNAPSHOT} --start 1")
        self._wait_for(vms)
        self.services_rolled_back = True

    # -- `worms build <component>`: some components (worms_cli/components.py) ---

    def build_components(self, plan, packages: bool = False) -> None:
        """Build the plan's components (components.plan): their stacks, their VMs (`tofu apply
        -target`, `-replace` to recreate), their share of the configuration (`--limit`), node
        plays. Packages need the internet window: when a VM is new or recreated, or with
        `packages`. Service VMs are configured from their snapshot and snapshotted again, with
        the devboxes off (as a full build does)."""
        self._execute(self._plan_components(plan, packages))

    def _plan_components(self, plan, packages: bool) -> list[Step]:
        from worms_cli.components import COMPONENTS

        actions = dict(plan.steps)
        chosen = [COMPONENTS[name] for name, _ in plan.steps]
        vms = [c for c in chosen if c.kind == "vm"]
        new = [c for c in vms if actions[c.name] in ("create", "recreate", "dependency")]
        services = [c for c in vms if c.service]
        window = bool(new) or (packages and bool(vms))
        steps = [Step("secrets", self.secrets)]
        for c in chosen:
            if c.kind == "stack":
                steps += [Step(f"init {c.stack}", partial(self.runner.tofu, c.stack, "init", "-input=false")),
                          Step(f"apply {c.stack}", getattr(self, c.name.replace("-", "_")))]  # fmt: skip
        if "host-network" in actions:
            steps.append(Step("host network", self.host_network))
        if vms:
            scope = (DEVBOXES_OFF, INTERNET) if window else ((DEVBOXES_OFF,) if services else ())
            if services:
                # Before OpenTofu: a VM it creates (or recreates) was not up before, and gets its
                # first snapshot at the end.
                self.data["service_names"] = {c.name for c in services}
                recreated = tuple(c.name for c in services if actions[c.name] == "recreate")
                steps.append(Step(f"roll {', '.join(c.name for c in services)} back to their snapshot",
                                  partial(self._roll_back_for_build, recreated), scope=(DEVBOXES_OFF,)))  # fmt: skip
            for stack in dict.fromkeys(c.stack for c in new):
                targets = [c for c in new if c.stack == stack]
                steps += [Step(f"init {stack}", partial(self.runner.tofu, stack, "init", "-input=false"), scope=scope),
                          Step(f"apply {stack}: {', '.join(c.name for c in targets)}",
                               partial(self._apply_targets, stack, targets, [c for c in targets if actions[c.name] == "recreate"]),
                               scope=scope, on_failure=self._inventory_after_failed_apply)]  # fmt: skip
            steps.append(Step("inventory", self.inventory, scope=scope))
            hosts = [c.group for c in vms]
            if "orchestrator" in actions and "database" not in actions:
                hosts.append("database")  # its grants, for the tables the migrations add
            limit = ",".join([*hosts, "localhost"])
            extra = ("--limit", limit) if window else ("--limit", limit, "--skip-tags", "packages")
            for playbook in CONFIGURE_PLAYBOOKS:
                if playbook != "SetupRoundAgent.yml":
                    steps.append(Step(f"{playbook} ({limit})", partial(self._ansible, playbook, *extra), scope=scope))
            offline = [c.group for c in vms if c.name == "dns" or c.stack == "private_internet"]
            if window and offline:
                steps.append(Step("offline: DNS and the caches", partial(self._offline_hosts, ",".join(offline)),
                                  scope=(DEVBOXES_OFF,) if services else ()))  # fmt: skip
            if services:
                steps.append(Step("snapshot them again", self._snapshot_after_build,
                                  scope=(DEVBOXES_OFF,)))  # fmt: skip
        if "round-agent" in actions:
            steps.append(Step("round agent", partial(self._ansible, "SetupRoundAgent.yml")))
        return steps

    def _apply_targets(self, stack: str, targets, recreate) -> None:
        """`tofu apply` of just these VMs of `stack` (and what they need), recreating `recreate`."""
        self._facts_fresh = False
        args = [f"-target={shlex.quote(c.module)}" for c in targets]
        args += [f"-replace={shlex.quote(f'{c.module}.{c.resource}')}" for c in recreate]
        self.runner.tofu(stack, "apply", "-auto-approve", "-parallelism=1", "-input=false", *args)

    def _offline_hosts(self, limit: str) -> None:
        if self.runner.flag_exists(INTERNET_FLAG):
            raise BuildError(f"internet access is still allowed ({INTERNET_FLAG}): run `worms internet disable`")
        self._ansible("SetupDNS.yml", "--limit", limit, user_args=False)
        self._ansible("SetupPrivateInternet.yml", "--limit", limit, user_args=False)

    # -- rounds / devbox -----------------------------------------------------

    def round_configure_sandbox(self):
        self.inventory()
        self._ansible("SetupSandbox.yml")

    def devbox_apply(self):
        self._apply("devbox")
        self.inventory()

    def devbox_configure(self):
        self.inventory()
        self._ansible("SetupSSHGateway.yml")
        self._ansible("SetupDevbox.yml")

    def devbox(self):
        self.devbox_apply()
        self.devbox_configure()

    def devbox_destroy(self):
        self._destroy("devbox")

    # -- debugging / tests ---------------------------------------------------

    def ping(self):
        self.runner.sh("ansible 'all:!proxmox_host' -m ansible.builtin.ping -o", cwd="ansible")

    def debug(self, host: str):
        extra = (" " + self.ansible_args) if self.ansible_args else ""
        self.runner.sh(f"ansible-playbook Debug.yml --flush-cache -e target={host}{extra}", cwd="ansible")

    def simulate(self):
        self.inventory()
        self._ansible("Simulate.yml")

    # -- teardown ------------------------------------------------------------

    def destroy(self):
        """Destroy the VM stacks (they reference the firewall outputs even on
        destroy, so migrate first). Keeps the firewall and platform."""
        self.firewall_migrate()
        for stack in ["sandbox", "devbox", "infrastructure/ephemeral", "infrastructure/persistent", "private_internet"]:
            self._destroy(stack)

    def destroy_all(self):
        """Destroy everything: the VM stacks, then the firewall and platform."""
        self.destroy()
        self._destroy("firewall")
        self._destroy("platform")

    def stop_all(self):
        """Freeze: hard-stop every running VM on the node, destroy nothing."""
        self.runner.node_sh("qm list | awk 'NR>1 && $3 == \"running\" {print $1}' | xargs -r -I {} qm stop {}")

    def terminate(self):
        """Stop, unlock and destroy every VM, then destroy all stacks (the full
        teardown the Makefile's `terminate` did). Best effort on the VMs: one
        that is gone already is no reason to stop."""
        self.runner.node_sh("qm list | awk 'NR>1 {print $1}' | xargs -r -I {} qm stop {}", check=False)
        self.runner.node_sh("qm list | awk 'NR>1 {print $1}' | xargs -r -I {} qm unlock {}", check=False)
        self.destroy_all()
        self.runner.node_sh("qm list | awk 'NR>1 {print $1}' | xargs -r -I {} qm destroy {}", check=False)


# `worms step <name>`: single steps of the pipeline, for the round agent (it runs them in its
# workspace, never interactively) and for troubleshooting. name -> (method, one-line help).
STEPS: dict[str, tuple[str, str]] = {
    "init": ("init", "tofu init every stack"),
    "inventory": ("inventory", "regenerate the Ansible inventory from the OpenTofu stacks"),
    "show-config": ("show_config", "print the hosts Ansible will contact"),
    "ping": ("ping", "one line per VM: can Ansible reach it?"),
    "offline": ("offline", "switch DNS and the caches to offline (internet denied)"),
    "snapshot-services": ("snapshot_services",
                          "snapshot the service VMs as they are, as what a reset returns to (no team VM may exist)"),
    "round-configure-sandbox": ("round_configure_sandbox", "configure a round's freshly built sandbox VMs (round agent)"),
    "devbox-apply": ("devbox_apply", "apply the devbox stack (round agent)"),
    "devbox-configure": ("devbox_configure", "configure the devbox VMs and the SSH gateway (round agent)"),
}
