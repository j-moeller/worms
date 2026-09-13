IMAGES := \
	opentofu/images/debian-13-generic-amd64-with-guest-agent.qcow2 \
	opentofu/images/resolute-server-cloudimg-amd64-with-guest-agent.qcow2 \
	opentofu/images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0-with-guest-agent.qcow2

images:
	mkdir -p images

images/debian-13-generic-amd64.qcow2: images
	# test -e $@ || curl https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 --output $@
	test -e $@ || curl https://gemmei.ftp.acc.umu.se/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 --output $@

images/resolute-server-cloudimg-amd64.qcow2: images
	test -e $@ || curl https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.qcow2 --output $@

images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2: images
	test -e $@ || curl https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2 --output $@

images/debian-13-generic-amd64-with-guest-agent.qcow2: images/debian-13-generic-amd64.qcow2
	cp $< $@
	virt-customize -a $@ --install qemu-guest-agent --verbose
	chmod 444 $@

images/resolute-server-cloudimg-amd64-with-guest-agent.qcow2: images/resolute-server-cloudimg-amd64.qcow2
	cp $< $@
	virt-customize -a $@ --install qemu-guest-agent --verbose
	chmod 444 $@

images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0-with-guest-agent.qcow2: images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2
	cp $< $@
	virt-customize -a $@ --install qemu-guest-agent --verbose
	chmod 444 $@
