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

  node_name                = var.proxmox_instance
  private_internet_cidr    = "10.2.0.1/24"
  maintenance_cidr         = "10.3.0.1/24"
  sandbox_maintenance_cidr = "${local.sandbox_maintenance_host_ip}/${split("/", local.networks.sandbox_maintenance)[1]}"
  monitoring_cidr          = "10.250.0.1/24"
  internet_enabled         = var.internet_enabled
  private_internet_network = local.networks.private_internet
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

resource "proxmox_virtual_environment_cluster_firewall" "main" {
  enabled = true
}

resource "proxmox_node_firewall" "worms_pve" {
  node_name = var.proxmox_instance

  enabled  = true
  nftables = true
}

# SSH is only ever initiated by the Proxmox host (Ansible), on vmbr2 for the
# infrastructure and on vmbr4 for the sandbox VMs. No VM may SSH into another.
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

resource "proxmox_virtual_environment_cluster_firewall_security_group" "webserver" {
  name = "webserver"

  rule {
    type   = "in"
    action = "ACCEPT"
    proto  = "tcp"
    dport  = "443"
  }
}

resource "proxmox_virtual_environment_cluster_firewall_security_group" "private_internet" {
  name = "private_internet"
}

resource "proxmox_virtual_environment_cluster_firewall_security_group" "sandbox_rest_api" {
  name = "sandbox_rest_api"

  # Allow incoming HTTPS traffic
  rule {
    type   = "in"
    action = "ACCEPT"
    proto  = "tcp"
    dport  = "443"
  }

  # Allow outgoing PostgreSQL traffic to the database
  rule {
    type   = "out"
    action = "ACCEPT"
    proto  = "tcp"
    dport  = "5432"
    dest   = local.infrastructure_ips.db
  }
}

resource "proxmox_virtual_environment_cluster_firewall_security_group" "dashboard" {
  name = "dashboard"

  # Dashboard is reachable from the public internet (enabled later by adding an
  # inbound 443 rule here, or on the WAN-facing bridge). The egress rules below
  # are the least-privilege set the dashboard needs to function.

  # Allow incoming HTTPS traffic from the public internet (uncomment to expose).
  # rule {
  #   type   = "in"
  #   action = "ACCEPT"
  #   proto  = "tcp"
  #   dport  = "443"
  # }

  # Dashboard -> database (PostgreSQL, read-only least-privilege DB user).
  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "5432"
    dest    = local.infrastructure_ips.db
    comment = "Dashboard -> database:5432"
  }

  # Dashboard -> orchestrator (HTTPS control API).
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

  # Orchestrator exposes an HTTPS control API consumed by the dashboard only.
  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "443"
    source  = local.infrastructure_ips.dashboard
    comment = "Dashboard -> orchestrator:443"
  }

  # Orchestrator -> Proxmox API (proxmoxer).
  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = tostring(local.proxmox_api_port)
    dest    = local.proxmox_host_ip
    comment = "Orchestrator -> proxmox:8006"
  }

  # Orchestrator -> database (PostgreSQL, least-privilege DB user).
  rule {
    type    = "out"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "5432"
    dest    = local.infrastructure_ips.db
    comment = "Orchestrator -> database:5432"
  }
}

resource "proxmox_virtual_environment_cluster_firewall_security_group" "database" {
  name = "database"

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "5432"
    source  = local.infrastructure_ips.scoring
    comment = "Allow scoring"
  }

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "5432"
    source  = local.infrastructure_ips.logging
    comment = "Allow logging"
  }

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "5432"
    source  = local.infrastructure_ips.dashboard
    comment = "Allow dashboard"
  }

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "5432"
    source  = local.infrastructure_ips.orchestrator
    comment = "Allow orchestrator"
  }
}

resource "proxmox_virtual_environment_cluster_firewall_security_group" "dns" {
  name = "dns"

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "udp"
    dport   = "53"
    comment = "Allow incoming DNS over UDP"
  }

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "53"
    comment = "Allow incoming DNS over TCP"
  }
}