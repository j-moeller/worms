# Ansible roles

One folder per component. Every folder is on `roles_path` (`ansible.cfg`), so
playbooks refer to roles by name only (`- role: nginx_tls`) and role names
stay unique across folders.

| Folder              | Roles                                                         | Playbook                       | Runs on                         |
|---------------------|---------------------------------------------------------------|--------------------------------|---------------------------------|
| `host/`             | `host_network` (nftables, sysctls, internet gate, traffic mirror) | SetupHostNetwork.yml, Internet.yml | Proxmox host (`proxmox_host`) |
| `pki/`              | `ca_create` (internal root CA), `ca_distribute` (trust store) | CreateCA.yml, DistributeCA.yml, SetupSandbox.yml | controller, `tls_hosts`, `sandbox` |
| `web/`              | `nginx_tls`, `nginx_tls_django` (TLS reverse proxies)         | ConfigureNginx*.yml            | `nginx_hosts`, `django_hosts`   |
| `database/`         | `setup_db_base`, `setup_db_permissions`                       | SetupDbAndDashboard.yml        | `database_hosts`                |
| `applications/`     | `django_app` (dashboard, orchestrator), `sandbox_api`, `network_monitor` (Zeek + zeek-shipper) | SetupDbAndDashboard.yml, SetupSandboxApi.yml, SetupNetworkMonitor.yml | `django_hosts`, `sandbox_api_hosts`, `network_monitor_hosts` |
| `dns/`              | `setup_dns`                                                   | SetupDNS.yml                   | `dns_hosts`                     |
| `audit/`            | `wazuh_server` (Wazuh all-in-one), `auditd` (auditd + rsyslog forwarding to Wazuh) | SetupAudit.yml | `wazuh_hosts`, `audit_hosts` |
| `private_internet/` | `apt_client`, `apt_cache`, `cve_search`, `devpi`, `docker_registry`, `manpages`, `ntp`, `wikipedia` | SetupPrivateInternet.yml | `private_internet` |

`database/setup_db_dashboard` is not used by any playbook (superseded by
`applications/django_app`).

A new role goes into the folder of its component; a new folder must also be
added to `roles_path` in `ansible.cfg`.
