# =============================================================================
# Devbox stack (docs/devbox.md): the team-personalised, always-on clones of the
# sandbox VMs, plus the trusted SSH gateway teams use to reach them.
#
# Per team (data in generated_devboxes*.tf, written by scripts/generate_devbox.py
# at team creation / reset):
#   - two devbox bridges (vmbr9*): the A-B link and the B-C link;
#   - three VMs A, B, C (A on A-B; B on A-B and B-C; C on B-C), each also on the
#     devbox SSH fabric (vmbr7) and the devbox maintenance bridge (vmbr6);
#   - one devbox-gw router that SNATs the team's VMs to a single services-bridge
#     address (its scoring/logging attribution identity) and does NOT bridge the
#     two segments.
#
# Shared:
#   - one trusted SSH gateway VM (vmbr7 + mgmt-exposed), published by host DNAT.
#
# This stack is modelled on opentofu/stacks/sandbox and reuses the same resource
# limits and containment. Devbox and sandbox never share a bridge (except the
# services bridge, where the host drops gateway<->gateway), network or pool.
# =============================================================================

terraform {
  required_providers {
    proxmox = {
      source = "bpg/proxmox"
    }
  }
}

provider "proxmox" {
  endpoint  = var.proxmox_api_url
  api_token = var.proxmox_api_token
  insecure  = true

  ssh {
    agent    = true
    username = "root"
  }
}

data "terraform_remote_state" "platform" {
  backend = "local"
  config  = { path = "../platform/terraform.tfstate" }
}

data "terraform_remote_state" "firewall" {
  backend = "local"
  config  = { path = "../firewall/terraform.tfstate" }
}

locals {
  platform = data.terraform_remote_state.platform.outputs.platform
  sg       = data.terraform_remote_state.firewall.outputs.security_groups
  nw       = local.platform.networks

  ssh_public_keys = [trimspace(file(pathexpand(var.ssh_public_key)))]

  # Same storage and limits as the sandbox (untrusted): own storage, CPU and
  # NIC/disk caps (opentofu/stacks/sandbox/main.tf).
  devbox_datastore = "local-zfs-sandbox"
  limits = {
    cpu_units      = 50
    nic_rate_mbps  = 10
    disk_iops      = 200
    disk_rate_mbps = 20
    gw_nic_mbps    = 50
  }

  # The maintenance NIC is always the last one on a devbox VM; its index is the
  # number of experiment NICs + the SSH-fabric NIC.
}

# Guard: a devbox (like a sandbox) must never be built while internet access is
# allowed (the VMs are untrusted). The flag is written by ansible/Internet.yml.
locals {
  internet_allowed = fileexists("/run/worms/internet-allowed")
}

resource "terraform_data" "guard" {
  input = local.internet_allowed

  lifecycle {
    precondition {
      condition     = !local.internet_allowed
      error_message = "Internet access is allowed (/run/worms/internet-allowed). Run make internet-deny before deploying the devbox."
    }
    precondition {
      condition = alltrue([
        for b in values(local.devbox_bridges) :
        startswith(b.name, "vmbr9") && can(cidrhost(b.network_cidr, 1))
      ])
      error_message = "Devbox bridges must be named vmbr9NNN (the host firewall matches vmbr9*) and carry a valid CIDR inside networks.devbox_vms."
    }
  }
}

# -----------------------------------------------------------------------------
# Per-team devbox bridges (pure layer-2, no host IP).
# -----------------------------------------------------------------------------
resource "proxmox_network_linux_bridge" "devbox" {
  for_each = local.devbox_bridges

  node_name = var.proxmox_instance
  name      = each.value.name
  ports     = []
  autostart = true
  comment   = "Devbox network ${each.key} (team ${each.value.team}, no host IP)"

  depends_on = [terraform_data.guard]
}

# -----------------------------------------------------------------------------
# cloud-init snippets.
# -----------------------------------------------------------------------------
resource "proxmox_virtual_environment_file" "devbox_vm" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data      = templatefile("${path.module}/templates/devbox-vm.tftpl", { ssh_public_keys = local.ssh_public_keys })
    file_name = "devbox-vm.yml"
  }
}

resource "proxmox_virtual_environment_file" "devbox_gw" {
  for_each = local.devbox_gws

  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data = templatefile("${path.module}/templates/devbox-gw.tftpl", {
      hostname           = each.value.name
      ssh_public_keys    = local.ssh_public_keys
      services_network   = local.networks.services
      mgmt_host_ip       = local.host_ips.mgmt_devbox
      services_tcp_ports = local.private_internet_ports.tcp
      services_udp_ports = local.private_internet_ports.udp
      devbox_segments    = [for b in each.value.segments : local.devbox_bridges[b].network_cidr]
    })
    file_name = "devbox-gw-${each.key}.yml"
  }
}

resource "proxmox_virtual_environment_file" "ssh_gateway" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data = templatefile("${path.module}/templates/ssh-gateway.tftpl", {
      hostname        = "devbox-ssh-gateway"
      ssh_public_keys = local.ssh_public_keys
      gateway_user    = var.gateway_user
    })
    file_name = "devbox-ssh-gateway.yml"
  }
}

# -----------------------------------------------------------------------------
# Devbox VMs (A, B, C per team).
# -----------------------------------------------------------------------------
module "devbox_vm" {
  source   = "../../modules/devbox_vm"
  for_each = local.devbox_vms

