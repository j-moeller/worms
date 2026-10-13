locals {
  seed = "1000"

  sandbox_bridges = {
    net1 = {
      name         = "vmbr1000"
      network_cidr = "10.1.0.1/24"
    }
    net2 = {
      name         = "vmbr1001"
      network_cidr = "10.1.0.1/24"
    }
  }

  sandbox_vms = {
    sandbox-vm01 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.111/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.net1.name
        }
      ]

      ip_configs = [
        {
          network_cidr = "10.1.0.11/24"
          gateway      = null
        }
      ]
    }
    sandbox-vm02 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.112/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.net1.name
        }
      ]

      ip_configs = [
        {
          network_cidr = "10.1.0.12/24"
          gateway      = null
        }
      ]
    }
    sandbox-vm03 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.113/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.net1.name
        }
      ]

      ip_configs = [
        {
          network_cidr = "10.1.0.13/24"
          gateway      = null
        }
      ]
    }
  }
}