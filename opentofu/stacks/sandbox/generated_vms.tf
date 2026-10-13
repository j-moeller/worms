/*
locals {
  seed = "sandbox"

  sandbox_bridges = {
    network-1000 = {
      name         = "vmbr1000"
      network_cidr = "10.5.0.0/24"
      }, network-1001 = {
      name         = "vmbr1001"
      network_cidr = "10.5.1.0/24"
      }, network-1002 = {
      name         = "vmbr1002"
      network_cidr = "10.5.2.0/24"
      }, network-1003 = {
      name         = "vmbr1003"
      network_cidr = "10.5.3.0/24"
  } }

  sandbox_vms = {
    sandbox-site-1-vm-1 = {
      cpu               = 1
      ram               = 2048
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.10/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1002.name
          }, {
          bridge = local.sandbox_bridges.network-1000.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.2.239/24"
          gateway      = "10.5.2.1" }, {
          network_cidr = "10.5.0.142/24"
      gateway = "10.5.0.1" }]
      }, sandbox-site-2-vm-2 = {
      cpu               = 1
      ram               = 2048
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.11/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1001.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.1.72/24"
      gateway = "10.5.1.1" }]
      }, sandbox-site-1-vm-3 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.12/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1002.name
          }, {
          bridge = local.sandbox_bridges.network-1000.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.2.162/24"
          gateway      = "10.5.2.1" }, {
          network_cidr = "10.5.0.26/24"
      gateway = "10.5.0.1" }]
      }, sandbox-site-2-vm-4 = {
      cpu               = 1
      ram               = 2048
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.13/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1003.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.3.93/24"
      gateway = "10.5.3.1" }]
      }, sandbox-site-1-vm-5 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.14/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1000.name
          }, {
          bridge = local.sandbox_bridges.network-1002.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.0.34/24"
          gateway      = "10.5.0.1" }, {
          network_cidr = "10.5.2.96/24"
      gateway = "10.5.2.1" }]
      }, sandbox-site-2-vm-6 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.15/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1003.name
          }, {
          bridge = local.sandbox_bridges.network-1001.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.3.187/24"
          gateway      = "10.5.3.1" }, {
          network_cidr = "10.5.1.105/24"
      gateway = "10.5.1.1" }]
      }, sandbox-site-1-vm-7 = {
      cpu               = 1
      ram               = 2048
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.16/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1000.name
          }, {
          bridge = local.sandbox_bridges.network-1002.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.0.65/24"
          gateway      = "10.5.0.1" }, {
          network_cidr = "10.5.2.43/24"
      gateway = "10.5.2.1" }]
      }, sandbox-site-2-vm-8 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.17/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1003.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.3.121/24"
      gateway = "10.5.3.1" }]
      }, sandbox-site-1-vm-9 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.18/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1000.name
          }, {
          bridge = local.sandbox_bridges.network-1002.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.0.150/24"
          gateway      = "10.5.0.1" }, {
          network_cidr = "10.5.2.223/24"
      gateway = "10.5.2.1" }]
      }, sandbox-site-2-vm-10 = {
      cpu               = 1
      ram               = 2048
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.19/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1001.name
          }, {
          bridge = local.sandbox_bridges.network-1003.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.1.57/24"
          gateway      = "10.5.1.1" }, {
          network_cidr = "10.5.3.88/24"
      gateway = "10.5.3.1" }]
      }, sandbox-site-1-vm-11 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.20/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1000.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.0.187/24"
      gateway = "10.5.0.1" }]
      }, sandbox-site-2-vm-12 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.21/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1001.name
          }, {
          bridge = local.sandbox_bridges.network-1003.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.1.120/24"
          gateway      = "10.5.1.1" }, {
          network_cidr = "10.5.3.86/24"
      gateway = "10.5.3.1" }]
      }, sandbox-site-1-vm-13 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.22/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1002.name
          }, {
          bridge = local.sandbox_bridges.network-1000.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.2.70/24"
          gateway      = "10.5.2.1" }, {
          network_cidr = "10.5.0.185/24"
      gateway = "10.5.0.1" }]
      }, sandbox-site-2-vm-14 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.23/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1003.name
          }, {
          bridge = local.sandbox_bridges.network-1001.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.3.172/24"
          gateway      = "10.5.3.1" }, {
          network_cidr = "10.5.1.241/24"
      gateway = "10.5.1.1" }]
      }, sandbox-site-1-vm-15 = {
      cpu               = 1
      ram               = 2048
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.24/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1000.name
          }, {
          bridge = local.sandbox_bridges.network-1002.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.0.173/24"
          gateway      = "10.5.0.1" }, {
          network_cidr = "10.5.2.215/24"
      gateway = "10.5.2.1" }]
      }, sandbox-site-2-vm-16 = {
      cpu               = 1
      ram               = 2048
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.25/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1003.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.3.111/24"
      gateway = "10.5.3.1" }]
      }, sandbox-site-1-vm-17 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.26/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1000.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.0.194/24"
      gateway = "10.5.0.1" }]
      }, sandbox-site-2-vm-18 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.27/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1001.name
          }, {
          bridge = local.sandbox_bridges.network-1003.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.1.108/24"
          gateway      = "10.5.1.1" }, {
          network_cidr = "10.5.3.156/24"
      gateway = "10.5.3.1" }]
      }, sandbox-site-1-vm-19 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.28/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1000.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.0.239/24"
      gateway = "10.5.0.1" }]
      }, sandbox-site-2-vm-20 = {
      cpu               = 1
      ram               = 1024
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.29/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.network-1003.name
      }]

      ip_configs = [
        {
          network_cidr = "10.5.3.87/24"
      gateway = "10.5.3.1" }]
  } }
}
*/