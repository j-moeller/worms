"""The build/operate pipeline: every step the top-level Makefile used to run,
now driven from the CLI. Each step runs the real tools (tofu, ansible-playbook,
the generator scripts, qm) through a Runner, locally on the Proxmox node or
over SSH (WORMS_SSH).

The internet window is the one subtle bit: infrastructure setup needs outbound
internet (cloud-init and Ansible install packages, the caches populate), so it
is allowed only around that step and ALWAYS denied again afterwards -- on
success, failure or interrupt -- exactly as the Makefile's `online` trap did.

Tofu reads its Proxmox credentials from the operator's environment on the node
(TF_VAR_proxmox_api_url / TF_VAR_proxmox_api_token, or a tfvars file), the same
way the stack Makefiles did; the pipeline does not handle them.
"""

from __future__ import annotations

from worms_cli import output
from worms_cli.build import BuildError, Runner

INTERNET_FLAG = "/run/worms/internet-allowed"

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

# The configure playbook sequence (was `make configure`).
CONFIGURE_PLAYBOOKS = [
    "WaitForVMs.yml",
    "SetupDNS.yml",
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
    def __init__(self, config, dry_run: bool = False, ansible_args: str = ""):
        self.runner = Runner(config, dry_run=dry_run)
        # Extra args appended to every ansible-playbook call (was make ARGS=...).
        self.ansible_args = ansible_args
        # Ansible caches facts for the run (ansible.cfg); start without any,
        # and drop them again whenever tofu may have rebuilt VMs.
        self._facts_fresh = False

    def _ansible(self, playbook: str, *args: str, vault: bool = True):
        if not self._facts_fresh:
            self.runner.sh("rm -rf .facts", cwd="ansible", quiet=True)
            self._facts_fresh = True
        extra = list(args)
        if self.ansible_args:
            extra.append(self.ansible_args)
        self.runner.ansible(playbook, *extra, vault=vault)

    # -- primitives ----------------------------------------------------------

    def secrets(self):
        self.runner.python("scripts/generate_secrets.py")

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
        self.runner.sh(script, cwd="opentofu/stacks/firewall")

    def _apply(self, stack: str):
        self._facts_fresh = False
        self.runner.tofu(stack, "apply", "-auto-approve", "-parallelism=1", "-input=false")

    def _destroy(self, stack: str):
        self._facts_fresh = False
        self.runner.tofu(stack, "destroy", "-auto-approve")

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
            raise BuildError(f"internet access is still allowed ({INTERNET_FLAG}): run `worms internet-deny`")
        self._ansible("SetupDNS.yml")
        self._ansible("SetupPrivateInternet.yml")

    def internet_allow(self):
        self._ansible("Internet.yml", "-e", "internet=allowed")

    def internet_deny(self):
        self._ansible("Internet.yml", "-e", "internet=denied")

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
                    "!!! INTERNET ACCESS IS STILL ALLOWED: run `worms internet-deny` -- " + str(exc)
                )

    # -- composite targets ---------------------------------------------------

    def build_all(self):
        """The full pipeline (was `make`): init -> secrets -> platform ->
        firewall -> host-network -> [internet] infrastructure + configure
        [/internet] -> offline -> show-config."""
        self.init()
        self.secrets()
        self.platform()
        self.firewall()
        self.host_network()
        self.online(lambda: (self.infrastructure(), self.configure()))
        self.offline()
        self.show_config()
        print(output.color("== setup complete (internet access denied) ==", "green"))

    def provision(self):
        """Ansible only (was `make provision`): configure under the internet
        window, then offline."""
        self.online(self.configure)
        self.offline()

    # -- rounds / devbox -----------------------------------------------------

    def round_reset_services(self):
        self._destroy("infrastructure/ephemeral")
        self._destroy("private_internet")
        self.online(self._round_reset_apply_and_configure)
        self.offline()

    def _round_reset_apply_and_configure(self):
        self._apply("private_internet")
        self._apply("infrastructure/ephemeral")
        self.inventory()
        self.configure()

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
        self.runner.sh("qm list | awk 'NR>1 && $3 == \"running\" {print $1}' | xargs -r -I {} qm stop {}")

    def terminate(self):
        """Stop, unlock and destroy every VM, then destroy all stacks (the full
        teardown the Makefile's `terminate` did)."""
        self.runner.sh("qm list | awk 'NR>1 {print $1}' | xargs -r -I {} qm stop {}", check=False)
        self.runner.sh("qm list | awk 'NR>1 {print $1}' | xargs -r -I {} qm unlock {}", check=False)
        self.destroy_all()
        self.runner.sh("qm list | awk 'NR>1 {print $1}' | xargs -r -I {} qm destroy {}", check=False)


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
    "internet-allow": ("internet_allow", "allow VM internet access (provisioning)"),
    "internet-deny": ("internet_deny", "deny VM internet access"),
    "round-reset-services": ("round_reset_services", "rebuild ephemeral + private-internet for a clean round"),
    "round-configure-sandbox": ("round_configure_sandbox", "configure freshly built sandbox VMs (CA)"),
    "devbox": ("devbox", "build + configure the team devboxes and SSH gateway"),
    "devbox-apply": ("devbox_apply", "apply the devbox stack only"),
    "devbox-configure": ("devbox_configure", "configure devbox VMs + SSH gateway only"),
    "ping": ("ping", "one line per host: can Ansible reach it?"),
    "simulate": ("simulate", "end-to-end simulation against the deployed platform"),
}
