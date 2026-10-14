variable "node_name" {
  type = string
}

variable "private_internet_cidr" {
  type = string
}

variable "maintenance_cidr" {
  type = string
}

variable "sandbox_maintenance_cidr" {
  description = "Host address on the sandbox management bridge (vmbr4), e.g. 10.4.0.1/16."
  type        = string
}

variable "monitoring_cidr" {
  type = string
}
variable "internet_enabled" {
  description = "Whether the private internet may reach the WAN. true = prep phase (full internet); false = runtime (egress sealed in iptables-legacy). Keep in sync with the Ansible internet_enabled var."
  type        = bool
  default     = true
}

variable "private_internet_network" {
  description = "Private-internet subnet as a network CIDR (e.g. 10.2.0.0/24), used for the host egress rule."
  type        = string
}

variable "wan_bridge" {
  description = "Host bridge carrying the real internet uplink. Private-internet egress is masqueraded/sealed on this interface."
  type        = string
  default     = "vmbr0"
}

variable "private_internet_maint_range" {
  description = "IP range of the private-internet VMs' maintenance NICs (sealed too, since the maintenance subnet is NAT'd for the infra VMs)."
  type        = string
  default     = "10.3.0.2-10.3.0.8"
}
