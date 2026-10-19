# Firewall stack: the Proxmox cluster/node firewall and every security group.
#
# Split from the platform stack so the rules can change (and be reviewed and
# applied) without touching bridges, images or cloud-init snippets. Depends on
# nothing but opentofu/shared/shared-variables.tf; the VM stacks read the group
# names from this stack's `security_groups` output.
#
# Apply order: platform -> firewall -> VM stacks (make platform firewall).

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

resource "proxmox_virtual_environment_cluster_firewall" "main" {
  enabled = true
}

resource "proxmox_node_firewall" "worms_pve" {
  node_name = var.proxmox_instance

  enabled  = true
  nftables = true
}

# =============================================================================
# Security groups. Every VM has input AND output policy DROP and ipfilter on;
# these groups are the complete list of what may flow. VMs attach them per NIC
# (iface = "netN"). The host nftables ruleset (ansible role host_network) enforces the
# same zone boundaries a second time for routed traffic.
# =============================================================================

# SSH is only ever initiated by the Proxmox host (Ansible), from
# its address on the management bridge. Attach on the management NIC only.
resource "proxmox_virtual_environment_cluster_firewall_security_group" "ssh" {
  name = "ssh"

  rule {
    type    = "in"
    action  = "ACCEPT"
    macro   = "SSH"
    source  = join(",", local.ssh_sources)
    comment = "SSH from the Proxmox host only"
  }
}

# Internet access for package installs / cache population. Every infrastructure
# VM attaches this group on its default-route NIC. It is EMPTY: only
# ansible/Internet.yml adds rules while internet access is allowed (and removes them),
# together with the host NAT, so OpenTofu ignores its rules.
resource "proxmox_virtual_environment_cluster_firewall_security_group" "provisioning" {
  name    = "provisioning"
  comment = "Managed by ansible/Internet.yml"

  lifecycle {
    ignore_changes = [rule]
  }
}

resource "proxmox_virtual_environment_cluster_firewall_security_group" "dashboard" {
  name = "dashboard"

  # Internet clients arrive via host DNAT (host_publish_dashboard, role host_network);
  # nothing else can route into the DMZ.
  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "443"
    comment = "HTTPS (public, via host DNAT)"
  }

  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "5432"
    dest    = local.infrastructure_ips.database
    comment = "Dashboard -> database:5432"
  }

  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "443"
    dest    = local.infrastructure_ips.orchestrator
    comment = "Dashboard -> orchestrator:443"
  }
}

resource "proxmox_virtual_environment_cluster_firewall_security_group" "orchestrator" {
  name = "orchestrator"

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "443"
    source  = local.infrastructure_ips.dashboard
    comment = "Dashboard -> orchestrator:443"
  }

  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = tostring(local.proxmox_api_port)
    dest    = local.proxmox_host_ip
    comment = "Orchestrator -> Proxmox API"
  }

  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "5432"
    dest    = local.infrastructure_ips.database
    comment = "Orchestrator -> database:5432"
  }
}

resource "proxmox_virtual_environment_cluster_firewall_security_group" "database" {
  name = "database"

  dynamic "rule" {
    for_each = ["dashboard", "orchestrator", "scoring", "logging", "monitor"]

    content {
      type    = "in"
      action  = "ACCEPT"
      proto   = "tcp"
      dport   = "5432"
      source  = local.infrastructure_ips[rule.value]
      comment = "Allow ${rule.value}"
    }
  }
}

# Database leg of scoring/logging (and later the monitor): nothing but 5432.
resource "proxmox_virtual_environment_cluster_firewall_security_group" "db_client" {
  name = "db_client"

  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "5432"
    dest    = local.infrastructure_ips.database
    comment = "-> database:5432"
  }
}

# Sandbox leg of scoring/logging. Sandbox traffic is SNATed by sandbox-gw, so
# its address is the only legitimate source.
resource "proxmox_virtual_environment_cluster_firewall_security_group" "sandbox_api" {
  name = "sandbox_api"

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "443"
    source  = local.services_ips.sandbox_gw
    comment = "Sandbox (via sandbox-gw) -> REST API"
  }
}

