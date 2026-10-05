variable "proxmox_instance" {}
variable "vmbr_maintenance_network" {}

variable "name" {}
variable "maintenance_network_cidr" {}
variable "maintenance_network_gateway" {}
variable "user_data_file_id" {}
variable "disk_file_id" {}

variable "tag" {
  default = "infrastructure"
}

variable "pool_id" {
  description = "Proxmox pool id (used for permission handling)"
}