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

resource "proxmox_virtual_environment_cluster_firewall_security_group" "ssh" {
  name = "ssh"

  rule {
    type   = "in"
    action = "ACCEPT"
    macro  = "SSH"
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
    dest   = "10.3.0.16"
  }
}

resource "proxmox_virtual_environment_cluster_firewall_security_group" "dashboard" {
  name = "dashboard"

  # Allow incoming HTTP traffic from a specific IP
  # rule {
  #   type   = "in"
  #   action = "ACCEPT"
  #   proto  = "tcp"
  #   dport  = "80"
  #   source = "REPLACE_WITH_ALLOWED_IP"
  # }

  # Allow incoming HTTPS traffic from a specific IP
  # rule {
  #   type   = "in"
  #   action = "ACCEPT"
  #   proto  = "tcp"
  #   dport  = "443"
  #   source = "REPLACE_WITH_ALLOWED_IP"
  # }

  # Allow outgoing PostgreSQL traffic to the database
  rule {
    type   = "out"
    action = "ACCEPT"
    proto  = "tcp"
    dport  = "5432"
    dest   = "10.3.0.16"
  }
}

resource "proxmox_virtual_environment_cluster_firewall_security_group" "database" {
  name = "database"

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "5432"
    source  = "10.3.0.14"
    comment = "Allow scoring"
  }

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "5432"
    source  = "10.3.0.15"
    comment = "Allow logging"
  }

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "5432"
    source  = "10.3.0.18"
    comment = "Allow dashboard"
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