IMAGES := \
	opentofu/images/debian-13-generic-amd64-with-guest-agent.qcow2 \
	opentofu/images/resolute-server-cloudimg-amd64-with-guest-agent.img \
	opentofu/images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0-with-guest-agent.qcow2

apply: $(IMAGES)
	cd opentofu && tofu validate
	@i=0; until (cd opentofu && tofu apply -auto-approve); do i=$$((i+1)); [ $$i -ge 10 ] && exit 1; done
	cd ansible && ansible-playbook -i inventory.ini SetupDb.yml
	cd ansible && ansible-playbook -i inventory.ini SetupLogging.yml
	cd ansible && ansible-playbook -i inventory.ini SetupScoring.yml

destroy:
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm stop {}
	cd opentofu && tofu destroy -auto-approve
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm destroy {}

include opentofu/Makefile
