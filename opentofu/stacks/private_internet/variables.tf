variable "internet_enabled" {
  description = "Whether the private internet has upstream internet. true = egress-proxy VM created and caches proxy through it; false = fully offline, egress-proxy VM not created. Keep in sync with the Ansible internet_enabled var in group_vars/all.yml."
  type        = bool
  default     = true
}
