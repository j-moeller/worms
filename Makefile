# =============================================================================
# worms-platform
#
#   make              Idempotent, end-to-end setup of the base infrastructure:
#                       init -> secrets -> apply -> inventory -> provision
#                     Provisioning installs packages: allow internet first.
#   make show-config  Print the VM names + IP addresses that are configured.
#   make private_internet  Re-apply the private-internet VMs and caches (online
#                     or offline mode follows the current internet state).
#   make startround   Bring up the per-round sandbox (generate it first).
#   make endround     Tear the sandbox back down.
#
# Internet access for VMs is never implicit (as root on the Proxmox node):
#   host/allow-internet.sh   refused while sandbox VMs exist
#   host/deny-internet.sh    required before `make startround`
#
# The Proxmox host's own network security (sysctls, nftables) is configured by
# Ansible (SetupHostNetwork.yml, runs locally on the node).
#
# Addressing is owned by OpenTofu (opentofu/shared/shared-variables.tf); the
# Ansible inventory is generated from it. Management passwords are generated
# once into the encrypted vault and reused, so you never handle them.
# =============================================================================

INTERNET_FLAG = /run/worms/internet-allowed

# Ansible invocation. Inventory and the vault key come from ansible/ansible.cfg;
# -e @group_vars/vault.yml loads the (auto-decrypted) secrets. Run from ansible/.
ANSIBLE = ansible-playbook -e @group_vars/vault.yml

.PHONY: all setup init secrets apply inventory show-config provision \
        private_internet startround endround destroy destroy_all terminate

all: setup

# ---- Idempotent base setup --------------------------------------------------
setup: init secrets apply inventory show-config provision
	@echo "== setup complete =="
	@test ! -e $(INTERNET_FLAG) || echo "Internet access is still allowed: host/deny-internet.sh && make private_internet"

init:
	cd opentofu/stacks/platform && tofu init -input=false
	cd opentofu/stacks/infrastructure/persistent && tofu init -input=false
	cd opentofu/stacks/infrastructure/ephemeral && tofu init -input=false
	cd opentofu/stacks/sandbox && tofu init -input=false
	cd opentofu/stacks/private_internet && tofu init -input=false

# Generate management secrets into the encrypted vault (adds only missing keys).
secrets:
	python3 scripts/generate_secrets.py

# Base infrastructure stacks (NOT the per-round sandbox). The platform stack
# creates the bridges, VM firewall and hookscript; the host firewall is then
# configured from the generated inventory before any VM stack is applied.
apply:
	cd opentofu/stacks/platform && $(MAKE) apply
	$(MAKE) inventory
	cd ansible && $(ANSIBLE) SetupHostNetwork.yml
	cd opentofu/stacks/private_internet && $(MAKE) apply
	cd opentofu/stacks/infrastructure/persistent && $(MAKE) apply
	cd opentofu/stacks/infrastructure/ephemeral && $(MAKE) apply

# Regenerate the Ansible inventory from OpenTofu outputs (prints the config).
inventory:
	python3 scripts/generate_inventory.py

# Print the configured VM names + IPs from the already-generated inventory.
show-config:
	python3 scripts/generate_inventory.py --show

# Idempotent Ansible provisioning of the base infrastructure.
provision:
	@test -e $(INTERNET_FLAG) || echo "WARNING: internet access is denied; package installs will fail (host/allow-internet.sh)"
	cd ansible && $(ANSIBLE) SetupDNS.yml
	cd ansible && $(ANSIBLE) CreateCA.yml
	cd ansible && $(ANSIBLE) DistributeCA.yml
	cd ansible && $(ANSIBLE) ConfigureNginx.yml
	cd ansible && $(ANSIBLE) ConfigureNginxDjango.yml
	cd ansible && $(ANSIBLE) SetupDbAndDashboard.yml
	cd ansible && $(ANSIBLE) SetupSandboxApi.yml
	cd ansible && $(ANSIBLE) SetupPrivateInternet.yml

# Private-internet VMs + caches (online/offline follows the internet state).
private_internet:
	cd opentofu/stacks/private_internet && $(MAKE) apply
	$(MAKE) inventory
	cd ansible && $(ANSIBLE) SetupPrivateInternet.yml

# ---- Per-round sandbox ------------------------------------------------------
# Generate the sandbox config first (scripts/generate_sandbox.py), then:
startround:
	@test ! -e $(INTERNET_FLAG) || (echo "Internet access is allowed: run host/deny-internet.sh first" && false)
	cd opentofu/stacks/sandbox && $(MAKE) apply
	$(MAKE) inventory          # pick up the new sandbox VMs' IPs
	$(MAKE) show-config

endround:
	cd opentofu/stacks/sandbox && $(MAKE) destroy

# ---- Teardown ---------------------------------------------------------------
destroy_all:
	cd opentofu/stacks/sandbox && $(MAKE) destroy
	cd opentofu/stacks/infrastructure/ephemeral && $(MAKE) destroy
	cd opentofu/stacks/infrastructure/persistent && $(MAKE) destroy
	cd opentofu/stacks/platform && $(MAKE) destroy

destroy:
	cd opentofu/stacks/sandbox && $(MAKE) destroy
	cd opentofu/stacks/infrastructure/ephemeral && $(MAKE) destroy
	cd opentofu/stacks/infrastructure/persistent && $(MAKE) destroy
	cd opentofu/stacks/private_internet && $(MAKE) destroy

terminate:
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm stop {}
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm unlock {}
	$(MAKE) destroy_all
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm destroy {}
