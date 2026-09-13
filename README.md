# Init

- OpenTofu sets up:
    - platform
    - infrastructure (ephemeral, persistent)
- Ansible sets up:
    - DNS
    - CA
    - DB (creates users)
    - Logging
    - Scoring
    - Dashboard
- Django migrates DB tables
- Ansible sets up:
    - DB (sets user table permissions)

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