resource "proxmox_virtual_environment_cluster_firewall_security_group" "dns" {
  name = "dns"

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "udp"
    dport   = "53"
    source  = local.networks.services
    comment = "DNS over UDP from the services bridge"
  }

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "53"
    source  = local.networks.services
    comment = "DNS over TCP from the services bridge"
  }
}

# Private-internet services, reachable from the services bridge only.
resource "proxmox_virtual_environment_cluster_firewall_security_group" "private_internet" {
  name = "private_internet"

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = local.private_internet_ports.tcp
    source  = local.networks.services
    comment = "Private-internet services (TCP)"
  }

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "udp"
    dport   = local.private_internet_ports.udp
    source  = local.networks.services
    comment = "Private-internet services (UDP)"
  }
}

# Clients on the services bridge (private-internet VMs using apt-cache/DNS,
# sandbox-gw on its services leg).
resource "proxmox_virtual_environment_cluster_firewall_security_group" "services_client" {
  name = "services_client"

  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = local.private_internet_ports.tcp
    dest    = local.networks.services
    comment = "-> services bridge (TCP)"
  }

  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "udp"
    dport   = local.private_internet_ports.udp
    dest    = local.networks.services
    comment = "-> services bridge (UDP)"
  }
}

# Sandbox VMs (assumed root-compromised). Free traffic among the generated
# networks (that is the experiment), plus the services bridge on the allowed
# ports. Nothing else exists from their point of view.
resource "proxmox_virtual_environment_cluster_firewall_security_group" "sandbox" {
  name = "sandbox"

  rule {
    type    = "in"
    action  = "ACCEPT"
    source  = local.networks.sandbox_vms
    dest    = local.networks.sandbox_vms
    comment = "Within the sandbox networks"
  }

  rule {
    type    = "out"
    action  = "ACCEPT"
    source  = local.networks.sandbox_vms
    dest    = local.networks.sandbox_vms
    comment = "Within the sandbox networks"
  }

  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = local.private_internet_ports.tcp
    dest    = local.networks.services
    comment = "-> services bridge (TCP, via sandbox-gw)"
  }

  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "udp"
    dport   = local.private_internet_ports.udp
    dest    = local.networks.services
    comment = "-> services bridge (UDP, via sandbox-gw)"
  }
}

# sandbox-gw, sandbox legs: routes between the sandbox networks and towards the
# services bridge. The router itself is assumed compromised; these rules (on the
# host side of its NICs) are what bound it.
resource "proxmox_virtual_environment_cluster_firewall_security_group" "sandbox_gw" {
  name = "sandbox_gw"

  rule {
    type    = "in"
    action  = "ACCEPT"
    source  = local.networks.sandbox_vms
    dest    = local.networks.sandbox_vms
    comment = "Route between sandbox networks"
  }

  rule {
    type    = "out"
    action  = "ACCEPT"
    source  = local.networks.sandbox_vms
    dest    = local.networks.sandbox_vms
    comment = "Route between sandbox networks"
  }

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = local.private_internet_ports.tcp
    source  = local.networks.sandbox_vms
    dest    = local.networks.services
    comment = "Sandbox -> services bridge (TCP)"
  }

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "udp"
    dport   = local.private_internet_ports.udp
    source  = local.networks.sandbox_vms
    dest    = local.networks.services
    comment = "Sandbox -> services bridge (UDP)"
  }
}

# Central log server (wazuh, siem bridge): syslog from the infrastructure VMs
# (routed by the host), the dashboard from the host only (SSH tunnel).
resource "proxmox_virtual_environment_cluster_firewall_security_group" "wazuh" {
  name = "wazuh"

  dynamic "rule" {
    for_each = ["dmz", "core", "ingest", "mgmt_exposed"]

    content {
      type    = "in"
      action  = "ACCEPT"
      proto   = "tcp"
      dport   = "514"
      source  = local.networks[rule.value]
      comment = "Syslog from ${rule.value}"
    }
  }

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "443"
    source  = local.host_ips.siem
    comment = "Dashboard from the Proxmox host (SSH tunnel)"
  }
}

# Every infrastructure VM ships its audit log to wazuh. Attach on the
# default-route NIC.
resource "proxmox_virtual_environment_cluster_firewall_security_group" "log_client" {
  name = "log_client"

  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "514"
    dest    = local.infrastructure_ips.wazuh
    comment = "-> wazuh:514 (syslog)"
  }
}
