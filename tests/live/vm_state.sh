#!/bin/sh
# Read-only: what is on a devbox VM now, for comparing before and after a reset.
# Prints key=value lines, with the VM's token only as a hash.
#
#   sh vm_state.sh <apt package> <pip package>
APT_PACKAGE=$1
PIP_PACKAGE=$2
printf 'host=%s\n' "$(hostname)"
printf 'file=%s\n' "$(cat /root/worms-live-marker 2>/dev/null || echo absent)"
printf 'apt=%s\n' "$(command -v "$APT_PACKAGE" >/dev/null && echo present || echo absent)"
printf 'pip=%s\n' "$(python3 -c "import $PIP_PACKAGE" 2>/dev/null && echo present || echo absent)"
printf 'token=%s\n' "$(sha256sum /etc/worms/flag 2>/dev/null | cut -d' ' -f1)"
printf 'boot=%s\n' "$(uptime -s | tr ' ' T)"
printf 'host_key=%s\n' "$(sha256sum /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null | cut -d' ' -f1)"
