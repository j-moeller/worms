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

  config = {
    path = "../platform/terraform.tfstate"
  }
}

data "terraform_remote_state" "firewall" {
  backend = "local"

  config = {
    path = "../firewall/terraform.tfstate"
  }
}

locals {
  platform = data.terraform_remote_state.platform.outputs.platform
}

locals {
  sg = data.terraform_remote_state.firewall.outputs.security_groups
  nw = local.platform.networks

  # The router takes the first host address (.1) of every generated network;
  # the generator hands out .1 as the sandbox VMs' gateway.
  bridge_keys = sort(keys(local.sandbox_bridges))
  gw_sandbox_nics = [
    for key in local.bridge_keys : {
      bridge = local.sandbox_bridges[key].name
      cidr   = "${cidrhost(local.sandbox_bridges[key].network_cidr, 1)}/${split("/", local.sandbox_bridges[key].network_cidr)[1]}"
    }
  ]
  gw_services_iface = "net${length(local.gw_sandbox_nics)}"
  gw_mgmt_iface     = "net${length(local.gw_sandbox_nics) + 1}"

  ssh_public_keys = [trimspace(file(pathexpand(var.ssh_public_key)))]

  # Untrusted VMs live on their own storage (a full or saturated disk cannot
  # take down the database & co. on local-zfs) and get hard resource limits,
  # so one rogue VM cannot starve the host or its neighbours.
  sandbox_datastore = "local-zfs-sandbox"
  limits = {
    cpu_units      = 50  # half the scheduling weight of infrastructure VMs (100)
    nic_rate_mbps  = 10  # MB/s per NIC
    disk_iops      = 200 # per direction
    disk_rate_mbps = 20  # MB/s per direction
    gw_nic_mbps    = 50  # MB/s per sandbox-gw NIC (aggregate of all sandbox -> services)
  }
}

# Guard rails: a sandbox must never coexist with internet access (the flag is
# written by ansible/Internet.yml; tofu runs on the Proxmox node), and the
# router can carry at most 32 NICs (sandbox legs + services + management).
locals {
  internet_allowed = fileexists("/run/worms/internet-allowed")
}

resource "terraform_data" "guard" {
  input = local.internet_allowed

  lifecycle {
    precondition {
      condition     = !local.internet_allowed
      error_message = "Internet access is allowed (/run/worms/internet-allowed). Run make internet-deny before deploying a sandbox."
    }

    precondition {
      condition     = length(local.sandbox_bridges) <= 30
      error_message = "sandbox-gw supports at most 30 sandbox networks (Proxmox allows 32 NICs per VM)."
    }

    precondition {
      condition = alltrue([
        for b in values(local.sandbox_bridges) :
        startswith(b.name, "vmbr1") && length(b.name) >= 8 && can(cidrhost(b.network_cidr, 1))
      ])
      error_message = "Sandbox bridges must be named vmbr1NNN (the host firewall matches the vmbr1* prefix) and carry a valid CIDR (inside networks.sandbox_vms; checked by scripts/generate_sandbox.py)."
    }
  }
}

# Pure layer-2 for the host: no address, no uplink. The host neither terminates
# nor routes sandbox traffic; sandbox-gw is the only router.
resource "proxmox_network_linux_bridge" "vmbr_sandbox" {
  for_each = local.sandbox_bridges

  node_name = var.proxmox_instance
  name      = each.value.name

  ports     = []
  autostart = true
  comment   = "Sandbox network ${each.key} (no host IP)"

  depends_on = [terraform_data.guard]
}

resource "proxmox_virtual_environment_file" "sandbox_vm" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data      = templatefile("${path.module}/templates/sandbox-vm.tftpl", { ssh_public_keys = local.ssh_public_keys })
    file_name = "sandbox-vm.yml"
  }
}

resource "proxmox_virtual_environment_file" "sandbox_gw" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data = templatefile("${path.module}/templates/sandbox-gw.tftpl", {
      ssh_public_keys    = local.ssh_public_keys
      sandbox_networks   = "{ ${local.networks.sandbox_vms} }"
      services_network   = local.networks.services
      mgmt_host_ip       = local.host_ips.mgmt_sandbox
      services_tcp_ports = local.private_internet_ports.tcp
      services_udp_ports = local.private_internet_ports.udp
    })
    file_name = "sandbox-gw.yml"
  }
}

