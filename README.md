# WORMS platform

WORMS is a competition platform for self-spreading programs ("worms"), running on one Proxmox host.

- **Teams** sign in to a dashboard and develop against their own three VMs (the **devbox**).
- **Uploads.** Each team uploads its worm as a zip.
- **Rounds.** Every team's worm runs, one team after another, in a **sandbox** built from a Python **gameplan**.
- **Containment.** Sandbox and devbox VMs are assumed malicious and rooted. They never reach the internet, the infrastructure or each other ([`docs/IMPORTANT`](docs/IMPORTANT)).

Built with OpenTofu (`opentofu/`), Ansible (`ansible/`), the `worms` CLI, the round agent and the gameplan framework in Python (`src/`, `scripts/`, `gameplans/`), and a Django dashboard (`projects/dashboard/`).

## Documentation

| Doc | For |
|---|---|
| [1 Components](docs/01-components.md) | Everyone: what runs where, networks, stacks, repository layout |
| [2 Installation](docs/02-installation.md) | Operators: from a bare Proxmox host to a running platform |
| [3 manage.py](docs/03-manage-py.md) | Operators: the `worms` CLI, Django's management commands, testing, logs |
| [4 Dev environment](docs/04-dev-environment.md) | Developers: devboxes, the SSH gateway, dev uploads |
| [5 Live environment](docs/05-live-environment.md) | Developers, gameplan authors: rounds, attempts, the round agent, writing a gameplan |
| [6 Background processes](docs/06-background-processes.md) | Operators: every service, timer and loop |
| [7 Security](docs/07-security.md) | Everyone: threat models, privileges, accounts, secrets, firewalls, findings |
| [8 User guide](docs/08-user-guide.md) | Teams: the dashboard, the worm contract, the APIs |
| [9 VM configuration](docs/09-vm-configuration.md) | Operators: what Ansible runs on each VM, and by which command |
| [10 SSH gateway](docs/10-ssh-gateway.md) | Operators: how teams reach their devboxes, what is stored where, what makes it safe |
| [11 Orchestrator](docs/11-orchestrator.md) | Developers, operators: the conductor, the processor, gameplans, VM resets |

## Quick start

On the host (details: [2 Installation](docs/02-installation.md)):

```sh
worms() { /root/app/.venv-test/bin/python /root/app/src/manage.py "$@"; }
worms secret issue proxmox-token          # OpenTofu and the round agent
worms secret issue orchestrator-token     # sandbox VM power, for rounds
worms secret issue topology-token         # the topology page and `worms status`
worms secret                              # generates everything else
worms init                           # root on the host accepts the WORMS key
worms apply --all                          # = make: everything, idempotent
worms user password                 # one-time password for `admin`
```

`worms --help` lists the commands by purpose, and `worms apply --help` the components and what each needs.

## Tests

```sh
worms test          # offline suite: no Proxmox needed
worms test --live      # on the host: the deployed platform end to end, PASS/FAIL report
```
