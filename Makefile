# Single switch for the private internet (propagated to OpenTofu -var and Ansible -e).
INTERNET_ENABLED ?= true

all: apply

startround:
	cd opentofu/stacks/sandbox && make apply

endround:
	cd opentofu/stacks/sandbox && make destroy

init:
	cd opentofu/stacks/platform && tofu init
	cd opentofu/stacks/infrastructure/persistent && tofu init
	cd opentofu/stacks/infrastructure/ephemeral && tofu init
	cd opentofu/stacks/sandbox && tofu init
	cd opentofu/stacks/private_internet && tofu init

apply:
	cd opentofu/stacks/platform && make apply INTERNET_ENABLED=$(INTERNET_ENABLED)
	cd opentofu/stacks/private_internet && make apply

	cd opentofu/stacks/platform && make apply
	cd opentofu/stacks/infrastructure/persistent && make apply
	cd opentofu/stacks/infrastructure/ephemeral && make apply
	cd opentofu/stacks/sandbox && make apply

##	cd ansible && ansible-playbook -i inventory.yml SetupDNS.yml
##	cd ansible && ansible-playbook -i inventory.yml CreateCA.yml
##	cd ansible && ansible-playbook -i inventory.yml DistributeCA.yml
##	cd ansible && ansible-playbook -i inventory.yml ConfigureNginx.yml
#	cd ansible && ansible-playbook -i inventory.yml ConfigureNginxFlask.yml
#	cd ansible && ansible-playbook -i inventory.yml ConfigureNginxDjango.yml
#	cd ansible && ansible-playbook -i inventory.yml SetupDbAndDashboard.yml
#	cd ansible && ansible-playbook -i inventory.yml SetupLogging.yml
#	cd ansible && ansible-playbook -i inventory.yml SetupScoring.yml

destroy_all:
	cd opentofu/stacks/sandbox && make destroy
	cd opentofu/stacks/infrastructure/ephemeral && make destroy
	cd opentofu/stacks/infrastructure/persistent && make destroy
	cd opentofu/stacks/platform && make destroy

destroy:
	cd opentofu/stacks/sandbox && make destroy
	cd opentofu/stacks/infrastructure/ephemeral && make destroy
	cd opentofu/stacks/infrastructure/persistent && make destroy
	cd opentofu/stacks/private_internet && make destroy

terminate:
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm stop {}
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm unlock {}
	make destroy_all
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm destroy {}

# INTERNET_ENABLED drives the whole private internet from one switch:
#   - platform stack: host egress seal (true = internet reachable, false = sealed)
#   - ansible:        caches online vs. offline mode
# The private_internet stack itself no longer depends on the switch (no egress VM).
private_internet:
	$(MAKE) -C opentofu/stacks/platform apply INTERNET_ENABLED=$(INTERNET_ENABLED)
	$(MAKE) -C opentofu/stacks/private_internet apply
	cd ansible && ansible-playbook -i inventory.yml SetupPrivateInternet.yml -e internet_enabled=$(INTERNET_ENABLED)
