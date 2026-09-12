
locals {
  seed = "sandbox"

  sandbox_bridges = {
    bridge-1000 = {
      name         = "vmbr1000"
      network_cidr = "10.5.0.0/24"
    }
    bridge-1001 = {
      name         = "vmbr1001"
      network_cidr = "10.6.0.0/24"
    }
    bridge-1002 = {
      name         = "vmbr1002"
      network_cidr = "10.7.0.0/24"
    }
    bridge-1003 = {
      name         = "vmbr1003"
      network_cidr = "10.8.0.0/24"
    }
    bridge-1004 = {
      name         = "vmbr1004"
      network_cidr = "10.9.0.0/24"
    }
    bridge-1005 = {
      name         = "vmbr1005"
      network_cidr = "10.10.0.0/24"
    }
    bridge-1006 = {
      name         = "vmbr1006"
      network_cidr = "10.11.0.0/24"
    }
    bridge-1007 = {
      name         = "vmbr1007"
      network_cidr = "10.12.0.0/24"
    }
  }

  sandbox_vms = {
    sandbox-sandbox-vm-1 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.10/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1003.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        },        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.8.0.71/24"
          gateway      = "10.8.0.1"        },        {
          network_cidr = "10.9.0.89/24"
          gateway      = null        },        {
          network_cidr = "10.5.0.22/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-2 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.11/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        },        {
          bridge = local.sandbox_bridges.bridge-1000.name
        },        {
          bridge = local.sandbox_bridges.bridge-1003.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.40/24"
          gateway      = "10.6.0.1"        },        {
          network_cidr = "10.5.0.24/24"
          gateway      = "10.5.0.1"        },        {
          network_cidr = "10.8.0.76/24"
          gateway      = "10.8.0.1"        }      ]
    },    sandbox-sandbox-vm-3 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.12/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1004.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.9.0.94/24"
          gateway      = "10.9.0.1"        }      ]
    },    sandbox-sandbox-vm-4 = {
      cpu               = 1
      ram               = 2048
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.13/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.46/24"
          gateway      = "10.6.0.1"        },        {
          network_cidr = "10.9.0.98/24"
          gateway      = "10.9.0.1"        }      ]
    },    sandbox-sandbox-vm-5 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.14/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.134/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-6 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.15/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.52/24"
          gateway      = "10.6.0.1"        }      ]
    },    sandbox-sandbox-vm-7 = {
      cpu               = 1
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.16/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1002.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.7.0.72/24"
          gateway      = "10.7.0.1"        }      ]
    },    sandbox-sandbox-vm-8 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.17/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.143/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-9 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.18/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.44/24"
          gateway      = "10.5.0.1"        }      ]
    },    sandbox-sandbox-vm-10 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.19/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        },        {
          bridge = local.sandbox_bridges.bridge-1005.name
        },        {
          bridge = local.sandbox_bridges.bridge-1001.name
        },        {
          bridge = local.sandbox_bridges.bridge-1002.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.47/24"
          gateway      = "10.5.0.1"        },        {
          network_cidr = "10.10.0.133/24"
          gateway      = null        },        {
          network_cidr = "10.6.0.66/24"
          gateway      = null        },        {
          network_cidr = "10.7.0.84/24"
          gateway      = "10.7.0.1"        }      ]
    },    sandbox-sandbox-vm-11 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.20/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1002.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.7.0.84/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-12 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.21/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.53/24"
          gateway      = "10.5.0.1"        }      ]
    },    sandbox-sandbox-vm-13 = {
      cpu               = 2
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.22/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.141/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-14 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.23/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.161/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-15 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.24/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1002.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.7.0.96/24"
          gateway      = "10.7.0.1"        }      ]
    },    sandbox-sandbox-vm-16 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.25/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1004.name
        },        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.9.0.133/24"
          gateway      = "10.9.0.1"        },        {
          network_cidr = "10.6.0.83/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-17 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.26/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.153/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-18 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.27/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1007.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.12.0.190/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-19 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.28/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.91/24"
          gateway      = "10.6.0.1"        },        {
          network_cidr = "10.9.0.143/24"
          gateway      = "10.9.0.1"        }      ]
    },    sandbox-sandbox-vm-20 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.29/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        },        {
          bridge = local.sandbox_bridges.bridge-1003.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.162/24"
          gateway      = "10.10.0.1"        },        {
          network_cidr = "10.8.0.129/24"
          gateway      = "10.8.0.1"        }      ]
    },    sandbox-sandbox-vm-21 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.30/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1007.name
        },        {
          bridge = local.sandbox_bridges.bridge-1002.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.12.0.199/24"
          gateway      = "10.12.0.1"        },        {
          network_cidr = "10.7.0.115/24"
          gateway      = "10.7.0.1"        }      ]
    },    sandbox-sandbox-vm-22 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.31/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1003.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.8.0.134/24"
          gateway      = "10.8.0.1"        }      ]
    },    sandbox-sandbox-vm-23 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.32/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        },        {
          bridge = local.sandbox_bridges.bridge-1007.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        },        {
          bridge = local.sandbox_bridges.bridge-1003.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.188/24"
          gateway      = "10.11.0.1"        },        {
          network_cidr = "10.12.0.206/24"
          gateway      = "10.12.0.1"        },        {
          network_cidr = "10.9.0.156/24"
          gateway      = "10.9.0.1"        },        {
          network_cidr = "10.8.0.140/24"
          gateway      = "10.8.0.1"        }      ]
    },    sandbox-sandbox-vm-24 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.33/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.174/24"
          gateway      = null        },        {
          network_cidr = "10.9.0.158/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-25 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.34/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1002.name
        },        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.7.0.126/24"
          gateway      = null        },        {
          network_cidr = "10.6.0.110/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-26 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.35/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1002.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.7.0.129/24"
          gateway      = "10.7.0.1"        }      ]
    },    sandbox-sandbox-vm-27 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.36/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1003.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.8.0.149/24"
          gateway      = "10.8.0.1"        },        {
          network_cidr = "10.9.0.167/24"
          gateway      = "10.9.0.1"        }      ]
    },    sandbox-sandbox-vm-28 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.37/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1003.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        },        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.8.0.152/24"
          gateway      = null        },        {
          network_cidr = "10.9.0.170/24"
          gateway      = "10.9.0.1"        },        {
          network_cidr = "10.5.0.103/24"
          gateway      = "10.5.0.1"        }      ]
    },    sandbox-sandbox-vm-29 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.38/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1002.name
        },        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.7.0.138/24"
          gateway      = "10.7.0.1"        },        {
          network_cidr = "10.11.0.207/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-30 = {
      cpu               = 1
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.39/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.107/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-31 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.40/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1007.name
        },        {
          bridge = local.sandbox_bridges.bridge-1006.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.12.0.229/24"
          gateway      = null        },        {
          network_cidr = "10.11.0.213/24"
          gateway      = "10.11.0.1"        },        {
          network_cidr = "10.9.0.180/24"
          gateway      = "10.9.0.1"        }      ]
    },    sandbox-sandbox-vm-32 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.41/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1007.name
        },        {
          bridge = local.sandbox_bridges.bridge-1005.name
        },        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.12.0.232/24"
          gateway      = "10.12.0.1"        },        {
          network_cidr = "10.10.0.199/24"
          gateway      = "10.10.0.1"        },        {
          network_cidr = "10.11.0.217/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-33 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.42/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.116/24"
          gateway      = "10.5.0.1"        }      ]
    },    sandbox-sandbox-vm-34 = {
      cpu               = 2
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.43/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1002.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.7.0.153/24"
          gateway      = "10.7.0.1"        },        {
          network_cidr = "10.9.0.188/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-35 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.44/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1007.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.12.0.241/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-36 = {
      cpu               = 2
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.45/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.227/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-37 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.46/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.145/24"
          gateway      = "10.6.0.1"        }      ]
    },    sandbox-sandbox-vm-38 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.47/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1002.name
        },        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.7.0.165/24"
          gateway      = null        },        {
          network_cidr = "10.11.0.234/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-39 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.48/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.151/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-40 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.49/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.154/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-41 = {
      cpu               = 2
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.50/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        },        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.225/24"
          gateway      = null        },        {
          network_cidr = "10.5.0.141/24"
          gateway      = "10.5.0.1"        }      ]
    },    sandbox-sandbox-vm-42 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.51/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        },        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.143/24"
          gateway      = "10.5.0.1"        },        {
          network_cidr = "10.9.0.212/24"
          gateway      = "10.9.0.1"        },        {
          network_cidr = "10.11.0.247/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-43 = {
      cpu               = 2
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.52/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        },        {
          bridge = local.sandbox_bridges.bridge-1003.name
        },        {
          bridge = local.sandbox_bridges.bridge-1002.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.231/24"
          gateway      = "10.10.0.1"        },        {
          network_cidr = "10.8.0.198/24"
          gateway      = null        },        {
          network_cidr = "10.7.0.182/24"
          gateway      = "10.7.0.1"        }      ]
    },    sandbox-sandbox-vm-44 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.53/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        },        {
          bridge = local.sandbox_bridges.bridge-1003.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.234/24"
          gateway      = null        },        {
          network_cidr = "10.9.0.218/24"
          gateway      = "10.9.0.1"        },        {
          network_cidr = "10.8.0.202/24"
          gateway      = "10.8.0.1"        }      ]
    },    sandbox-sandbox-vm-45 = {
      cpu               = 1
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.54/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1007.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.12.0.31/24"
          gateway      = "10.12.0.1"        }      ]
    },    sandbox-sandbox-vm-46 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.55/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        },        {
          bridge = local.sandbox_bridges.bridge-1007.name
        },        {
          bridge = local.sandbox_bridges.bridge-1003.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.17/24"
          gateway      = "10.11.0.1"        },        {
          network_cidr = "10.12.0.35/24"
          gateway      = "10.12.0.1"        },        {
          network_cidr = "10.8.0.208/24"
          gateway      = "10.8.0.1"        }      ]
    },    sandbox-sandbox-vm-47 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.56/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1007.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.12.0.37/24"
          gateway      = "10.12.0.1"        }      ]
    },    sandbox-sandbox-vm-48 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.57/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1003.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        },        {
          bridge = local.sandbox_bridges.bridge-1002.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.8.0.212/24"
          gateway      = "10.8.0.1"        },        {
          network_cidr = "10.9.0.230/24"
          gateway      = null        },        {
          network_cidr = "10.7.0.197/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-49 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.58/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        },        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.249/24"
          gateway      = null        },        {
          network_cidr = "10.5.0.165/24"
          gateway      = "10.5.0.1"        }      ]
    },    sandbox-sandbox-vm-50 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.59/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        },        {
          bridge = local.sandbox_bridges.bridge-1007.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.12/24"
          gateway      = "10.10.0.1"        },        {
          network_cidr = "10.12.0.47/24"
          gateway      = "10.12.0.1"        }      ]
    },    sandbox-sandbox-vm-51 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.60/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1003.name
        },        {
          bridge = local.sandbox_bridges.bridge-1000.name
        },        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.8.0.221/24"
          gateway      = "10.8.0.1"        },        {
          network_cidr = "10.5.0.171/24"
          gateway      = "10.5.0.1"        },        {
          network_cidr = "10.6.0.189/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-52 = {
      cpu               = 1
      ram               = 2048
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.61/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.173/24"
          gateway      = "10.5.0.1"        },        {
          network_cidr = "10.9.0.242/24"
          gateway      = "10.9.0.1"        }      ]
    },    sandbox-sandbox-vm-53 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.62/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.193/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-54 = {
      cpu               = 2
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.63/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.41/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-55 = {
      cpu               = 2
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.64/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.199/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-56 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.65/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1007.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.12.0.64/24"
          gateway      = "10.12.0.1"        }      ]
    },    sandbox-sandbox-vm-57 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.66/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1004.name
        },        {
          bridge = local.sandbox_bridges.bridge-1007.name
        },        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.9.0.16/24"
          gateway      = null        },        {
          network_cidr = "10.12.0.68/24"
          gateway      = "10.12.0.1"        },        {
          network_cidr = "10.11.0.52/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-58 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.67/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1003.name
        },        {
          bridge = local.sandbox_bridges.bridge-1002.name
        },        {
          bridge = local.sandbox_bridges.bridge-1007.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.8.0.242/24"
          gateway      = null        },        {
          network_cidr = "10.7.0.226/24"
          gateway      = null        },        {
          network_cidr = "10.12.0.72/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-59 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.68/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1004.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.9.0.22/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-60 = {
      cpu               = 1
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.69/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        },        {
          bridge = local.sandbox_bridges.bridge-1003.name
        },        {
          bridge = local.sandbox_bridges.bridge-1005.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.59/24"
          gateway      = "10.11.0.1"        },        {
          network_cidr = "10.8.0.249/24"
          gateway      = "10.8.0.1"        },        {
          network_cidr = "10.10.0.44/24"
          gateway      = "10.10.0.1"        }      ]
    },    sandbox-sandbox-vm-61 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.70/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        },        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.62/24"
          gateway      = "10.11.0.1"        },        {
          network_cidr = "10.5.0.201/24"
          gateway      = "10.5.0.1"        }      ]
    },    sandbox-sandbox-vm-62 = {
      cpu               = 1
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.71/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        },        {
          bridge = local.sandbox_bridges.bridge-1005.name
        },        {
          bridge = local.sandbox_bridges.bridge-1002.name
        },        {
          bridge = local.sandbox_bridges.bridge-1003.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.203/24"
          gateway      = "10.5.0.1"        },        {
          network_cidr = "10.10.0.49/24"
          gateway      = null        },        {
          network_cidr = "10.7.0.239/24"
          gateway      = "10.7.0.1"        },        {
          network_cidr = "10.8.0.17/24"
          gateway      = "10.8.0.1"        }      ]
    },    sandbox-sandbox-vm-63 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.72/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.206/24"
          gateway      = "10.5.0.1"        },        {
          network_cidr = "10.9.0.35/24"
          gateway      = "10.9.0.1"        }      ]
    },    sandbox-sandbox-vm-64 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.73/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.226/24"
          gateway      = "10.6.0.1"        }      ]
    },    sandbox-sandbox-vm-65 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.74/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.74/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-66 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.75/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1003.name
        },        {
          bridge = local.sandbox_bridges.bridge-1007.name
        },        {
          bridge = local.sandbox_bridges.bridge-1001.name
        },        {
          bridge = local.sandbox_bridges.bridge-1002.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.8.0.26/24"
          gateway      = "10.8.0.1"        },        {
          network_cidr = "10.12.0.95/24"
          gateway      = "10.12.0.1"        },        {
          network_cidr = "10.6.0.234/24"
          gateway      = "10.6.0.1"        },        {
          network_cidr = "10.7.0.12/24"
          gateway      = "10.7.0.1"        }      ]
    },    sandbox-sandbox-vm-67 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.76/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        },        {
          bridge = local.sandbox_bridges.bridge-1003.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.63/24"
          gateway      = "10.10.0.1"        },        {
          network_cidr = "10.8.0.30/24"
          gateway      = "10.8.0.1"        }      ]
    },    sandbox-sandbox-vm-68 = {
      cpu               = 1
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.77/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.66/24"
          gateway      = "10.10.0.1"        }      ]
    },    sandbox-sandbox-vm-69 = {
      cpu               = 1
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.78/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1002.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.7.0.18/24"
          gateway      = "10.7.0.1"        }      ]
    },    sandbox-sandbox-vm-70 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.79/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1003.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.8.0.38/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-71 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.80/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1004.name
        },        {
          bridge = local.sandbox_bridges.bridge-1003.name
        },        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.9.0.58/24"
          gateway      = "10.9.0.1"        },        {
          network_cidr = "10.8.0.42/24"
          gateway      = "10.8.0.1"        },        {
          network_cidr = "10.5.0.232/24"
          gateway      = "10.5.0.1"        }      ]
    },    sandbox-sandbox-vm-72 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.81/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1002.name
        },        {
          bridge = local.sandbox_bridges.bridge-1007.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.7.0.27/24"
          gateway      = null        },        {
          network_cidr = "10.12.0.113/24"
          gateway      = "10.12.0.1"        }      ]
    },    sandbox-sandbox-vm-73 = {
      cpu               = 2
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.82/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.236/24"
          gateway      = "10.5.0.1"        }      ]
    },    sandbox-sandbox-vm-74 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.83/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1004.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.9.0.67/24"
          gateway      = "10.9.0.1"        }      ]
    },    sandbox-sandbox-vm-75 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.84/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.242/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-76 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.85/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        },        {
          bridge = local.sandbox_bridges.bridge-1001.name
        },        {
          bridge = local.sandbox_bridges.bridge-1007.name
        },        {
          bridge = local.sandbox_bridges.bridge-1003.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.90/24"
          gateway      = "10.10.0.1"        },        {
          network_cidr = "10.6.0.23/24"
          gateway      = "10.6.0.1"        },        {
          network_cidr = "10.12.0.126/24"
          gateway      = null        },        {
          network_cidr = "10.8.0.59/24"
          gateway      = "10.8.0.1"        }      ]
    },    sandbox-sandbox-vm-77 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.86/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1007.name
        },        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.12.0.127/24"
          gateway      = null        },        {
          network_cidr = "10.11.0.111/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-78 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.87/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1004.name
        },        {
          bridge = local.sandbox_bridges.bridge-1005.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.9.0.79/24"
          gateway      = "10.9.0.1"        },        {
          network_cidr = "10.10.0.97/24"
          gateway      = "10.10.0.1"        }      ]
    },    sandbox-sandbox-vm-79 = {
      cpu               = 1
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.88/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        },        {
          bridge = local.sandbox_bridges.bridge-1005.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.116/24"
          gateway      = null        },        {
          network_cidr = "10.10.0.100/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-80 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.89/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.102/24"
          gateway      = "10.10.0.1"        }      ]
    },    sandbox-sandbox-vm-81 = {
      cpu               = 2
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.90/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.105/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-82 = {
      cpu               = 1
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.91/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1007.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        },        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.12.0.142/24"
          gateway      = "10.12.0.1"        },        {
          network_cidr = "10.9.0.92/24"
          gateway      = "10.9.0.1"        },        {
          network_cidr = "10.5.0.25/24"
          gateway      = "10.5.0.1"        }      ]
    },    sandbox-sandbox-vm-83 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.92/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.43/24"
          gateway      = null        },        {
          network_cidr = "10.9.0.95/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-84 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.93/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.46/24"
          gateway      = "10.6.0.1"        }      ]
    },    sandbox-sandbox-vm-85 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.94/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.134/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-86 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.95/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.137/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-87 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.96/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1002.name
        },        {
          bridge = local.sandbox_bridges.bridge-1005.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.7.0.72/24"
          gateway      = "10.7.0.1"        },        {
          network_cidr = "10.10.0.124/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-88 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.97/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1007.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.12.0.160/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-89 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.98/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1005.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.10.0.129/24"
          gateway      = "10.10.0.1"        }      ]
    },    sandbox-sandbox-vm-90 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.99/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        },        {
          bridge = local.sandbox_bridges.bridge-1004.name
        },        {
          bridge = local.sandbox_bridges.bridge-1000.name
        },        {
          bridge = local.sandbox_bridges.bridge-1003.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.64/24"
          gateway      = "10.6.0.1"        },        {
          network_cidr = "10.9.0.116/24"
          gateway      = "10.9.0.1"        },        {
          network_cidr = "10.5.0.49/24"
          gateway      = null        },        {
          network_cidr = "10.8.0.101/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-91 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.100/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1003.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.8.0.101/24"
          gateway      = "10.8.0.1"        }      ]
    },    sandbox-sandbox-vm-92 = {
      cpu               = 2
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.101/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1003.name
        },        {
          bridge = local.sandbox_bridges.bridge-1001.name
        },        {
          bridge = local.sandbox_bridges.bridge-1000.name
        },        {
          bridge = local.sandbox_bridges.bridge-1007.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.8.0.104/24"
          gateway      = "10.8.0.1"        },        {
          network_cidr = "10.6.0.71/24"
          gateway      = "10.6.0.1"        },        {
          network_cidr = "10.5.0.55/24"
          gateway      = "10.5.0.1"        },        {
          network_cidr = "10.12.0.175/24"
          gateway      = "10.12.0.1"        }      ]
    },    sandbox-sandbox-vm-93 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.102/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.73/24"
          gateway      = "10.6.0.1"        }      ]
    },    sandbox-sandbox-vm-94 = {
      cpu               = 2
      ram               = 1024
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.103/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1002.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.7.0.93/24"
          gateway      = "10.7.0.1"        }      ]
    },    sandbox-sandbox-vm-95 = {
      cpu               = 1
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.104/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        },        {
          bridge = local.sandbox_bridges.bridge-1001.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.62/24"
          gateway      = null        },        {
          network_cidr = "10.6.0.80/24"
          gateway      = "10.6.0.1"        }      ]
    },    sandbox-sandbox-vm-96 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.105/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.11.0.167/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-97 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.106/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.68/24"
          gateway      = null        }      ]
    },    sandbox-sandbox-vm-98 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.107/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1001.name
        },        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.6.0.88/24"
          gateway      = "10.6.0.1"        },        {
          network_cidr = "10.11.0.174/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-99 = {
      cpu               = 2
      ram               = 256
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.108/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1003.name
        },        {
          bridge = local.sandbox_bridges.bridge-1006.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.8.0.125/24"
          gateway      = "10.8.0.1"        },        {
          network_cidr = "10.11.0.177/24"
          gateway      = "10.11.0.1"        }      ]
    },    sandbox-sandbox-vm-100 = {
      cpu               = 2
      ram               = 512
      disk_file_id      = local.platform.os_images.debian.file_id
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "10.3.0.109/24"

      network_devices = [
        {
          bridge = local.sandbox_bridges.bridge-1000.name
        },        {
          bridge = local.sandbox_bridges.bridge-1002.name
        }      ]

      ip_configs = [
        {
          network_cidr = "10.5.0.77/24"
          gateway      = "10.5.0.1"        },        {
          network_cidr = "10.7.0.112/24"
          gateway      = "10.7.0.1"        }      ]
    }  }
}
