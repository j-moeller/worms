resource "proxmox_network_linux_bridge" "vmbr_private_internet" {
  node_name = var.node_name
  name      = "vmbr1"

  address   = var.private_internet_cidr
  ports     = []
  autostart = true
}

resource "proxmox_network_linux_bridge" "vmbr_maintenance" {
  node_name = var.node_name
  name      = "vmbr2"

  address   = var.maintenance_cidr
  ports     = []
  autostart = true
  comment   = "Every VM (sandbox, infrastructure, ...) is connected to this network. The network is used to connect to VMs via SSH."
}

resource "proxmox_network_linux_bridge" "vmbr_monitoring" {
  node_name = var.node_name
  name      = "vmbr3"

  address   = var.monitoring_cidr
  ports     = []
  autostart = true
  comment   = "This network is a mirror of the network(s) the sandbox VMs are communicating over."
}

# Host-level egress policy for the private internet.
#
# vmbr1 has no physical uplink; its VMs only reach the WAN because the host
# forwards + masquerades out the WAN bridge. OpenTofu cannot express host
# iptables declaratively, so this installs a version-controlled, idempotent
# script + systemd unit (see files/) that toggles the egress:
#   internet_enabled = true  -> prep:    masquerade on, forwarding allowed
#   internet_enabled = false -> runtime: forwarding to the WAN dropped (sealed)
#
# Enforced in iptables-legacy, which is what actually governs routed traffic on
# this node (the nftables proxmox-firewall is empty/dormant and only filters
# firewall=1 NICs; ours are firewall=0). Runs on the Proxmox node (where tofu is
# applied, same assumption as the local state backend). Changing internet_enabled
# replaces this resource, re-running the provisioner. Covers both NICs of the
# private-internet VMs (private subnet + maintenance IP range).

resource "null_resource" "private_internet_egress" {
  triggers = {
    sealed      = var.internet_enabled ? "0" : "1"
    net         = var.private_internet_network
    wan         = var.wan_bridge
    maint_range = var.private_internet_maint_range
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -eu
      install -m 0755 '${path.module}/files/worms-private-internet-egress.sh' /usr/local/sbin/worms-private-internet-egress
      sed -i 's|@NET@|${var.private_internet_network}|; s|@WAN@|${var.wan_bridge}|; s|@MAINT_RANGE@|${var.private_internet_maint_range}|; s|@SEALED@|${var.internet_enabled ? "0" : "1"}|' /usr/local/sbin/worms-private-internet-egress
      install -m 0644 '${path.module}/files/worms-private-internet-egress.service' /etc/systemd/system/worms-private-internet-egress.service
      systemctl daemon-reload
      systemctl enable worms-private-internet-egress.service
      systemctl restart worms-private-internet-egress.service
    EOT
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      systemctl disable --now worms-private-internet-egress.service 2>/dev/null || true
      rm -f /etc/systemd/system/worms-private-internet-egress.service /usr/local/sbin/worms-private-internet-egress
      systemctl daemon-reload 2>/dev/null || true
      while iptables-legacy -C FORWARD -s '${self.triggers.net}' -o '${self.triggers.wan}' -j DROP 2>/dev/null; do iptables-legacy -D FORWARD -s '${self.triggers.net}' -o '${self.triggers.wan}' -j DROP; done
      while iptables-legacy -C FORWARD -m iprange --src-range '${self.triggers.maint_range}' -o '${self.triggers.wan}' -j DROP 2>/dev/null; do iptables-legacy -D FORWARD -m iprange --src-range '${self.triggers.maint_range}' -o '${self.triggers.wan}' -j DROP; done
    EOT
  }
}
