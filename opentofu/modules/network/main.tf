# One Linux bridge per network zone (see opentofu/shared/shared-variables.tf).
#
# Only the routed zones (dmz, core, ingest, siem) and the management bridges get a
# host address. The services, sandbox and mirror bridges are pure layer 2 for
# the host: it has no IP there, so nothing a sandbox VM can reach terminates on
# (or is routed by) the hypervisor's IP stack.
#
# Routing and filtering between zones is done by the host nftables ruleset
# (ansible role host_network), which OpenTofu cannot express declaratively.

locals {
  zones = {
    dmz = {
      address = "${var.host_ips.dmz}/${split("/", var.networks.dmz)[1]}"
      comment = "DMZ: dashboard (published to the internet by host DNAT on 443)."
    }
    core = {
      address = "${var.host_ips.core}/${split("/", var.networks.core)[1]}"
      comment = "Core: database and orchestrator."
    }
    ingest = {
      address = "${var.host_ips.ingest}/${split("/", var.networks.ingest)[1]}"
      comment = "Ingest: database leg of scoring/logging (routed to database:5432 only)."
    }
    siem = {
      address = "${var.host_ips.siem}/${split("/", var.networks.siem)[1]}"
      comment = "SIEM: wazuh (receives syslog from every infrastructure VM, routed tcp 514 only)."
    }
    services = {
      address = null
      comment = "Sandbox services: scoring/logging (sandbox leg), private internet, DNS, sandbox-gw. No host IP, no uplink."
    }
    mirror = {
      address = null
      comment = "Mirror of the sandbox networks for the monitor VM. No host IP."
    }
    mgmt_trusted = {
      address = "${var.host_ips.mgmt_trusted}/${split("/", var.networks.mgmt_trusted)[1]}"
      comment = "Management (SSH host -> VM) for database and orchestrator. Ports isolated."
    }
    mgmt_exposed = {
      address = "${var.host_ips.mgmt_exposed}/${split("/", var.networks.mgmt_exposed)[1]}"
      comment = "Management (SSH host -> VM) for dashboard, scoring, logging, DNS, private internet. Ports isolated."
    }
    mgmt_sandbox = {
      address = "${var.host_ips.mgmt_sandbox}/${split("/", var.networks.mgmt_sandbox)[1]}"
      comment = "Management (SSH host -> VM) for sandbox VMs and sandbox-gw. Ports isolated."
    }
    # Devbox (docs/devbox.md). The untrusted devbox VMs and their gateways get
    # their own maintenance bridge, never shared with the infrastructure's.
    mgmt_devbox = {
      address = "${var.host_ips.mgmt_devbox}/${split("/", var.networks.mgmt_devbox)[1]}"
      comment = "Management (SSH host -> VM) for devbox VMs and devbox gateways. Ports isolated."
    }
    # The SSH fabric between the trusted SSH gateway and every devbox VM. Ports
    # MUST be isolated (hookscript), so devbox VMs of different teams can reach
    # the gateway but never each other here. The host address publishes the
    # gateway (DNAT); the host does not route this bridge otherwise.
    devbox_ssh = {
      address = "${var.host_ips.devbox_ssh}/${split("/", var.networks.devbox_ssh)[1]}"
      comment = "Management: devbox SSH fabric (SSH gateway <-> devbox VMs). Ports isolated."
    }
  }
}

resource "proxmox_network_linux_bridge" "zone" {
  for_each = local.zones

  node_name = var.node_name
  name      = var.bridges[each.key]

  address   = each.value.address
  ports     = []
  autostart = true
  comment   = each.value.comment
}
