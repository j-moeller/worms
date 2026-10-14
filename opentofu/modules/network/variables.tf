variable "node_name" {
  type = string
}

variable "bridges" {
  description = "Zone -> bridge name (local.bridges)."
  type        = map(string)
}

variable "networks" {
  description = "Zone -> subnet CIDR (local.networks)."
  type        = map(string)
}

variable "host_ips" {
  description = "Zone -> host address on that bridge (local.host_ips)."
  type        = map(string)
}