# -----------------------------------------------------------------------------
# sandbox-gw
# -----------------------------------------------------------------------------

resource "proxmox_virtual_environment_vm" "gw" {
  name      = "sandbox-gw"
  node_name = var.proxmox_instance

  cpu {
    cores = 1
    limit = 1
    units = local.limits.cpu_units
  }
  memory { dedicated = 512 }

  # Only SSH is used: no guest agent (the host never parses guest-controlled
  # agent replies; stop/kill works without it), no serial console, no display,
  # no USB tablet. A VM that ignores the ACPI shutdown is hard-stopped on
  # destroy.
  agent { enabled = false }
  tablet_device   = false
  stop_on_destroy = true
  vga { type = "none" }

  tags = ["untrusted", "sandbox-gw"]

  # TODO:
  # HOOK:
  # hook_script_file_id = local.platform.hookscript_file_id

  initialization {
    datastore_id = local.sandbox_datastore

    dynamic "ip_config" {
      for_each = local.gw_sandbox_nics

      content {
        ipv4 {
          address = ip_config.value.cidr
        }
      }
    }

    # services leg: no gateway; the router has no default route at all.
    ip_config {
      ipv4 {
        address = "${local.services_ips.sandbox_gw}/${split("/", local.networks.services)[1]}"
      }
    }

    ip_config {
      ipv4 {
        address = "${local.management_ips.sandbox_gw}/${split("/", local.networks.mgmt_sandbox)[1]}"
      }
    }

    user_data_file_id = proxmox_virtual_environment_file.sandbox_gw.id
  }

  dynamic "network_device" {
    for_each = local.gw_sandbox_nics

    content {
      bridge     = network_device.value.bridge
      model      = "virtio"
      firewall   = true
      rate_limit = local.limits.gw_nic_mbps
    }
  }

  network_device {
    bridge     = local.nw.services.name
    model      = "virtio"
    firewall   = true
    rate_limit = local.limits.gw_nic_mbps
  }

  network_device {
    bridge     = local.nw.mgmt_sandbox.name
    model      = "virtio"
    firewall   = true
    rate_limit = local.limits.gw_nic_mbps
  }

  disk {
    datastore_id = local.sandbox_datastore
    file_id      = local.platform.os_images.debian.file_id
    interface    = "virtio0"
    iothread     = true
    discard      = "on"
    size         = 5
    speed {
      iops_read  = local.limits.disk_iops
      iops_write = local.limits.disk_iops
      read       = local.limits.disk_rate_mbps
      write      = local.limits.disk_rate_mbps
    }
  }

  depends_on = [proxmox_network_linux_bridge.vmbr_sandbox]
}

resource "proxmox_pool_membership" "gw" {
  pool_id = local.platform.pools["sandbox"].pool_id
  vm_id   = proxmox_virtual_environment_vm.gw.vm_id
}

resource "proxmox_virtual_environment_firewall_options" "gw" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.gw.vm_id

  enabled       = true
  input_policy  = "DROP"
  output_policy = "DROP"
  ipfilter      = true
  macfilter     = true
}

# Sandbox legs forward traffic of every sandbox VM (and replies from the
# services bridge); the services and management legs only their own address.
resource "proxmox_virtual_environment_firewall_ipset" "gw" {
  for_each = merge(
    { for i, nic in local.gw_sandbox_nics : "ipfilter-net${i}" => [local.networks.sandbox_vms, local.networks.services] },
    {
      "ipfilter-${local.gw_services_iface}" = [local.services_ips.sandbox_gw]
      "ipfilter-${local.gw_mgmt_iface}"     = [local.management_ips.sandbox_gw]
    },
  )

  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.gw.vm_id
  name      = each.key

  dynamic "cidr" {
    for_each = each.value

    content {
      name = cidr.value
    }
  }
}

resource "proxmox_virtual_environment_firewall_rules" "gw" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.gw.vm_id

  # Sandbox legs (rules are scoped by source/dest to the sandbox networks).
  rule {
    security_group = local.sg.sandbox_gw
    comment        = "Route sandbox <-> sandbox, sandbox -> services"
  }

  rule {
    security_group = local.sg.services_client
    iface          = local.gw_services_iface
    comment        = "Services leg: allowed ports only"
  }

  rule {
    security_group = local.sg.ssh
    iface          = local.gw_mgmt_iface
    comment        = "SSH from the Proxmox host on the management NIC"
  }
}

