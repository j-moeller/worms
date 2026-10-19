# =============================================================================
# worms-platform
#
#   make              Idempotent, end-to-end setup of the base infrastructure:
#                       init -> secrets -> platform -> firewall -> host-network
#                       -> [internet allowed] -> infrastructure -> configure
#                       -> [internet denied] -> offline
#   make firewall     Apply only the Proxmox firewall (security groups).
#   make host-network Host nftables/sysctls only (SetupHostNetwork.yml).
#   make provision    Ansible only (configure + offline), same internet window.
#   make show-config  Print the hosts Ansible will contact (from the OpenTofu
#                     state) and the VMs it skips.
#   make ping         One line per host: can Ansible reach it?
#   make debug HOST=<group|host>
#                     Step-by-step connection diagnostics (ansible/Debug.yml).
#   ARGS="..."        Extra ansible-playbook arguments, e.g.
#                     make provision ARGS="--limit database -vvv"
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
# ARGS is passed to every playbook (debugging), e.g.
#   make provision ARGS="--limit database -vvv"
ARGS ?=
ANSIBLE_BASE = ansible-playbook -e @group_vars/vault.yml
ANSIBLE = $(ANSIBLE_BASE) $(ARGS)

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

.PHONY: all init secrets platform firewall firewall-migrate host-network infrastructure \
        inventory show-config \
        provision configure offline internet-allow internet-deny ping debug \
        destroy destroy_all terminate

all: init secrets platform firewall host-network
	$(call online,infrastructure configure)
	$(MAKE) --no-print-directory offline show-config
	@echo "== setup complete (internet access denied) =="

provision:
	$(call online,configure)
	$(MAKE) --no-print-directory offline

init:
	cd opentofu/stacks/platform && tofu init -input=false
	cd opentofu/stacks/firewall && tofu init -input=false
	cd opentofu/stacks/infrastructure/persistent && tofu init -input=false
	cd opentofu/stacks/infrastructure/ephemeral && tofu init -input=false
	cd opentofu/stacks/sandbox && tofu init -input=false
	cd opentofu/stacks/private_internet && tofu init -input=false

# Generate management secrets into the encrypted vault (adds only missing keys).
secrets:
	python3 scripts/generate_secrets.py

# Bridges, images, cloud-init snippets, pools and hookscript.
platform: firewall-migrate
	cd opentofu/stacks/platform && $(MAKE) apply

# Proxmox firewall: cluster/node options and the security groups the VMs
# attach (incl. the empty `provisioning` group Internet.yml fills). Independent
# of the platform stack; every VM stack reads its `security_groups` output.
firewall: firewall-migrate
	cd opentofu/stacks/firewall && $(MAKE) apply

# Host firewall and routing (Ansible on the Proxmox node: nftables, sysctls,
# the node rule for the orchestrator). Needs only the platform addressing, so
# the inventory has no VMs yet; it is regenerated after `infrastructure`.
# Needed before internet can be allowed: the gate is the nft set internet_wan.
host-network:
	$(MAKE) --no-print-directory inventory
	cd ansible && $(ANSIBLE) SetupHostNetwork.yml

# One-time state migration for deployments made before the firewall stack:
# moves the firewall resources from the platform state into the firewall state
# (state only; backups are written next to both states). A moved state has no
# outputs yet, which the VM stacks read (also on destroy), so they are written
# with a refresh-only apply (changes nothing in Proxmox). No-op on fresh
# installs and once migrated.
firewall-migrate:
	@cd opentofu/stacks/firewall && \
	for r in $$(tofu -chdir=../platform state list 2>/dev/null | grep -E '^proxmox_(virtual_environment_cluster_firewall|node_firewall)'); do \
	  tofu state mv -state=../platform/terraform.tfstate -state-out=terraform.tfstate "$$r" "$$r" || exit 1; \
	done; \
	if [ -n "$$(tofu state list 2>/dev/null)" ] && ! tofu output -json security_groups >/dev/null 2>&1; then \
	  echo "firewall: writing outputs (refresh-only)"; \
	  tofu apply -refresh-only -auto-approve -input=false || exit 1; \
	fi

