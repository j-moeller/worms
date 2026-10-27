variable "proxmox_instance" {
  description = "Name of the proxmox instance"
  type        = string
  default     = "worms-pve"
}

variable "proxmox_api_url" {
  description = "URL of the proxmox instance"
  type        = string
}

variable "proxmox_api_token" {
  description = "API token"
  type        = string
  sensitive   = true
}

variable "datastore_id" {
  description = "Id of the datastore"
  type        = string
  default     = "local"
}

variable "ssh_public_key" {
  description = "Existing public SSH key name"
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}

# The one external IP the sandbox/devbox VMs may reach on TCP 443 (the
# `llm-access` security group, egress only). shared-variables.tf is symlinked
# into every stack, so this carries a safe placeholder default (loopback =
# effectively no access); set the real LLM endpoint with TF_VAR_llm_access_ip.
variable "llm_access_ip" {
  description = "IP the sandbox/devbox VMs may reach on TCP 443 for LLM access (egress only)."
  type        = string
  default     = "127.0.0.1"
}

# =============================================================================
# SINGLE SOURCE OF TRUTH for all non-sandbox network addressing.
#
# This file is symlinked into every stack (opentofu/stacks/*/shared-variables.tf),
# so these locals are the one place where bridges, subnets, VM addresses and the
# Proxmox host addresses are defined. Every stack, the firewall, the Ansible
# inventory (scripts/generate_inventory.py) and through it the host nftables
# ruleset (ansible role host_network) consume them via the platform stack's `infrastructure`
# output. Do not hardcode these addresses anywhere else.
#
# Zones (see README "Network design"):
#
#   bridge  zone           subnet          host IP     members
#   vmbr20  dmz            10.10.0.0/24    10.10.0.1   dashboard (internet via host DNAT)
#   vmbr21  core           10.11.0.0/24    10.11.0.1   database, orchestrator
#   vmbr22  ingest         10.12.0.0/24    10.12.0.1   scoring, logging (DB leg)
#   vmbr24  siem           10.13.0.0/24    10.13.0.1   wazuh (central log server)
#   vmbr23  services       10.2.0.0/24     none        scoring/logging (sandbox leg),
#                                                      private internet, DNS, sandbox-gw
#   vmbr1*  sandbox        10.64.0.0/10    none        generated sandbox networks
#   vmbr3   mirror         -               none        sandbox traffic mirror (monitor)
#   vmbr2   mgmt-trusted   10.3.0.0/24     10.3.0.1    SSH: database, orchestrator
#   vmbr5   mgmt-exposed   10.5.0.0/24     10.5.0.1    SSH: dashboard, scoring, logging,
#                                                      DNS, private internet, monitor
#   vmbr4   mgmt-sandbox   10.4.0.0/16     10.4.0.1    SSH: sandbox VMs, sandbox-gw
#
# The host routes ONLY between dmz/core/ingest (allow-listed in host nftables,
# ansible role host_network), plus syslog (tcp 514) from every infrastructure VM
# to wazuh on the siem bridge. It has no address on any bridge a sandbox VM can reach,
# and never forwards traffic arriving on a management bridge (except mgmt-exposed
# while internet access is allowed (Internet.yml), which is refused while
# sandbox VMs exist).
#
# Sandbox VM addressing is NOT here: it is dynamic, owned entirely by
# opentofu/stacks/sandbox (generated_vms*.tf) and surfaced via its own output.
# =============================================================================
locals {
  # Bridge names. vmbr1* is reserved for the generated sandbox networks and
  # vmbr9* for the generated devbox networks (docs/devbox.md), so the host
  # firewall can match each family with a prefix; infrastructure bridges avoid
  # both. Devbox and sandbox share only the services bridge (both reach
  # scoring/logging/private-internet there, never each other -- see the host
  # nftables ruleset and docs/devbox.md).
  bridges = {
    dmz          = "vmbr20"
    core         = "vmbr21"
    ingest       = "vmbr22"
    services     = "vmbr23"
    siem         = "vmbr24"
    mirror       = "vmbr3"
    mgmt_trusted = "vmbr2"
    mgmt_exposed = "vmbr5"
    mgmt_sandbox = "vmbr4"
    mgmt_devbox  = "vmbr6" # untrusted devbox maintenance (host SSH, port-isolated)
    devbox_ssh   = "vmbr7" # devbox SSH fabric (teams reach VMs via the gateway, port-isolated)
    sandbox_glob = "vmbr1*"
    devbox_glob  = "vmbr9*"
  }

  networks = {
    dmz          = "10.10.0.0/24"
    core         = "10.11.0.0/24"
    ingest       = "10.12.0.0/24"
    services     = "10.2.0.0/24"
    siem         = "10.13.0.0/24"
    sandbox_vms  = "10.64.0.0/10"
    devbox_vms   = "10.128.0.0/10" # 10.128-10.191; disjoint from sandbox 10.64/10
    mgmt_trusted = "10.3.0.0/24"
    mgmt_exposed = "10.5.0.0/24"
    mgmt_sandbox = "10.4.0.0/16"
    mgmt_devbox  = "10.6.0.0/16"
    devbox_ssh   = "10.7.0.0/16"
  }

  # Proxmox host addresses (the gateway of each routed zone, the SSH source on
  # each management bridge). No entry = the host has no address on that bridge.
  host_ips = {
    dmz          = "10.10.0.1"
    core         = "10.11.0.1"
    ingest       = "10.12.0.1"
    siem         = "10.13.0.1"
    mgmt_trusted = "10.3.0.1"
    mgmt_exposed = "10.5.0.1"
    mgmt_sandbox = "10.4.0.1"
    mgmt_devbox  = "10.6.0.1"
    devbox_ssh   = "10.7.0.1"
  }

  # Data-plane address of each infrastructure VM: what other VMs connect to, and
  # what the database / firewall rules see as source.
  infrastructure_ips = {
    dashboard    = "10.10.0.18"
    database     = "10.11.0.16"
    orchestrator = "10.11.0.19"
    scoring      = "10.12.0.14"
    logging      = "10.12.0.15"
    monitor      = "10.12.0.100"
    wazuh        = "10.13.0.30"
  }

  # Addresses on the services bridge (what sandbox VMs reach, via sandbox-gw).
  services_ips = {
    sandbox_gw = "10.2.0.2"
    dns        = "10.2.0.11"
    scoring    = "10.2.0.14"
    logging    = "10.2.0.15"
  }

  # Management (SSH) address of every static VM. Ansible connects to these.
  management_ips = {
    database     = "10.3.0.16"
    orchestrator = "10.3.0.19"
    dashboard    = "10.5.0.18"
    scoring      = "10.5.0.14"
    logging      = "10.5.0.15"
    dns          = "10.5.0.11"
    monitor      = "10.5.0.100"
    wazuh        = "10.5.0.30"
    sandbox_gw   = "10.4.0.2"
  }

  # Private-internet service VMs: services-bridge address (what sandbox VMs
  # reach) and management address (what Ansible reaches). Same last octet.
  private_internet_ips = {
    apt_cache       = { network = "10.2.0.20", maintenance = "10.5.0.20" }
    devpi           = { network = "10.2.0.21", maintenance = "10.5.0.21" }
    docker_registry = { network = "10.2.0.22", maintenance = "10.5.0.22" }
    manpages        = { network = "10.2.0.23", maintenance = "10.5.0.23" }
    ntp             = { network = "10.2.0.24", maintenance = "10.5.0.24" }
    cve_search      = { network = "10.2.0.25", maintenance = "10.5.0.25" }
    wikipedia       = { network = "10.2.0.26", maintenance = "10.5.0.26" }
  }

  # Ports sandbox VMs may use on the services bridge. Enforced on the sandbox
  # VMs' and sandbox-gw's NICs (the host-side tap firewall).
  private_internet_ports = {
    tcp = "53,80,443,3141,3142,5000,8888"
    udp = "53,123"
  }

  # Trusted SSH gateway (docs/devbox.md): teams reach it (host DNAT) and it
  # jumps them into their own devbox VMs over the port-isolated devbox SSH
  # fabric. It is hardened infrastructure, NOT part of the untrusted devbox.
  ssh_gateway_ips = {
    devbox_ssh  = "10.7.0.2"  # on vmbr7 (reaches every devbox VM; ports isolated)
    maintenance = "10.5.0.40" # mgmt-exposed (Ansible)
  }
  # TCP port teams connect to on the host WAN; DNATed to the gateway:22.
  ssh_gateway_public_port = 2222

  # Services-bridge addresses reserved for per-team devbox gateways (their SNAT
  # source, which the scoring/logging servers map to the team). The generated
  # devbox stack assigns within this block; sandbox-gw (10.2.0.2) is outside it.
  devbox_services_block = "10.2.0.32/27" # 10.2.0.32 - 10.2.0.63

  # Proxmox API, reached by the orchestrator on its core gateway address.
  proxmox_host_ip  = local.host_ips.core
  proxmox_api_port = 8006

  # Addresses the host uses to SSH into VMs (Ansible). The `ssh`
  # security group accepts port 22 from these only.
  ssh_sources = [
    local.host_ips.mgmt_trusted,
    local.host_ips.mgmt_exposed,
    local.host_ips.mgmt_sandbox,
    local.host_ips.mgmt_devbox,
  ]
}
