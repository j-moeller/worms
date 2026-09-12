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

locals {
  networks = {
    sandbox_vms      = "10.1.0.0/24"
    private_internet = "10.2.0.0/24"
    maintenance      = "10.3.0.0/24"
  }

  infrastructure_ips = {
    dns       = "10.2.0.11"
    heartbeat = "10.3.0.13"
    scoring   = "10.3.0.14"
    logging   = "10.3.0.15"
    db        = "10.3.0.16"
    dashboard = "10.3.0.18"
    monitor   = "10.3.0.100"
  }
}