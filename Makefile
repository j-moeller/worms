IMAGES := \
	opentofu/images/debian-13-generic-amd64-with-guest-agent.qcow2 \
	opentofu/images/resolute-server-cloudimg-amd64-with-guest-agent.img \
	opentofu/images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0-with-guest-agent.qcow2

all: apply

startround:
	cd opentofu/stacks/sandbox && make apply

endround:
	cd opentofu/stacks/sandbox && make destroy

apply:
	cd opentofu/stacks/platform && make apply
	cd opentofu/stacks/infrastructure/persistent && make apply
	cd opentofu/stacks/infrastructure/ephemeral && make apply
	cd opentofu/stacks/sandbox && make apply

	cd ansible && ansible-playbook -i inventory.yml CreateCA.yml
	cd ansible && ansible-playbook -i inventory.yml DistributeCA.yml
	cd ansible && ansible-playbook -i inventory.yml ConfigureNginx.yml
	cd ansible && ansible-playbook -i inventory.yml ConfigureNginxFlask.yml
	cd ansible && ansible-playbook -i inventory.yml SetupDNS.yml
	cd ansible && ansible-playbook -i inventory.yml SetupDb.yml
	cd ansible && ansible-playbook -i inventory.yml SetupLogging.yml
	cd ansible && ansible-playbook -i inventory.yml SetupScoring.yml
	cd ansible && ansible-playbook -i inventory.yml SetupDashboard.yml

destroy:
	cd opentofu/stacks/sandbox && make destroy
	cd opentofu/stacks/infrastructure/ephemeral && make destroy
	cd opentofu/stacks/infrastructure/persistent && make destroy
	cd opentofu/stacks/platform && make destroy

#destroy:
#	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm stop {}
#	cd opentofu && tofu destroy -auto-approve
#	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm destroy {}
