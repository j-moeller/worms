"""The build/operate pipeline: every step the top-level Makefile used to run,
now driven from the CLI. Each step runs the real tools (tofu, ansible-playbook,
the generator scripts, qm) through a Runner: on this machine (your laptop, or
the node), and the node's own part (sandbox and devbox stacks) on the node
(worms_cli/session.py).

The internet window is the one subtle bit: infrastructure setup needs outbound
internet (cloud-init and Ansible install packages, the caches populate), so it
is allowed only around that step and ALWAYS denied again afterwards -- on
success, failure or interrupt -- exactly as the Makefile's `online` trap did.

OpenTofu gets its Proxmox credentials from the vault, through the Runner
(TF_VAR_* in the tofu process's environment only); see worms_cli/secrets.py.
"""

from __future__ import annotations

import shlex
from datetime import datetime

from worms_cli import output
from worms_cli.build import BuildError, Runner

INTERNET_FLAG = "/run/worms/internet-allowed"

# The VMs a reset returns to their snapshot: the services a worm can talk to. The
# persistent VMs hold the records, and the team VMs are the teams' own.
SERVICE_POOLS = ("ephemeral", "private_internet")
TEAM_POOLS = ("sandbox", "devbox")
SERVICE_SNAPSHOT = "clean"

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


