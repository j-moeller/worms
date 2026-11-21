# Ansible roles

One folder per component. Every folder is on `roles_path` (`ansible.cfg`), so
playbooks refer to roles by name only (`- role: nginx_tls`) and role names
stay unique across folders.

| Folder              | Roles                                                         | Playbook                       | Runs on                         |
|---------------------|---------------------------------------------------------------|--------------------------------|---------------------------------|
| `host/`             | `host_network` (nftables, sysctls, internet gate, traffic mirror) | SetupHostNetwork.yml, Internet.yml | Proxmox host (`proxmox_host`) |
| `pki/`              | `ca_create` (internal root CA), `ca_distribute` (trust store) | Site.yml (phases 1, 3), SetupSandbox.yml, SetupDevbox.yml | controller, `tls_hosts`, `sandbox` |
| `web/`              | `nginx_tls`, `nginx_tls_django` (TLS reverse proxies)         | ConfigureNginx*.yml            | `nginx_hosts`, `django_hosts`   |
| `database/`         | `setup_db_base`, `setup_db_permissions`                       | Site.yml (phases 3, 4)         | `database_hosts`                |
| `applications/`     | `django_app` (dashboard, orchestrator), `sandbox_api`, `network_monitor` (Zeek + zeek-shipper) | Site.yml (phases 3, 4) | `django_hosts`, `sandbox_api_hosts`, `network_monitor_hosts` |
| `dns/`              | `setup_dns`                                                   | Site.yml (phase 2)             | `dns_hosts`                     |
| `system/`           | `apt_updates` (no automatic apt runs; package updates while online) | Site.yml (phase 3) | every infrastructure VM |
| `audit/`            | `syslog_server` (central rsyslog server), `auditd` (auditd + rsyslog forwarding to it) | Site.yml (phase 3) | `syslog_hosts`, `audit_hosts` |
| `private_internet/` | `apt_client`, `apt_cache`, `cve_search`, `devpi`, `docker_registry`, `manpages`, `ntp`, `wikipedia` | Site.yml (phase 3) | `private_internet` |

A new role goes into the folder of its component; a new folder must also be
added to `roles_path` in `ansible.cfg`.
