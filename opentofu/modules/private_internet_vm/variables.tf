variable "name" {}
variable "proxmox_instance" {}
variable "cpu" {}
variable "ram" {}
variable "network_cidr" {}
variable "maintenance_network_gateway" {}
variable "maintenance_network_cidr" {}
variable "user_data_file_id" {}
variable "vmbr_network" {}
variable "vmbr_maintenance_network" {}
variable "disk_file_id" {}

variable "datastore_id" {
  description = "Proxmox datastore for the VM disks"
  type        = string
  default     = "local-zfs"
}

variable "disk_size" {
  description = "Root/OS disk size in GB"
  type        = number
  default     = 5
}

variable "data_disk_size" {
  description = "Optional dedicated data disk size in GB (0 = no data disk)"
  type        = number
  default     = 0
}

variable "cpu_type" {
  description = "Proxmox CPU type (e.g. \"host\" for AVX support; null = provider default)"
  type        = string
  default     = null
}

variable "pool_id" {
  description = "Proxmox pool id (used for permission handling)"
}

variable "firewall_rules" {
  description = "Security groups to apply, optionally limited to one NIC (net0 = services, net1 = management)."
  type = list(object({
    group = string
    iface = optional(string)
  }))
}

variable "hookscript_file_id" {
  description = "Proxmox hookscript (port isolation on management bridges)."
  type        = string
}