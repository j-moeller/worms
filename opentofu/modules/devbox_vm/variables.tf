variable "proxmox_instance" {}
variable "name" {}
variable "datastore" {}
variable "disk_file_id" {}
variable "user_data_file_id" {}
variable "pool_id" {}
variable "dns_server" {}

# A devbox VM or gateway is small (nftables router / a team's playground).
variable "ram" {
  type    = number
  default = 512
}

# Ballooning floor (MB): the host may reclaim down to this under pressure.
variable "ram_min" {
  type    = number
  default = 256
}

variable "limits" {
  type = object({
    cpu_units      = number
    nic_rate_mbps  = number
    disk_iops      = number
    disk_rate_mbps = number
    gw_nic_mbps    = number
  })
}

variable "is_gateway" {
  description = "A per-team devbox gateway (router) rather than a plain devbox VM."
  type        = bool
  default     = false
}

# Experiment NICs: one per devbox segment this VM is attached to (net0..).
variable "segment_nics" {
  type = list(object({
    bridge  = string
    cidr    = string
    gateway = optional(string)
  }))
}

# Gateways only: the services-bridge NIC (SNAT identity).
variable "services" {
  type    = object({ bridge = string, cidr = string })
  default = null
}

# Plain VMs only: the port-isolated SSH-fabric NIC (vmbr7).
variable "ssh_fabric" {
  type    = object({ bridge = string, cidr = string })
  default = null
}

# Always last: the maintenance NIC (vmbr6, host SSH for Ansible).
variable "management" {
  type = object({ bridge = string, cidr = string })
}

variable "security_groups" {
  description = "Security-group names to apply (keys: devbox|devbox_gw, devbox_ssh_target, services_client, ssh; plain VMs also llm_access)."
  type        = map(string)
}

# Proxmox hookscript (port isolation on the management bridges and vmbr7).
# Available but attached only when applying with root@pam (see main.tf).
variable "hookscript_file_id" {
  description = "Proxmox hookscript (port isolation on management bridges / vmbr7)."
  default     = null
}
