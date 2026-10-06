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

locals {
  platform = data.terraform_remote_state.platform.outputs.platform
}

# Private-internet services. Each VM gets two NICs:
#   - private_internet (10.2.0.x, with gateway)  -> what sandbox VMs reach
#   - maintenance      (10.3.0.x, no gateway)    -> Ansible configures over this
# Same last octet in both nets per service (see opentofu/shared: infrastructure_ips).
# data_disk_size > 0 attaches a dedicated data disk (virtio1 -> /dev/vdb), mounted
# by the service's cloud-init snippet. cpu_type "host" gives cve_search AVX (MongoDB 8).
locals {
  services = {
    apt_cache = {
      cpu                         = 1
      cpu_type                    = null
      ram                         = 1024
      disk_size                   = 5
      data_disk_size              = 20
      network_cidr                = "10.2.0.20/24"
      network_gateway_ip          = "10.2.0.1"
      maintenance_network_cidr    = "10.3.0.2/24"
      maintenance_network_gateway = "10.3.0.1"
      user_data_file_id           = local.platform.cloud_init.apt_cache.file_id
      vmbr_network                = local.platform.networks.private_internet.name
      vmbr_maintenance_network    = local.platform.networks.maintenance.name
      disk_file_id                = local.platform.os_images.debian.file_id
    }
    devpi = {
      cpu                         = 1
      cpu_type                    = null
      ram                         = 2048 # devpi-web (Whoosh indexing) OOMs on 1 GB -> crash-loop, nothing cached
      disk_size                   = 5
      data_disk_size              = 10
      network_cidr                = "10.2.0.21/24"
      network_gateway_ip          = "10.2.0.1"
      maintenance_network_cidr    = "10.3.0.3/24"
      maintenance_network_gateway = "10.3.0.1"
      user_data_file_id           = local.platform.cloud_init.devpi.file_id
      vmbr_network                = local.platform.networks.private_internet.name
      vmbr_maintenance_network    = local.platform.networks.maintenance.name
      disk_file_id                = local.platform.os_images.debian.file_id
    }
    docker_registry = {
      cpu                         = 1
      cpu_type                    = null
      ram                         = 1024
      disk_size                   = 5
      data_disk_size              = 20
      network_cidr                = "10.2.0.22/24"
      network_gateway_ip          = "10.2.0.1"
      maintenance_network_cidr    = "10.3.0.4/24"
      maintenance_network_gateway = "10.3.0.1"
      user_data_file_id           = local.platform.cloud_init.docker_registry.file_id
      vmbr_network                = local.platform.networks.private_internet.name
      vmbr_maintenance_network    = local.platform.networks.maintenance.name
      disk_file_id                = local.platform.os_images.debian.file_id
    }
    manpages = {
      cpu                         = 1
      cpu_type                    = null
      ram                         = 1024
      disk_size                   = 8
      data_disk_size              = 0
      network_cidr                = "10.2.0.23/24"
      network_gateway_ip          = "10.2.0.1"
      maintenance_network_cidr    = "10.3.0.5/24"
      maintenance_network_gateway = "10.3.0.1"
      user_data_file_id           = local.platform.cloud_init.manpages.file_id
      vmbr_network                = local.platform.networks.private_internet.name
      vmbr_maintenance_network    = local.platform.networks.maintenance.name
      disk_file_id                = local.platform.os_images.debian.file_id
    }
    ntp = {
      cpu                         = 1
      cpu_type                    = null
      ram                         = 512
      disk_size                   = 5
      data_disk_size              = 0
      network_cidr                = "10.2.0.24/24"
      network_gateway_ip          = "10.2.0.1"
      maintenance_network_cidr    = "10.3.0.6/24"
      maintenance_network_gateway = "10.3.0.1"
      user_data_file_id           = local.platform.cloud_init.ntp.file_id
      vmbr_network                = local.platform.networks.private_internet.name
      vmbr_maintenance_network    = local.platform.networks.maintenance.name
      disk_file_id                = local.platform.os_images.debian.file_id
    }
    cve_search = {
      cpu                         = 2
      cpu_type                    = "host"
      ram                         = 4096
      disk_size                   = 10
      data_disk_size              = 10
      network_cidr                = "10.2.0.25/24"
      network_gateway_ip          = "10.2.0.1"
      maintenance_network_cidr    = "10.3.0.7/24"
      maintenance_network_gateway = "10.3.0.1"
      user_data_file_id           = local.platform.cloud_init.cve_search.file_id
      vmbr_network                = local.platform.networks.private_internet.name
      vmbr_maintenance_network    = local.platform.networks.maintenance.name
      disk_file_id                = local.platform.os_images.debian.file_id
    }
    wikipedia = {
      cpu                         = 1
      cpu_type                    = null
      ram                         = 2048
      disk_size                   = 5
      data_disk_size              = 20
      network_cidr                = "10.2.0.26/24"
      network_gateway_ip          = "10.2.0.1"
      maintenance_network_cidr    = "10.3.0.8/24"
      maintenance_network_gateway = "10.3.0.1"
      user_data_file_id           = local.platform.cloud_init.wikipedia.file_id
      vmbr_network                = local.platform.networks.private_internet.name
      vmbr_maintenance_network    = local.platform.networks.maintenance.name
      disk_file_id                = local.platform.os_images.debian.file_id
    }
  }

  # Internet egress is no longer a VM in this stack. During the prep phase the
  # VMs reach the internet directly (host masquerade); at runtime the platform
  # stack seals that egress with a proxmox-firewall forward rule
  # (internet_enabled=false). The caches here just toggle their own online/offline
  # behaviour via the Ansible internet_enabled var.
  private_internet = local.services
}

module "private_internet_vms" {
  source = "../../modules/private_internet_vm"

  for_each = local.private_internet

  proxmox_instance         = var.proxmox_instance
  pool_id                  = local.platform.pools["private_internet"].pool_id
  name                     = replace(each.key, "_", "-")
  cpu                      = each.value.cpu
  cpu_type                 = each.value.cpu_type
  ram                      = each.value.ram
  disk_size                = each.value.disk_size
  data_disk_size           = each.value.data_disk_size
  network_cidr             = each.value.network_cidr
  network_gateway_ip       = each.value.network_gateway_ip
  maintenance_network_cidr = each.value.maintenance_network_cidr
  user_data_file_id        = each.value.user_data_file_id
  vmbr_network             = each.value.vmbr_network
  vmbr_maintenance_network = each.value.vmbr_maintenance_network
  disk_file_id             = each.value.disk_file_id
}
