variable "proxmox_instance" {}

variable "name" {}
variable "user_data_file_id" {}
variable "disk_file_id" {}

variable "nics" {
  description = "NICs in order (net0, net1, ...). At most one should carry a gateway. The LAST NIC is the management NIC (SSH, Ansible)."
  type = list(object({
    bridge  = string
    cidr    = string
    gateway = optional(string)
  }))
}

variable "firewall_rules" {
  description = "Security groups to apply, optionally limited to one NIC (iface = \"netN\")."
  type = list(object({
    group = string
    iface = optional(string)
  }))
}

variable "hookscript_file_id" {
  description = "Proxmox hookscript (port isolation on management bridges)."
  type        = string
}

variable "cpu" {
  type    = number
  default = 2
}

variable "ram" {
  type    = number
  default = 1024
}

variable "tag" {
  default = "infrastructure"
}

variable "pool_id" {
  description = "Proxmox pool id (used for permission handling)"
}