  proxmox_instance  = var.proxmox_instance
  name              = each.value.name
  datastore         = local.devbox_datastore
  disk_file_id      = local.platform.os_images.debian.file_id
  user_data_file_id = proxmox_virtual_environment_file.devbox_vm.id
  pool_id           = local.platform.pools["devbox"].pool_id
  limits            = local.limits
  dns_server        = local.services_ips.dns

  # Experiment NICs (one per devbox segment this VM is on), then the SSH-fabric
  # NIC (vmbr7) and the maintenance NIC (vmbr6). Only the first segment carries
  # the default route (via the team's devbox-gw); a VM has one gateway.
  segment_nics = [
    for i, b in each.value.segments : {
      bridge  = local.devbox_bridges[b].name
      cidr    = each.value.segment_cidrs[b]
      gateway = i == 0 ? each.value.gateway_ip : null
    }
  ]
  ssh_fabric = { bridge = local.nw.devbox_ssh.name, cidr = each.value.ssh_fabric_cidr }
  management = { bridge = local.nw.mgmt_devbox.name, cidr = each.value.maintenance_cidr }

  security_groups = {
    devbox            = local.sg.devbox
    devbox_ssh_target = local.sg.devbox_ssh_target
    ssh               = local.sg.ssh
  }

  depends_on = [proxmox_network_linux_bridge.devbox]
}

# -----------------------------------------------------------------------------
# Per-team devbox gateways.
# -----------------------------------------------------------------------------
module "devbox_gw" {
  source   = "../../modules/devbox_vm"
  for_each = local.devbox_gws

  proxmox_instance  = var.proxmox_instance
  name              = each.value.name
  datastore         = local.devbox_datastore
  disk_file_id      = local.platform.os_images.debian.file_id
  user_data_file_id = proxmox_virtual_environment_file.devbox_gw[each.key].id
  pool_id           = local.platform.pools["devbox"].pool_id
  limits            = local.limits
  dns_server        = local.services_ips.dns
  is_gateway        = true

  # The gateway sits on both devbox segments, the services bridge (its SNAT
  # identity) and the maintenance bridge. No SSH-fabric NIC.
  segment_nics = [
    for b in each.value.segments : {
      bridge  = local.devbox_bridges[b].name
      cidr    = each.value.segment_cidrs[b]
      gateway = null
    }
  ]
  services   = { bridge = local.nw.services.name, cidr = each.value.services_cidr }
  management = { bridge = local.nw.mgmt_devbox.name, cidr = each.value.maintenance_cidr }

  security_groups = {
    devbox_gw       = local.sg.devbox_gw
    services_client = local.sg.services_client
    ssh             = local.sg.ssh
  }

  depends_on = [proxmox_network_linux_bridge.devbox]
}

# -----------------------------------------------------------------------------
# Trusted SSH gateway (shared). Teams reach it via host DNAT; it jumps them into
# their own devbox VMs over the port-isolated SSH fabric.
# -----------------------------------------------------------------------------
resource "proxmox_virtual_environment_vm" "ssh_gateway" {
  name      = "devbox-ssh-gateway"
  node_name = var.proxmox_instance

  cpu { cores = 1 }
  memory { dedicated = 512 }

  agent { enabled = false }
  tablet_device   = false
  stop_on_destroy = true
  vga { type = "none" }

  tags = ["infrastructure", "devbox-ssh-gateway"]

  initialization {
    datastore_id = "local-zfs"

    ip_config {
      ipv4 {
        address = "${local.ssh_gateway_ips.devbox_ssh}/${split("/", local.networks.devbox_ssh)[1]}"
      }
    }
    ip_config {
      ipv4 {
        address = "${local.ssh_gateway_ips.maintenance}/${split("/", local.networks.mgmt_exposed)[1]}"
        gateway = local.host_ips.mgmt_exposed
      }
    }
    user_data_file_id = proxmox_virtual_environment_file.ssh_gateway.id
  }

  network_device {
    bridge   = local.nw.devbox_ssh.name
    model    = "virtio"
    firewall = true
  }
  network_device {
    bridge   = local.nw.mgmt_exposed.name
    model    = "virtio"
    firewall = true
  }

  disk {
    datastore_id = "local-zfs"
    file_id      = local.platform.os_images.debian.file_id
    interface    = "virtio0"
    iothread     = true
    discard      = "on"
    size         = 5
  }
}

resource "proxmox_virtual_environment_firewall_options" "ssh_gateway" {
  node_name     = var.proxmox_instance
  vm_id         = proxmox_virtual_environment_vm.ssh_gateway.vm_id
  enabled       = true
  input_policy  = "DROP"
  output_policy = "DROP"
  ipfilter      = true
  macfilter     = true
}

resource "proxmox_virtual_environment_firewall_ipset" "ssh_gateway" {
  for_each = {
    "ipfilter-net0" = local.ssh_gateway_ips.devbox_ssh
    "ipfilter-net1" = local.ssh_gateway_ips.maintenance
  }
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.ssh_gateway.vm_id
  name      = each.key
  cidr { name = each.value }
}

resource "proxmox_virtual_environment_firewall_rules" "ssh_gateway" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.ssh_gateway.vm_id

  rule {
    security_group = local.sg.devbox_ssh_gateway
    iface          = "net0"
    comment        = "Teams in (public DNAT), jump to devbox VMs on the fabric"
  }
  rule {
    security_group = local.sg.ssh
    iface          = "net1"
    comment        = "SSH from the Proxmox host (Ansible) on the management NIC"
  }
}

resource "proxmox_pool_membership" "ssh_gateway" {
  pool_id = local.platform.pools["persistent"].pool_id
  vm_id   = proxmox_virtual_environment_vm.ssh_gateway.vm_id
}