class Pipeline:
    def __init__(self, config, ansible_args: str = ""):
        self.runner = Runner(config)
        # Extra args appended to every ansible-playbook call.
        self.ansible_args = ansible_args
        # Ansible caches facts for the run (ansible.cfg); start without any,
        # and drop them again whenever tofu may have rebuilt VMs.
        self._facts_fresh = False

    def _ansible(self, playbook: str, *args: str, vault: bool = True, user_args: bool = True):
        if not self._facts_fresh:
            self.runner.sh("rm -rf .facts", cwd="ansible", quiet=True)
            self._facts_fresh = True
        extra = list(args)
        if self.ansible_args and user_args:
            extra.append(self.ansible_args)
        self.runner.ansible(playbook, *extra, vault=vault)

    # -- primitives ----------------------------------------------------------

    def secrets(self):
        self.runner.secrets()

    def init(self):
        for stack in INIT_STACKS:
            self.runner.tofu(stack, "init", "-input=false")

    def firewall_migrate(self):
        # One-time state migration for deployments made before the firewall
        # stack (moves the firewall resources out of the platform state). No-op
        # on fresh installs and once migrated. Ported verbatim from the Makefile.
        script = (
            "for r in $(tofu -chdir=../platform state list 2>/dev/null | "
            "grep -E '^proxmox_(virtual_environment_cluster_firewall|node_firewall)'); do "
            "tofu state mv -state=../platform/terraform.tfstate -state-out=terraform.tfstate \"$r\" \"$r\" || exit 1; "
            "done; "
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

    def infrastructure(self):
        # Regenerate the inventory afterwards whatever happens (a failed apply
        # still leaves the inventory matching the state).
        try:
            self._apply("private_internet")
            self._apply("infrastructure/persistent")
            self._apply("infrastructure/ephemeral")
        finally:
            self.inventory()

    def inventory(self):
        self.runner.python("scripts/generate_inventory.py")

    def show_config(self):
        self.runner.python("scripts/generate_inventory.py", "--show")

    def configure(self):
        self.inventory()
        for playbook in CONFIGURE_PLAYBOOKS:
            self._ansible(playbook)

    def offline(self):
        if self.runner.flag_exists(INTERNET_FLAG):
            raise BuildError(f"internet access is still allowed ({INTERNET_FLAG}): run `worms build internet-deny`")
        self._ansible("SetupDNS.yml")
        self._ansible("SetupPrivateInternet.yml")

    # Without the user's Ansible arguments: Internet.yml runs on the node, so a
    # `--limit <vm>` would silently skip it, and the window would neither open
    # nor close.
    def internet_allow(self):
        self._ansible("Internet.yml", "-e", "internet=allowed", user_args=False)

    def internet_deny(self):
        self._ansible("Internet.yml", "-e", "internet=denied", user_args=False)

    def online(self, step):
        """Run `step` with internet access allowed, then deny it again whatever
        happens (success, failure, interrupt). A failed deny is loud."""
        self.internet_allow()
        try:
            step()
        finally:
            try:
                self.internet_deny()
            except BuildError as exc:
                raise BuildError(
                    "!!! INTERNET ACCESS IS STILL ALLOWED: run `worms build internet-deny` -- " + str(exc)
                )

    # -- composite targets ---------------------------------------------------

    def build_all(self):
        """The full pipeline: init -> secrets -> platform -> firewall ->
        host-network -> [internet] infrastructure + configure [/internet] ->
        offline -> snapshot of the service VMs -> show-config."""
        self.init()
        self.secrets()
        self.platform()
        self.firewall()
        self.host_network()
        self.online(lambda: (self.infrastructure(), self.configure()))
        self.offline()
        self.snapshot_new_services()
        self.show_config()
        print(output.color("== setup complete (internet access denied) ==", "green"))

    def provision(self):
        """Ansible only: configure under the internet window, then offline."""
        self.online(self.configure)
        self.offline()
        self._warn_of_older_snapshots(self._service_vms())

    # -- the service VMs: reset, snapshot, rebuild ------------------------------

    def _service_vms(self) -> list[dict]:
        return sorted((vm for vm in self.runner.vms() if vm.get("pool") in SERVICE_POOLS), key=lambda vm: vm["vmid"])

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

    def _warn_of_older_snapshots(self, vms: list[dict]) -> None:
        if any(self.runner.snapshot(vm, SERVICE_SNAPSHOT) for vm in vms):
            output.warn(
                "a reset returns the service VMs to their older snapshot, without what this build changed on them. "
                "To keep it: `worms build rebuild-services`"
            )

    def snapshot_new_services(self):
        """Snapshot the service VMs that have none, after a build. A snapshot that
        exists is kept: VMs that have been up since may no longer be clean."""
        vms = self._service_vms()
        new = [vm for vm in vms if not self.runner.snapshot(vm, SERVICE_SNAPSHOT)]
        for vm in new:
            self._take_snapshot(vm)
        self._wait_for(new)
        self._warn_of_older_snapshots([vm for vm in vms if vm not in new])

    def snapshot_services(self):
        """Make the service VMs' present state the one a reset returns to. Team
        VMs are assumed malicious and can reach these services, so never with any
        of them around; earlier exposure is for the operator to rule out."""
        self._refuse_vms_in(TEAM_POOLS, "Refusing to snapshot: team VMs can reach the services, so they may not be clean")
        vms = self._service_vms()
        for vm in vms:
            if self.runner.snapshot(vm, SERVICE_SNAPSHOT):
                self.runner.node_sh(f"qm delsnapshot {vm['vmid']} {SERVICE_SNAPSHOT}")
            self._take_snapshot(vm)
        self._wait_for(vms)

    def round_reset_services(self):
        """Between rounds: return the service VMs to their snapshot. Nothing is
        rebuilt and no internet is needed, so devbox VMs can stay. What only lives
        on these VMs goes with it: the devbox map, which `worms build` then has
        the round agent write again from the database (src/manage.py)."""
        self._refuse_vms_in(("sandbox",), "Refusing to reset during a round: sandbox VMs exist")
        vms = self._service_vms()
        snapshots = {vm["name"]: self.runner.snapshot(vm, SERVICE_SNAPSHOT) for vm in vms}
        missing = sorted(name for name, snapshot in snapshots.items() if not snapshot)
        if missing or not vms:
            raise BuildError(
                f"nothing was reset: no snapshot on {', '.join(missing) or 'any service VM'}. "
                "Make one with `worms build rebuild-services`"
            )
        oldest = min(snapshots.values(), key=lambda snapshot: snapshot["snaptime"])
        taken = datetime.fromtimestamp(oldest["snaptime"]).isoformat(sep=" ", timespec="minutes")
        print(output.color(f"resetting {len(vms)} VMs to their snapshot of {taken} ({oldest.get('description', '').strip()})", "dim"))
        for vm in vms:
            self.runner.node_sh(f"qm rollback {vm['vmid']} {SERVICE_SNAPSHOT} --start 1")
        self._wait_for(vms)

    def rebuild_services(self):
        """Rebuild the service VMs from nothing and snapshot them: for when the
        snapshot no longer fits, such as after changing their configuration. The
        internet window is refused while team VMs exist, so say so before
        destroying anything."""
        self._refuse_vms_in(TEAM_POOLS, "Refusing to destroy the services: rebuilding needs the internet window, refused while team VMs exist")
        self._destroy("infrastructure/ephemeral")
        self._destroy("private_internet")
        self.online(self._apply_and_configure_services)
        self.offline()
        self.snapshot_new_services()

    def _apply_and_configure_services(self):
        self._apply("private_internet")
        self._apply("infrastructure/ephemeral")
        self.inventory()
        self.configure()

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
        teardown the Makefile's `terminate` did)."""
        self.runner.node_sh("qm list | awk 'NR>1 {print $1}' | xargs -r -I {} qm stop {}", check=False)
        self.runner.node_sh("qm list | awk 'NR>1 {print $1}' | xargs -r -I {} qm unlock {}", check=False)
        self.destroy_all()
        self.runner.node_sh("qm list | awk 'NR>1 {print $1}' | xargs -r -I {} qm destroy {}", check=False)


# component name -> (Pipeline method name, one-line help). `worms build` with no
# component runs the full pipeline (build_all).
COMPONENTS: dict[str, tuple[str, str]] = {
    "all": ("build_all", "full idempotent end-to-end setup (the default)"),
    "secrets": ("secrets", "generate management secrets into the vault"),
    "init": ("init", "tofu init every stack"),
    "platform": ("platform", "bridges, images, cloud-init, pools, hookscript"),
    "firewall": ("firewall", "Proxmox cluster/node firewall + security groups"),
    "host-network": ("host_network", "host nftables/sysctls (SetupHostNetwork.yml)"),
    "infrastructure": ("infrastructure", "create the base infrastructure VMs"),
    "inventory": ("inventory", "regenerate the Ansible inventory from OpenTofu"),
    "show-config": ("show_config", "print the hosts Ansible will contact"),
    "configure": ("configure", "Ansible configuration of all VMs"),
    "provision": ("provision", "configure under the internet window, then offline"),
    "offline": ("offline", "switch DNS/caches to offline (internet denied)"),
    "internet-allow": ("internet_allow", "open the internet window by hand (refused while a devbox/sandbox VM exists)"),
    "internet-deny": ("internet_deny", "close the internet window by hand, if a build reports it still open"),
    "round-reset-services": (
        "round_reset_services",
        "between rounds: return the ephemeral + private-internet VMs to their snapshot (devboxes can stay)",
    ),
    "rebuild-services": (
        "rebuild_services",
        "rebuild those VMs from nothing, and snapshot them (with no devbox or sandbox VM left)",
    ),
    "snapshot-services": (
        "snapshot_services",
        "snapshot those VMs as they are, as what a reset returns to (with no devbox or sandbox VM left)",
    ),
    "round-configure-sandbox": ("round_configure_sandbox", "configure freshly built sandbox VMs (CA)"),
    "devbox": ("devbox", "build + configure the team devboxes and SSH gateway"),
    "devbox-apply": ("devbox_apply", "apply the devbox stack only"),
    "devbox-configure": ("devbox_configure", "configure devbox VMs + SSH gateway only"),
    "ping": ("ping", "one line per host: can Ansible reach it?"),
    "simulate": ("simulate", "end-to-end simulation against the deployed platform"),
}
