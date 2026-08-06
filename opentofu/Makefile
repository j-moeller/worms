opentofu/images:
	mkdir -p opentofu/images

opentofu/images/debian-13-generic-amd64.qcow2: opentofu/images
	# test -e $@ || curl https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 --output $@
	test -e $@ || curl https://gemmei.ftp.acc.umu.se/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 --output $@

opentofu/images/resolute-server-cloudimg-amd64.img: opentofu/images
	test -e $@ || curl https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img --output $@

opentofu/images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2: opentofu/images
	test -e $@ || curl https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2 --output $@

opentofu/images/debian-13-generic-amd64-with-guest-agent.qcow2: opentofu/images/debian-13-generic-amd64.qcow2
	cp $< $@
	virt-customize -a $@ --install qemu-guest-agent --verbose
	chmod 444 $@

opentofu/images/resolute-server-cloudimg-amd64-with-guest-agent.img: opentofu/images/resolute-server-cloudimg-amd64.img
	cp $< $@
	virt-customize -a $@ --install qemu-guest-agent --verbose
	chmod 444 $@

opentofu/images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0-with-guest-agent.qcow2: opentofu/images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2
	cp $< $@
	virt-customize -a $@ --install qemu-guest-agent --verbose
	chmod 444 $@
