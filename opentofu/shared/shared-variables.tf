variable "proxmox_instance" {
  description = "Name of the proxmox instance"
  type        = string
  default     = "worms-pve"
}

variable "proxmox_api_url" {
  description = "URL of the proxmox instance"
  type        = string
}

variable "proxmox_api_token" {
  description = "API token"
  type        = string
  sensitive   = true
}

variable "datastore_id" {
  description = "Id of the datastore"
  type        = string
  default     = "local"
}

variable "ssh_public_key" {
  description = "Existing public SSH key name"
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}

# =============================================================================
# SINGLE SOURCE OF TRUTH for all non-sandbox network addressing.
#
# This file is symlinked into every stack (opentofu/stacks/*/shared-variables.tf),
# so these locals are the one place where infrastructure IPs, subnets and the
# Proxmox host are defined. Every stack and the firewall consume them from here;
# the Ansible inventory is generated from the `infrastructure` output that the
# platform stack derives from these values (scripts/generate_inventory.py).
#
# Change an address here and it propagates to OpenTofu AND, after
# `make inventory`, to Ansible. Do not hardcode these addresses anywhere else.
#
# Sandbox VM addressing is NOT here: it is dynamic, owned entirely by
# opentofu/stacks/sandbox (generated_vms*.tf) and surfaced via its own output.
# =============================================================================
locals {
  networks = {
    sandbox_vms         = "10.1.0.0/24"
    private_internet    = "10.2.0.0/24"
    maintenance         = "10.3.0.0/24"
    # Sandbox VMs' management NICs live on their own bridge (vmbr4), never on
    # the infrastructure maintenance network: the only peer they share is the
    # Proxmox host. /16 so large generated rounds fit.
    sandbox_maintenance = "10.4.0.0/16"
  }

  # Static infrastructure VMs. `maintenance` is the address Ansible connects to.
  # `dns` additionally has a private-internet address (10.2.0.11) used by clients.
  infrastructure_ips = {
    dns          = "10.2.0.11"
    heartbeat    = "10.3.0.13"
    scoring      = "10.3.0.14"
    logging      = "10.3.0.15"
    db           = "10.3.0.16"
    dashboard    = "10.3.0.18"
    orchestrator = "10.3.0.19"
    monitor      = "10.3.0.100"
  }

  # DNS maintenance-network address (the VM has both a private-internet NIC at
  # infrastructure_ips.dns and this maintenance NIC that Ansible reaches it on).
  dns_maintenance_ip = "10.3.0.11"

  # Private-internet service VMs. Each has a private-internet NIC (what sandbox
  # VMs reach) and a maintenance NIC (what Ansible reaches). Defined once here;
  # the private_internet stack and the inventory both consume these.
  private_internet_ips = {
    apt_cache       = { network = "10.2.0.20", maintenance = "10.3.0.2" }
    devpi           = { network = "10.2.0.21", maintenance = "10.3.0.3" }
    docker_registry = { network = "10.2.0.22", maintenance = "10.3.0.4" }
    manpages        = { network = "10.2.0.23", maintenance = "10.3.0.5" }
    ntp             = { network = "10.2.0.24", maintenance = "10.3.0.6" }
    cve_search      = { network = "10.2.0.25", maintenance = "10.3.0.7" }
    wikipedia       = { network = "10.2.0.26", maintenance = "10.3.0.8" }
  }

  # Proxmox host, reachable by the orchestrator on the maintenance bridge
  # (vmbr2 gateway address). The Proxmox API listens on 8006.
  proxmox_host_ip  = "10.3.0.1"
  proxmox_api_port = 8006

  # Proxmox host address on the sandbox management bridge (vmbr4).
  sandbox_maintenance_host_ip = "10.4.0.1"

  # Addresses the host uses to SSH into VMs (Ansible). The `ssh` security group
  # accepts port 22 from these only.
  ssh_sources = [local.proxmox_host_ip, local.sandbox_maintenance_host_ip]
}