# Base infrastructure VMs (NOT the per-round sandbox). Internet access must be
# allowed: cloud-init installs packages at first boot. The inventory is
# regenerated afterwards, also when an apply fails, so it always matches the
# state (show-config then lists what was actually created).
infrastructure:
	@trap '$(MAKE) --no-print-directory -C $(CURDIR) inventory' EXIT; \
	set -e; \
	cd opentofu/stacks/private_internet && $(MAKE) apply; \
	cd $(CURDIR)/opentofu/stacks/infrastructure/persistent && $(MAKE) apply; \
	cd $(CURDIR)/opentofu/stacks/infrastructure/ephemeral && $(MAKE) apply

# Regenerate the Ansible inventory from the OpenTofu state: hosts = the VMs
# the stacks actually contain (prints them, and the VMs Ansible skips).
inventory:
	python3 scripts/generate_inventory.py

# Print the hosts of the already-generated inventory (no tofu call).
show-config:
	python3 scripts/generate_inventory.py --show

# Ansible configuration of all VMs (internet access allowed: package installs,
# caches in online mode populate themselves).
configure:
	$(MAKE) --no-print-directory inventory
	cd ansible && $(ANSIBLE) WaitForVMs.yml
	cd ansible && $(ANSIBLE) SetupDNS.yml
	cd ansible && $(ANSIBLE) CreateCA.yml
	cd ansible && $(ANSIBLE) DistributeCA.yml
	cd ansible && $(ANSIBLE) ConfigureNginx.yml
	cd ansible && $(ANSIBLE) ConfigureNginxDjango.yml
	cd ansible && $(ANSIBLE) SetupDbAndDashboard.yml
	cd ansible && $(ANSIBLE) SetupSandboxApi.yml
	cd ansible && $(ANSIBLE) SetupPrivateInternet.yml
	cd ansible && $(ANSIBLE) SetupAudit.yml

# Internet access denied: switch DNS and the private-internet caches to
# offline mode (both follow the host state, internet_enabled).
offline:
	@test ! -e $(INTERNET_FLAG) || (echo "Internet access is still allowed: make internet-deny" && false)
	cd ansible && $(ANSIBLE) SetupDNS.yml
	cd ansible && $(ANSIBLE) SetupPrivateInternet.yml

# ---- Debugging ----------------------------------------------------------------
ping:
	cd ansible && ansible 'all:!proxmox_host' -m ansible.builtin.ping -o

debug:
	@test -n "$(HOST)" || (echo 'usage: make debug HOST=<group or host>, e.g. HOST=database' && false)
	cd ansible && ansible-playbook Debug.yml -e target='$(HOST)' $(ARGS)

internet-allow:
	cd ansible && $(ANSIBLE_BASE) Internet.yml -e internet=allowed

internet-deny:
	cd ansible && $(ANSIBLE_BASE) Internet.yml -e internet=denied

# ---- Teardown ---------------------------------------------------------------
# VM stacks first (they reference the security groups, pools and images),
# then the firewall, then the platform.
destroy_all: destroy
	cd opentofu/stacks/firewall && $(MAKE) destroy
	cd opentofu/stacks/platform && $(MAKE) destroy

# The VM stacks read the firewall outputs even when destroying.
destroy: firewall-migrate
	cd opentofu/stacks/sandbox && $(MAKE) destroy
	cd opentofu/stacks/infrastructure/ephemeral && $(MAKE) destroy
	cd opentofu/stacks/infrastructure/persistent && $(MAKE) destroy
	cd opentofu/stacks/private_internet && $(MAKE) destroy

terminate:
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm stop {}
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm unlock {}
	$(MAKE) destroy_all
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm destroy {}
