#!/usr/bin/env bash
# =============================================================================
# worms-platform: DENY internet access again (the normal state).
#
#   host/deny-internet.sh         (as root, on the Proxmox node)
#
# Reverses host/allow-internet.sh, host side first (cuts access immediately):
#   1. empties the nftables gate set `internet_wan`,
#   2. removes all rules from the Proxmox security group `provisioning`,
#   3. verifies both, and only then removes /run/worms/internet-allowed, which
#      re-enables the sandbox stack.
#
# Idempotent. Afterwards, switch the caches to offline mode:
#   make private_internet
# =============================================================================
set -euo pipefail

FLAG=/run/worms/internet-allowed
GROUP=/cluster/firewall/groups/provisioning

die() { echo "deny-internet: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root on the Proxmox node"

nft flush set inet worms internet_wan 2>/dev/null || true

for pos in $(pvesh get "$GROUP" --output-format json | python3 -c "import json,sys; print(*sorted((r['pos'] for r in json.load(sys.stdin)), reverse=True))"); do
  pvesh delete "${GROUP}/${pos}"
done

if nft list set inet worms internet_wan 2>/dev/null | grep -q 'elements'; then
  die "nftables gate still open; NOT clearing ${FLAG}"
fi
remaining=$(pvesh get "$GROUP" --output-format json | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
[ "$remaining" -eq 0 ] || die "group provisioning still has ${remaining} rule(s); NOT clearing ${FLAG}"

rm -f "$FLAG"
echo "deny-internet: internet access DENIED. Run 'make private_internet' to switch the caches to offline mode."
