IMAGES := \
	images/debian-13-generic-amd64-with-guest-agent.qcow2 \
	images/resolute-server-cloudimg-amd64-with-guest-agent.img \
	images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0-with-guest-agent.qcow2

apply: $(IMAGES)
	tofu validate
	@i=0; until tofu apply -auto-approve; do i=$$((i+1)); [ $$i -ge 10 ] && exit 1; done

destroy:
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm stop {}
	tofu destroy -auto-approve
	qm list | awk '{print $$1}' | tail -n +2 | xargs -I {} qm destroy {}

images:
	mkdir -p images

images/debian-13-generic-amd64.qcow2: images
	# test -e $@ || curl https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 --output $@
	test -e $@ || curl https://gemmei.ftp.acc.umu.se/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 --output $@

images/resolute-server-cloudimg-amd64.img: images
	test -e $@ || curl https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img --output $@

images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2: images
	test -e $@ || curl https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2 --output $@

images/debian-13-generic-amd64-with-guest-agent.qcow2: images/debian-13-generic-amd64.qcow2
	cp $< $@
	virt-customize -a $@ --install qemu-guest-agent --verbose
	chmod 444 $@

images/resolute-server-cloudimg-amd64-with-guest-agent.img: images/resolute-server-cloudimg-amd64.img
	cp $< $@
	virt-customize -a $@ --install qemu-guest-agent --verbose
	chmod 444 $@

images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0-with-guest-agent.qcow2: images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2
	cp $< $@
	virt-customize -a $@ --install qemu-guest-agent --verbose
	chmod 444 $@
