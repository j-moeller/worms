# The unprivileged account teams log in as on the SSH gateway. Their key
# options (permitopen/restrict, assembled by the ssh_gateway Ansible role)
# bound each key to that team's own devbox VMs.
variable "gateway_user" {
  type    = string
  default = "team"
}
