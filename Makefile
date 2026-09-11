IMAGES := \
	opentofu/images/debian-13-generic-amd64-with-guest-agent.qcow2 \
	opentofu/images/resolute-server-cloudimg-amd64-with-guest-agent.img \
	opentofu/images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0-with-guest-agent.qcow2

run: 
	make apply
#	make create_ca
#	make distribute_ca
#	make nginx_tls
#	make nginx_tls_flask
	make setup_db
#	make setup_logging
#	make setup_scoring
#	make setup_dashboard
#	make setup_dns

create_ca:
	cd ansible && ansible-playbook -i inventory.yml CreateCA.yml

distribute_ca:
	cd ansible && ansible-playbook -i inventory.yml DistributeCA.yml

nginx_tls:
	cd ansible && ansible-playbook -i inventory.yml ConfigureNginx.yml

nginx_tls_flask:
	cd ansible && ansible-playbook -i inventory.yml ConfigureNginxFlask.yml

setup_dns:
	cd ansible && ansible-playbook -i inventory.yml SetupDNS.yml

setup_db:
	cd ansible && ansible-playbook -i inventory.yml SetupDb.yml

setup_logging:
	cd ansible && ansible-playbook -i inventory.yml SetupLogging.yml

setup_scoring:
	cd ansible && ansible-playbook -i inventory.yml SetupScoring.yml

setup_dashboard:
	cd ansible && ansible-playbook -i inventory.yml SetupDashboard.yml

apply: $(IMAGES)
	cd opentofu && tofu validate
	@i=0; until (cd opentofu && tofu apply -auto-approve); do i=$$((i+1)); [ $$i -ge 10 ] && exit 1; done

destroy:
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm stop {}
	cd opentofu && tofu destroy -auto-approve
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm destroy {}

include opentofu/Makefile
