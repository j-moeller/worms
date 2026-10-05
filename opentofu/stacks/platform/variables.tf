variable "internet_enabled" {
  description = "Whether the private internet may reach the WAN. true = prep phase (full internet for populating caches); false = runtime (egress sealed at the host). Single switch, propagated from the root Makefile; keep in sync with the Ansible internet_enabled var in group_vars/all.yml."
  type        = bool
  default     = true
}
