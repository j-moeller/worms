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

variable "ssh_public_key_name" {
  description = "Existing public SSH key name"
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}
