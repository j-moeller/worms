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

module "os_images" {
  source = "../../modules/os_images"

  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance
}


module "cloud_init" {
  source = "../../modules/cloud_init"

  datastore_id   = var.datastore_id
  node_name      = var.proxmox_instance
  ssh_public_key = var.ssh_public_key
}


module "networks" {
  source = "../../modules/network"

  node_name             = var.proxmox_instance
  private_internet_cidr = "10.2.0.1/24"
  maintenance_cidr      = "10.3.0.1/24"
  monitoring_cidr       = "10.250.0.1/24"
}