# -----------------------------------------------------------------------------
# Sandbox VMs
# -----------------------------------------------------------------------------

resource "proxmox_pool_membership" "pool_membership" {
  for_each = local.sandbox_vms

  pool_id = local.platform.pools["sandbox"].pool_id
  vm_id   = proxmox_virtual_environment_vm.vm[each.key].vm_id
}

resource "proxmox_virtual_environment_firewall_options" "vm" {
  for_each = local.sandbox_vms

  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.vm[each.key].vm_id

  enabled       = true
  input_policy  = "DROP"
  output_policy = "DROP"
  ipfilter      = true
  macfilter     = true
}

# Each NIC may only send from its configured address (net0.. = sandbox NICs,
# last = management).
resource "proxmox_virtual_environment_firewall_ipset" "vm" {
  for_each = merge([
    for name, vm in local.sandbox_vms : {
      for i, cidr in concat([for c in vm.ip_configs : c.network_cidr], [vm.maintenance_network_cidr]) :
      "${name}/ipfilter-net${i}" => { vm = name, set = "ipfilter-net${i}", ip = split("/", cidr)[0] }
    }
  ]...)

  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.vm[each.value.vm].vm_id
  name      = each.value.set

  cidr {
    name = each.value.ip
  }
}

# Sandbox traffic (within the sandbox networks, services bridge on the allowed
# ports) on the sandbox NICs; SSH from the host on the management NIC only.
resource "proxmox_virtual_environment_firewall_rules" "vm" {
  for_each = local.sandbox_vms

  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.vm[each.key].vm_id

  rule {
    security_group = local.sg.sandbox
    comment        = "Sandbox networks + services bridge"
  }

  rule {
    security_group = local.sg.ssh
    iface          = "net${length(each.value.network_devices)}"
    comment        = "SSH from the Proxmox host on the management NIC"
  }
}

resource "proxmox_virtual_environment_vm" "vm" {
  for_each = local.sandbox_vms

  name      = each.key
  node_name = var.proxmox_instance

  cpu {
    cores = each.value.cpu
    limit = each.value.cpu
    units = local.limits.cpu_units
  }

  memory {
    dedicated = each.value.ram
  }

  # Only SSH is used: no guest agent (the host never parses guest-controlled
  # agent replies; stop/kill works without it), no serial console, no display,
  # no USB tablet. A VM that ignores the ACPI shutdown is hard-stopped on
  # destroy.
  agent { enabled = false }
  tablet_device   = false
  stop_on_destroy = true
  vga { type = "none" }

  tags = [
    "untrusted"
  ]

  # TODO:
  # HOOK:
  # hook_script_file_id = local.platform.hookscript_file_id

  initialization {
    datastore_id = local.sandbox_datastore

    dynamic "ip_config" {
      for_each = each.value.ip_configs

      content {
        ipv4 {
          address = ip_config.value.network_cidr
          gateway = ip_config.value.gateway
        }
      }
    }

    ip_config {
      ipv4 {
        address = each.value.maintenance_network_cidr
      }
    }

    dns {
      servers = [local.services_ips.dns]
      domain  = "worms.internal"
    }

    user_data_file_id = proxmox_virtual_environment_file.sandbox_vm.id
  }

  dynamic "network_device" {
    for_each = each.value.network_devices

    content {
      bridge     = network_device.value.bridge
      model      = "virtio"
      firewall   = true
      rate_limit = local.limits.nic_rate_mbps
    }
  }

  # Management NIC on the sandbox-only management bridge (vmbr4).
  network_device {
    bridge     = local.nw.mgmt_sandbox.name
    model      = "virtio"
    firewall   = true
    rate_limit = local.limits.nic_rate_mbps
  }

  disk {
    datastore_id = local.sandbox_datastore
    file_id      = each.value.disk_file_id

    interface = "virtio0"
    iothread  = true
    discard   = "on"
    size      = 5
    speed {
      iops_read  = local.limits.disk_iops
      iops_write = local.limits.disk_iops
      read       = local.limits.disk_rate_mbps
      write      = local.limits.disk_rate_mbps
    }
  }

  depends_on = [proxmox_network_linux_bridge.vmbr_sandbox, terraform_data.guard]
}
