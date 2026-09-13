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

apply:
	cd opentofu/stacks/platform && make apply
	cd opentofu/stacks/infrastructure/persistent && make apply
	cd opentofu/stacks/infrastructure/ephemeral && make apply
#	cd opentofu/stacks/sandbox && make apply

##	cd ansible && ansible-playbook -i inventory.yml SetupDNS.yml
##	cd ansible && ansible-playbook -i inventory.yml CreateCA.yml
##	cd ansible && ansible-playbook -i inventory.yml DistributeCA.yml
##	cd ansible && ansible-playbook -i inventory.yml ConfigureNginx.yml
#	cd ansible && ansible-playbook -i inventory.yml ConfigureNginxFlask.yml
	cd ansible && ansible-playbook -i inventory.yml ConfigureNginxDjango.yml
	cd ansible && ansible-playbook -i inventory.yml SetupDbAndDashboard.yml
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

terminate:
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm stop {}
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm unlock {}
	make destroy_all
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm destroy {}
