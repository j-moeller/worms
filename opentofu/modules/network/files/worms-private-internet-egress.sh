#!/bin/sh
# Managed by OpenTofu (platform stack -> network module). DO NOT EDIT BY HAND.
#
# Egress control for the private internet, enforced in iptables-legacy -- the
# backend that actually governs this host's routed traffic. On this node the
# MASQUERADE for the VM subnets lives in iptables-legacy, the nftables
# proxmox-firewall ruleset is empty/dormant, and it would only filter NICs with
# firewall=1 (the private-internet VMs use firewall=0). So iptables-legacy is the
# only layer that reliably sees these packets. Re-applied at boot by the
# accompanying systemd unit, so the switch persists across reboots; nothing else
# manages iptables-legacy here, so it is not clobbered.
#
#   SEALED=0 -> prep:    masquerade present, forwarding allowed (full internet)
#   SEALED=1 -> runtime: forwarding to the WAN dropped for the private-internet VMs,
#               on BOTH their private subnet and their maintenance-NIC IPs.
# Intra-subnet traffic is bridged (not forwarded) and established returns are
# conntracked, so the caches keep serving while sealed.
set -eu

IPT="iptables-legacy"
NET="@NET@"
WAN="@WAN@"
MAINT_RANGE="@MAINT_RANGE@"
SEALED="@SEALED@"

# Ensure the private subnet is masqueraded when forwarding is allowed (idempotent).
$IPT -t nat -C POSTROUTING -s "$NET" -o "$WAN" -j MASQUERADE 2>/dev/null \
  || $IPT -t nat -A POSTROUTING -s "$NET" -o "$WAN" -j MASQUERADE

# Clear any previous seal (idempotent).
while $IPT -C FORWARD -s "$NET" -o "$WAN" -j DROP 2>/dev/null; do
  $IPT -D FORWARD -s "$NET" -o "$WAN" -j DROP
done
while $IPT -C FORWARD -m iprange --src-range "$MAINT_RANGE" -o "$WAN" -j DROP 2>/dev/null; do
  $IPT -D FORWARD -m iprange --src-range "$MAINT_RANGE" -o "$WAN" -j DROP
done

if [ "$SEALED" = "1" ]; then
  $IPT -I FORWARD 1 -s "$NET" -o "$WAN" -j DROP
  $IPT -I FORWARD 1 -m iprange --src-range "$MAINT_RANGE" -o "$WAN" -j DROP
fi
