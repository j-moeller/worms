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

# Private-internet services. Each VM gets two NICs:
#   - net0 services     (10.2.0.x, no gateway)  -> what sandbox VMs reach (via sandbox-gw)
#   - net1 mgmt-exposed (10.5.0.x, gw = host)   -> SSH from the host; routed to the
#     internet ONLY while internet access is allowed (Internet.yml), which cannot coexist
#     with sandbox VMs. Otherwise these VMs have no path out.
# Same last octet in both nets per service (see opentofu/shared: private_internet_ips).
# data_disk_size > 0 attaches a dedicated data disk (virtio1 -> /dev/vdb), mounted
# by the service's cloud-init snippet. cpu_type "host" gives cve_search AVX (MongoDB 8).
locals {
  sg = data.terraform_remote_state.firewall.outputs.security_groups

  # net0: serve the services bridge, and use apt-cache/DNS there (apt_client).
  # net1: SSH from the host; internet egress while internet access is allowed (Internet.yml);
  #       syslog to wazuh (routed by the host).
  firewall_rules = [
    { group = local.sg.private_internet, iface = "net0" },
    { group = local.sg.services_client, iface = "net0" },
    { group = local.sg.ssh, iface = "net1" },
    { group = local.sg.provisioning, iface = "net1" },
    { group = local.sg.log_client, iface = "net1" },
  ]

  services = {
    /*
    apt_cache = {
      cpu               = 1
      cpu_type          = null
      ram               = 1024
      disk_size         = 5
      data_disk_size    = 20
      user_data_file_id = local.platform.cloud_init.apt_cache.file_id
    }
    devpi = {
      cpu               = 1
      cpu_type          = null
      ram               = 2048 # devpi-web (Whoosh indexing) OOMs on 1 GB -> crash-loop, nothing cached
      disk_size         = 5
      data_disk_size    = 10
      user_data_file_id = local.platform.cloud_init.devpi.file_id
    }
    docker_registry = {
      cpu               = 1
      cpu_type          = null
      ram               = 1024
      disk_size         = 5
      data_disk_size    = 20
      user_data_file_id = local.platform.cloud_init.docker_registry.file_id
    }
    manpages = {
      cpu               = 1
      cpu_type          = null
      ram               = 1024
      disk_size         = 8
      data_disk_size    = 0
      user_data_file_id = local.platform.cloud_init.manpages.file_id
    }
    ntp = {
      cpu               = 1
      cpu_type          = null
      ram               = 512
      disk_size         = 5
      data_disk_size    = 0
      user_data_file_id = local.platform.cloud_init.ntp.file_id
    }
    cve_search = {
      cpu               = 2
      cpu_type          = "host"
      ram               = 4096
      disk_size         = 10
      data_disk_size    = 10
      user_data_file_id = local.platform.cloud_init.cve_search.file_id
    }
    */
    wikipedia = {
      cpu               = 1
      cpu_type          = null
      ram               = 2048
      disk_size         = 5
      data_disk_size    = 20
      user_data_file_id = local.platform.cloud_init.wikipedia.file_id
    }
  }
}

module "private_internet_vms" {
  source = "../../modules/private_internet_vm"

  for_each = local.services

  proxmox_instance  = var.proxmox_instance
  pool_id           = local.platform.pools["private_internet"].pool_id
  name              = replace(each.key, "_", "-")
  cpu               = each.value.cpu
  cpu_type          = each.value.cpu_type
  ram               = each.value.ram
  disk_size         = each.value.disk_size
  data_disk_size    = each.value.data_disk_size
  disk_file_id      = local.platform.os_images.debian.file_id
  user_data_file_id = each.value.user_data_file_id

  vmbr_network                = local.platform.networks.services.name
  network_cidr                = "${local.private_internet_ips[each.key].network}/24"
  vmbr_maintenance_network    = local.platform.networks.mgmt_exposed.name
  maintenance_network_cidr    = "${local.private_internet_ips[each.key].maintenance}/24"
  maintenance_network_gateway = local.host_ips.mgmt_exposed

  firewall_rules     = local.firewall_rules
  hookscript_file_id = local.platform.hookscript_file_id
}
