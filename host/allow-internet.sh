#!/usr/bin/env bash
# =============================================================================
# worms-platform: ALLOW internet access for the infrastructure VMs.
#
#   host/allow-internet.sh        (as root, on the Proxmox node)
#
# For package installs and populating the private-internet caches. Opens
# HTTP(S) and DNS for the VMs in the dmz, core and ingest zones and on the
# mgmt-exposed bridge. Never for the sandbox, services or mirror bridges.
#
# Opens two layers, both runtime state only:
#   * Proxmox firewall: rules in the security group `provisioning`, which every
#     infrastructure VM attaches on its default-route NIC (empty otherwise).
#   * Host nftables: the WAN interface is added to the set `internet_wan`,
#     which gates the forward/masquerade rules (ansible role host_network).
#     A reboot or ruleset reload empties it.
#
# Safety: refused while any VM is in the sandbox pool. While allowed,
# /run/worms/internet-allowed exists and the sandbox stack (and
# `make startround`) refuse to deploy; it is written before anything is opened.
#
# Revoke with host/deny-internet.sh.
# =============================================================================
set -euo pipefail

FLAG=/run/worms/internet-allowed
GROUP=/cluster/firewall/groups/provisioning

die() { echo "allow-internet: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root on the Proxmox node"
[ -r /etc/worms/host.env ] || die "host network not configured (ansible-playbook SetupHostNetwork.yml)"
# shellcheck source=/dev/null
. /etc/worms/host.env
nft list set inet worms internet_wan >/dev/null 2>&1 || die "host firewall not loaded (systemctl status worms-host-firewall)"

sandbox_vms=$(pvesh get /cluster/resources --type vm --output-format json \
  | python3 -c "import json,sys; print(sum(1 for r in json.load(sys.stdin) if r.get('pool') == 'sandbox'))")
[ "$sandbox_vms" -eq 0 ] || die "refusing: ${sandbox_vms} VM(s) in the sandbox pool (make endround first)"

# 1. Flag first: from now on no sandbox can be deployed.
mkdir -p "$(dirname "$FLAG")"
date -u +%FT%TZ >"$FLAG"

# 2. Proxmox firewall (VM egress): replace whatever the group contains.
for pos in $(pvesh get "$GROUP" --output-format json | python3 -c "import json,sys; print(*sorted((r['pos'] for r in json.load(sys.stdin)), reverse=True))"); do
  pvesh delete "${GROUP}/${pos}"
done
pvesh create "$GROUP" --type out --action ACCEPT --proto tcp --dport 80,443 --enable 1 --comment "internet allowed: HTTP(S)"
pvesh create "$GROUP" --type out --action ACCEPT --proto udp --dport 53 --enable 1 --comment "internet allowed: DNS"
pvesh create "$GROUP" --type out --action ACCEPT --proto tcp --dport 53 --enable 1 --comment "internet allowed: DNS"

# 3. Host forwarding + NAT.
nft add element inet worms internet_wan "{ \"${WAN_IF}\" }"

echo "allow-internet: internet access ALLOWED via ${WAN_IF}. Revoke with host/deny-internet.sh."
