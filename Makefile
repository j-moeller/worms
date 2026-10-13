# =============================================================================
# worms-platform
#
#   make            Idempotent, end-to-end setup of the base infrastructure:
#                     init -> secrets -> tofu apply -> inventory -> provision.
#                   Re-running it changes nothing once everything is in place.
#   make show-config  Print the VM names + IP addresses that are configured.
#   make startround   Bring up the per-round sandbox (generate it first).
#   make endround     Tear the sandbox back down.
#
# Addressing is owned by OpenTofu (opentofu/shared/shared-variables.tf); the
# Ansible inventory is generated from it. Management passwords are generated
# once into the encrypted vault and reused, so you never handle them.
# =============================================================================

# Single switch for the private internet (propagated to OpenTofu -var and Ansible -e).
INTERNET_ENABLED ?= true

# Ansible invocation. Inventory and the vault key come from ansible/ansible.cfg;
# -e @group_vars/vault.yml loads the (auto-decrypted) secrets. Run from ansible/.
ANSIBLE = ansible-playbook -e @group_vars/vault.yml

.PHONY: all setup init secrets apply inventory show-config provision \
        startround endround destroy destroy_all terminate private_internet

all: setup

# ---- Idempotent base setup --------------------------------------------------
# Each step is safe to re-run: tofu converges, secrets are only generated once,
# the inventory is regenerated deterministically, and the Ansible roles are
# idempotent. So `make` can be run repeatedly and only drifts the system toward
# the declared state.
setup: init secrets apply inventory show-config provision
	@echo "== setup complete =="

init:
	cd opentofu/stacks/platform && tofu init -input=false
	cd opentofu/stacks/infrastructure/persistent && tofu init -input=false
	cd opentofu/stacks/infrastructure/ephemeral && tofu init -input=false
	cd opentofu/stacks/sandbox && tofu init -input=false
	cd opentofu/stacks/private_internet && tofu init -input=false

# Generate management secrets ONCE into the encrypted vault (no-op if present).
secrets:
	python3 scripts/generate_secrets.py

# Apply the base infrastructure stacks (NOT the per-round sandbox).
apply:
	cd opentofu/stacks/platform && $(MAKE) apply INTERNET_ENABLED=$(INTERNET_ENABLED)
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
	cd ansible && $(ANSIBLE) SetupDNS.yml
	cd ansible && $(ANSIBLE) CreateCA.yml
	cd ansible && $(ANSIBLE) DistributeCA.yml
	cd ansible && $(ANSIBLE) ConfigureNginx.yml
	cd ansible && $(ANSIBLE) ConfigureNginxFlask.yml
	cd ansible && $(ANSIBLE) ConfigureNginxDjango.yml
	cd ansible && $(ANSIBLE) SetupDbAndDashboard.yml
	cd ansible && $(ANSIBLE) SetupLogging.yml
	cd ansible && $(ANSIBLE) SetupScoring.yml
	cd ansible && $(ANSIBLE) SetupPrivateInternet.yml -e internet_enabled=$(INTERNET_ENABLED)

# ---- Per-round sandbox ------------------------------------------------------
# Generate the sandbox config first (scripts/generate_sandbox.py), then:
startround:
	cd opentofu/stacks/sandbox && $(MAKE) apply
	$(MAKE) inventory          # pick up the new sandbox VMs' IPs
	$(MAKE) show-config
	# Provision the sandboxes (certificates, /etc/flags for the CTF, ...), e.g.:
	#   cd ansible && $(ANSIBLE) SetupSandbox.yml
	# then deliver files via the orchestrator.

endround:
	cd opentofu/stacks/sandbox && $(MAKE) destroy

# INTERNET_ENABLED drives the whole private internet from one switch:
#   - platform stack: host egress seal (true = internet reachable, false = sealed)
#   - ansible:        caches online vs. offline mode
private_internet:
	$(MAKE) -C opentofu/stacks/platform apply INTERNET_ENABLED=$(INTERNET_ENABLED)
	$(MAKE) -C opentofu/stacks/private_internet apply
	$(MAKE) inventory
	cd ansible && $(ANSIBLE) SetupPrivateInternet.yml -e internet_enabled=$(INTERNET_ENABLED)

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
