# =============================================================================
# worms-platform
#
#   make              Idempotent, end-to-end setup of the base infrastructure:
#                       init -> secrets -> platform -> [internet allowed]
#                       -> infrastructure -> configure -> [internet denied]
#                       -> offline
#   make provision    Ansible only (configure + offline), same internet window.
#   make show-config  Print the VM names + IP addresses that are configured.
#   make internet-allow / internet-deny
#                     Toggle internet access by hand (Ansible, Internet.yml).
#
# Internet access for VMs is never implicit. It is allowed only for the
# duration of provisioning (cloud-init and Ansible install packages, the
# private-internet caches are populated) and always denied again afterwards,
# also when a step fails or the run is interrupted. Allowing is refused while
# sandbox VMs exist; while it is allowed, the sandbox stack refuses to deploy.
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

# $(call online,<targets>): run the make targets with internet access allowed,
# then deny it again, whatever happens (success, failure, Ctrl-C). A failed
# deny fails the run loudly.
define online
	@trap '$(MAKE) --no-print-directory internet-deny || { echo "!!! INTERNET ACCESS IS STILL ALLOWED: run make internet-deny" >&2; exit 1; }' EXIT; \
	trap 'exit 130' INT TERM HUP; \
	set -e; \
	$(MAKE) --no-print-directory internet-allow; \
	$(MAKE) --no-print-directory $(1)
endef

.PHONY: all init secrets platform infrastructure inventory show-config \
        provision configure offline internet-allow internet-deny \
        destroy destroy_all terminate

all: init secrets platform
	$(call online,infrastructure configure)
	$(MAKE) --no-print-directory offline show-config
	@echo "== setup complete (internet access denied) =="

provision:
	$(call online,configure)
	$(MAKE) --no-print-directory offline

init:
	cd opentofu/stacks/platform && tofu init -input=false
	cd opentofu/stacks/infrastructure/persistent && tofu init -input=false
	cd opentofu/stacks/infrastructure/ephemeral && tofu init -input=false
	cd opentofu/stacks/sandbox && tofu init -input=false
	cd opentofu/stacks/private_internet && tofu init -input=false

# Generate management secrets into the encrypted vault (adds only missing keys).
secrets:
	python3 scripts/generate_secrets.py

# Bridges, VM firewall and hookscript (OpenTofu), then the host firewall
# (Ansible, from the generated inventory). Needed before internet can be
# allowed: the gate lives in the host firewall and the provisioning group.
platform:
	cd opentofu/stacks/platform && $(MAKE) apply
	$(MAKE) --no-print-directory inventory
	cd ansible && $(ANSIBLE) SetupHostNetwork.yml

# Base infrastructure VMs (NOT the per-round sandbox). Internet access must be
# allowed: cloud-init installs packages at first boot.
infrastructure:
	cd opentofu/stacks/private_internet && $(MAKE) apply
	cd opentofu/stacks/infrastructure/persistent && $(MAKE) apply
	cd opentofu/stacks/infrastructure/ephemeral && $(MAKE) apply
	$(MAKE) --no-print-directory inventory

# Regenerate the Ansible inventory from OpenTofu outputs (prints the config).
inventory:
	python3 scripts/generate_inventory.py

# Print the configured VM names + IPs from the already-generated inventory.
show-config:
	python3 scripts/generate_inventory.py --show

# Ansible configuration of all VMs (internet access allowed: package installs,
# caches in online mode populate themselves).
configure:
	cd ansible && $(ANSIBLE) WaitForVMs.yml
	cd ansible && $(ANSIBLE) SetupDNS.yml
	cd ansible && $(ANSIBLE) CreateCA.yml
	cd ansible && $(ANSIBLE) DistributeCA.yml
	cd ansible && $(ANSIBLE) ConfigureNginx.yml
	cd ansible && $(ANSIBLE) ConfigureNginxDjango.yml
	cd ansible && $(ANSIBLE) SetupDbAndDashboard.yml
	cd ansible && $(ANSIBLE) SetupSandboxApi.yml
	cd ansible && $(ANSIBLE) SetupPrivateInternet.yml

# Internet access denied: switch DNS and the private-internet caches to
# offline mode (both follow the host state, internet_enabled).
offline:
	@test ! -e $(INTERNET_FLAG) || (echo "Internet access is still allowed: make internet-deny" && false)
	cd ansible && $(ANSIBLE) SetupDNS.yml
	cd ansible && $(ANSIBLE) SetupPrivateInternet.yml

internet-allow:
	cd ansible && $(ANSIBLE) Internet.yml -e internet=allowed

internet-deny:
	cd ansible && $(ANSIBLE) Internet.yml -e internet=denied

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
