# Secrets

All passwords, keys and tokens live in an encrypted Ansible vault, never in
version control:

```
cd ansible
cp group_vars/vault.yml.example group_vars/vault.yml
# fill in strong, unique values, then:
ansible-vault encrypt group_vars/vault.yml
```

Run every playbook with `--ask-vault-pass` (or `--vault-password-file`).
`group_vars/all.yml` references these as `{{ vault_* }}`; the Django hosts and
the database receive their credentials through root-owned `0600` files, so
secrets are passed securely at initialization time and never land in a
world-readable location.

# Init

- OpenTofu sets up:
    - platform (networks, pools, firewall security groups)
    - infrastructure:
        - persistent: database, dashboard, orchestrator (each on its own
          maintenance IP: 10.3.0.16 / .18 / .19)
        - ephemeral: scoring, logging
- Ansible sets up:
    - DNS
    - CA
    - DB (creates least-privilege users, TLS-only)
    - Logging
    - Scoring
    - Dashboard + Orchestrator (Django hosts; each started with its own
      settings module: `config.settings.dashboard` / `config.settings.orchestrator`)
- Django migrates DB tables (as the `django_migrator` role)
- Ansible sets up:
    - DB (sets per-user least-privilege table permissions)

# Components & least-privilege communication

- Dashboard (public-facing later) → database:5432 (read-only role +
  INSERT on uploads), and → orchestrator:443.
- Orchestrator → Proxmox API:8006 (proxmoxer, scoped API token) and
  → database:5432 (writes virtual_machines / worm_deployment_plans /
  worm_deployments, reads the rest).
- Database accepts :5432 only from the named service IPs; each DB role may
  connect only from its own VM (`/32` in `pg_hba`, `scram-sha-256`, TLS).

The firewall security groups in `opentofu/stacks/platform/main.tf` enforce the
network side; the PostgreSQL roles/grants in the `setup_db_*` roles enforce the
data side.

# Round start

- Python generates sandbox, secrets
- Add round + sandbox (including secrets) to DB
- OpenTofu sets up:
    - sandbox
    - monitor VM
- Python sets up "mirroring" of vmbrs to monitor network
- Python generates worm deployment (adds to DB)
- Ansible sets up:
    - secrets in VM
    - worms in VM

# Round end

- cd opentofu/stacks/sandbox && tofu destroy