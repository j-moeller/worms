output "platform" {
  value = {
    os_images  = module.os_images
    cloud_init = module.cloud_init
    networks   = module.networks
    pools      = proxmox_virtual_environment_pool.pools
  }
}