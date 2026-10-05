terraform {
  required_providers {
    proxmox = {
      source = "bpg/proxmox"
    }
  }
}

provider "proxmox" {
  endpoint  = var.proxmox_api_url
  api_token = var.proxmox_api_token
  insecure  = true

  ssh {
    agent    = true
    username = "root"
  }
}

module "os_images" {
  source = "../../modules/os_images"

  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance
}


module "cloud_init" {
  source = "../../modules/cloud_init"

  datastore_id   = var.datastore_id
  node_name      = var.proxmox_instance
  ssh_public_key = var.ssh_public_key
}


module "networks" {
  source = "../../modules/network"

  node_name             = var.proxmox_instance
  private_internet_cidr = "10.2.0.1/24"
  maintenance_cidr      = "10.3.0.1/24"
  monitoring_cidr       = "10.250.0.1/24"
}

# Platform-layer egress control for the private internet.
#
# Expressed as a proxmox-firewall FORWARD rule (PVE 9 / nftables firewall) so it
# PERSISTS: it lives in /etc/pve/firewall/cluster.fw, survives reboots and firewall
# reloads, and is ordered deterministically by the firewall itself (unlike a raw
# iptables rule, which the firewall would flush/reorder on its next reload).
#
# internet_enabled = false -> rule active: everything the private-internet subnet
#   tries to forward out (i.e. to the internet) is dropped. Intra-subnet traffic is
#   bridged (not forwarded) and established return traffic is allowed by conntrack,
#   so the caches keep serving sandbox/infra clients.
# internet_enabled = true  -> rule disabled: the subnet reaches the internet via the
#   host masquerade (prep phase, for populating the caches).
#
# Both NICs of the private-internet VMs are sealed: the private subnet below, and
# their maintenance-NIC addresses via the IPSet (the maintenance subnet is NAT'd
# for the infra VMs, so a private-internet VM could otherwise egress that way).
# NOTE: the masquerade that grants prep-phase internet is still a host-level rule
# outside this config — see docs for that remaining follow-up.
resource "proxmox_virtual_environment_firewall_ipset" "private_internet_maint" {
  name    = "worms-pi-maint"
  comment = "Private-internet VMs' maintenance IPs (managed by OpenTofu)"

  dynamic "cidr" {
    for_each = toset([for ip in range(2, 9) : "10.3.0.${ip}"]) # 10.3.0.2 .. 10.3.0.8
    content {
      name = cidr.value
    }
  }
}

resource "proxmox_virtual_environment_firewall_rules" "private_internet_egress" {
  # private NIC: drop the whole private subnet's egress
  rule {
    type    = "forward"
    action  = "DROP"
    source  = local.networks.private_internet
    enabled = !var.internet_enabled
    comment = "WORMS: seal private-internet egress when internet_enabled=false (managed by OpenTofu)"
  }

  # maintenance NIC: drop egress from just the private-internet VMs' maintenance IPs
  rule {
    type    = "forward"
    action  = "DROP"
    source  = "+${proxmox_virtual_environment_firewall_ipset.private_internet_maint.name}"
    enabled = !var.internet_enabled
    comment = "WORMS: seal private-internet maintenance-NIC egress when internet_enabled=false (managed by OpenTofu)"
  }
}

resource "proxmox_virtual_environment_pool" "pools" {
  for_each = toset([
    "ephemeral",
    "persistent",
    "sandbox",
    "devbox",
    "private_internet",
  ])

  pool_id = each.value
}
