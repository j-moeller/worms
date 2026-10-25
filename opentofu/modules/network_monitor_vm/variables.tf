variable "proxmox_instance" {}

variable "name" {
  default = "monitor"
}

variable "user_data_file_id" {}
variable "disk_file_id" {}

variable "data_nic" {
  description = "net0: data plane (database, syslog), carries the default route."
  type = object({
    bridge  = string
    cidr    = string
    gateway = string
  })
}

variable "management_nic" {
  description = "net1: management (SSH from the host, Ansible)."
  type = object({
    bridge = string
    cidr   = string
  })
}

variable "capture_bridge" {
  description = "net2: the mirror bridge the host copies the sandbox traffic into. No address, no Proxmox firewall."
  type        = string
}

variable "firewall_rules" {
  description = "Security groups to apply, optionally limited to one NIC (iface = \"netN\")."
  type = list(object({
    group = string
    iface = optional(string)
  }))
}

variable "cpu" {
  type    = number
  default = 2
}

variable "ram" {
  type    = number
  default = 2048
}

# Ballooning floor (MB): the host may reclaim down to this. null = half the ceiling.
variable "ram_min" {
  type    = number
  default = null
}

variable "disk_size" {
  description = "Root disk size in GB (Zeek logs are kept locally for a while, see role network_monitor)."
  type        = number
  default     = 20
}

variable "tag" {
  default = "ephemeral"
}

variable "pool_id" {
  description = "Proxmox pool id (used for permission handling)"
}
