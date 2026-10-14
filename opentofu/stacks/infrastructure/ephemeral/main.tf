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
    path = "../../platform/terraform.tfstate"
  }
}

locals {
  platform = data.terraform_remote_state.platform.outputs.platform
}

locals {
  sg = local.platform.security_groups
  nw = local.platform.networks
}

# DNS for the services bridge (sandbox VMs resolve through sandbox-gw).
# net0 = services (no gateway), net1 = mgmt-exposed (gateway = host, only
# forwarded to the internet while internet access is allowed (Internet.yml), for
# upstream DNS while populating).
module "dns" {
  source = "../../../modules/private_internet_vm"

  proxmox_instance            = var.proxmox_instance
  name                        = "dns"
  cpu                         = 1
  ram                         = 1024
  network_cidr                = "${local.services_ips.dns}/24"
  maintenance_network_cidr    = "${local.management_ips.dns}/24"
  maintenance_network_gateway = local.host_ips.mgmt_exposed
  user_data_file_id           = local.platform.cloud_init.dns.file_id
  vmbr_network                = local.nw.services.name
  vmbr_maintenance_network    = local.nw.mgmt_exposed.name
  disk_file_id                = local.platform.os_images.debian.file_id
  hookscript_file_id          = local.platform.hookscript_file_id
  pool_id                     = local.platform.pools["private_internet"].pool_id
  firewall_rules = [
    { group = local.sg.dns, iface = "net0" },
    { group = local.sg.ssh, iface = "net1" },
    { group = local.sg.provisioning, iface = "net1" },
  ]
}

# Scoring and logging: the only dual-homed VMs between the sandbox side and the
# database. net0 = services (sandbox API, inbound 443 from sandbox-gw only),
# net1 = ingest (default route; outbound database:5432 only), net2 = mgmt-exposed.
module "sandbox_apis" {
  source = "../../../modules/infrastructure_vm"

  for_each = toset(["scoring", "logging"])

  proxmox_instance = var.proxmox_instance
  name             = each.key
  nics = [
    { bridge = local.nw.services.name, cidr = "${local.services_ips[each.key]}/24" },
    { bridge = local.nw.ingest.name, cidr = "${local.infrastructure_ips[each.key]}/24", gateway = local.host_ips.ingest },
    { bridge = local.nw.mgmt_exposed.name, cidr = "${local.management_ips[each.key]}/24" },
  ]
  firewall_rules = [
    { group = local.sg.sandbox_api, iface = "net0" },
    { group = local.sg.db_client, iface = "net1" },
    { group = local.sg.provisioning, iface = "net1" },
    { group = local.sg.ssh, iface = "net2" },
  ]
  user_data_file_id  = local.platform.cloud_init.flask.file_id
  disk_file_id       = local.platform.os_images.debian.file_id
  hookscript_file_id = local.platform.hookscript_file_id
  tag                = "ephemeral"
  pool_id            = local.platform.pools["ephemeral"].pool_id
}

# The network monitor (modules/network_monitor_vm) is not deployed yet. When it
# is: data NIC on ingest (db_client), mirror NIC on vmbr3, management on
# mgmt-exposed (it parses attacker traffic, so it is high risk).

# Renamed module instances (keep the existing VMs; only their NICs change).
moved {
  from = module.private_internet_vms["dns"]
  to   = module.dns
}

moved {
  from = module.infrastructure_vms["scoring"]
  to   = module.sandbox_apis["scoring"]
}

moved {
  from = module.infrastructure_vms["logging"]
  to   = module.sandbox_apis["logging"]
}
