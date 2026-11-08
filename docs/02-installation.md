# 2. Installation

This guide takes a node from "Proxmox VE is installed and root has a password" to a running platform with devboxes.

- **The controller.** The node itself acts as the controller: the vault, the CA and the OpenTofu state live in its checkout, `/root/app`. To control it from a laptop instead, see [Controller on a laptop](#controller-on-a-laptop).
- **Production and development.** They differ only in the node's hostname: `worms` or `worms-dev` ([1](01-components.md#ansible)).

## 1. Requirements

**Hardware**
- A CPU with `x86-64-v3` (AVX2) and VMX or SVM. Team VMs use CPU type `x86-64-v3`.
- At least 32 GB RAM; 48 GB to also run a round ([1](01-components.md#resources)).
- About 400 GB disk on ZFS.

```sh
/lib64/ld-linux-x86-64.so.2 --help | grep x86-64-v3     # "supported"
grep -m1 -owE 'vmx|svm' /proc/cpuinfo
cat /sys/devices/system/cpu/smt/control                  # SMT on means L1TF/MDS between hyperthreads (7 Security)
```

**Network**
- **Hostname.** The node is called `worms` (production) or `worms-dev`. Any other name is refused ([`session.py`](../src/worms_cli/session.py)).
- **Public interface.** `vmbr0` holds the public address and the default route (`host_wan_interface`).
- **Public name.** The DNS name in `worms_environments` points to the node.
- **Address ranges.** Nothing on the node uses 10.2.0.0–10.13.255.255, 10.64.0.0/10 or 10.128.0.0/10.
- **Outbound, during builds.** HTTP(S) and DNS to the internet. The DNS VM forwards to `130.149.7.7` (`setup_dns` defaults); change that outside TU Berlin.
- **Outbound, always.** Team VMs need 141.23.117.34:443 (the LLM endpoint).
- **Inbound, production.** Teams use 22 (the devbox SSH gateway) and 443 (the dashboard). Operators use 2222 (SSH) and 8006 (web UI). On worms-dev the two SSH ports are swapped.

## 2. Prepare the node

### 2.1 Packages

Proxmox VE 9 (Debian 13). Switch to the no-subscription repository and upgrade, then install:

```sh
apt install -y git rsync make tmux python3-venv ansible libguestfs-tools curl gnupg
# OpenTofu from its Debian repository (https://opentofu.org/docs/intro/install/deb/), then:
tofu version; ansible --version | head -1
ansible-galaxy collection list | grep -E 'ansible.posix|community.crypto|community.postgresql'   # all three
```

- **Use the `ansible` package.** `ansible-core` alone lacks the collections and `ansible-vault`. Without `ansible-vault` the vault is written in plain text ([`secrets.py`](../src/worms_cli/secrets.py)).
- **Python.** The CLI needs Python ≥ 3.12.

### 2.2 Nesting off, then reboot

The build writes this file as well (role `host_network`), but it only takes effect when `kvm_intel`/`kvm_amd` loads:

```sh
printf 'options kvm_intel nested=0\noptions kvm_amd nested=0\n' > /etc/modprobe.d/worms-no-nesting.conf
reboot
cat /sys/module/kvm_intel/parameters/nested      # N
```

### 2.3 Storage

Nothing in the code creates these:

```sh
pvesm set local --content iso,vztmpl,backup,snippets,import
pvesm add zfspool local-zfs-sandbox --pool rpool --content images,rootdir --sparse 0
```

| Storage | Holds | Where in the code |
|---|---|---|
| `local` (snippets) | cloud-init files | [`modules/cloud_init`](../opentofu/modules/cloud_init/) |
| `local` (import) | OS images | [`modules/os_images`](../opentofu/modules/os_images/) |
| `local-zfs` | infrastructure disks | hardcoded |
| `local-zfs-sandbox` | team disks, thick | hardcoded |

### 2.4 Operators' SSH port and the web UI

- **Why.** On production, port 22 belongs to the teams: the build forwards it to the SSH gateway. The node's sshd therefore also listens on `operator_ssh_port` (2222).
- **The guard.** The build refuses to forward 22 while nothing listens on 2222.
- **What changes after the build.** The firewall step sets the node's input policy to DROP. From then on only the node's subnet and the rules below may open connections to it. Add `--source` to restrict them further (finding S22).

```sh
printf 'Port 22\nPort 2222\n' > /etc/ssh/sshd_config.d/worms-ports.conf && sshd -t && systemctl reload ssh
pvesh create /nodes/$(hostname)/firewall/rules --type in --action ACCEPT --proto tcp --dport 2222 --enable 1 --comment "operators: SSH"
pvesh create /nodes/$(hostname)/firewall/rules --type in --action ACCEPT --proto tcp --dport 8006 --enable 1 --comment "operators: web UI"
```

Keep Proxmox's own web UI certificate. The orchestrator trusts only `/etc/pve/pve-root-ca.pem`.

### 2.5 Repository, images and CLI

```sh
git clone --branch main <repository> /root/app && cd /root/app
find opentofu -name '*.auto.tfvars'               # must print nothing: worms refuses to run otherwise
cd opentofu/modules/os_images && make images/debian-13-generic-amd64-with-guest-agent.qcow2 \
  images/resolute-server-cloudimg-amd64-with-guest-agent.qcow2 && cd /root/app
python3 -m venv .venv-test && .venv-test/bin/pip install -q -r tests/requirements.txt
echo 'worms() { /root/app/.venv-test/bin/python /root/app/src/manage.py "$@"; }' >> ~/.bashrc && . ~/.bashrc
tmux new -s worms                                 # builds take about an hour
```

- **Images.** They are git-ignored, so a fresh clone has none.
- **The venv.** It provides Django, psycopg2 and proxmoxer for the database and status commands.
- **Config.** The node needs no `~/.worms/config`.

## 3. Secrets and tokens

Start with a fresh vault. Never copy another node's: its tokens, WORMS key and state belong to that node.

```sh
worms secrets issue proxmox-token        # WormsProvision on /        (OpenTofu, the round agent)
worms secrets issue orchestrator-token   # WormsSandboxPower on /pool/sandbox (sandbox VM power)
worms secrets issue topology-token       # WormsTopology on /         (topology page, worms status)
worms secrets                            # generates everything else; shows states, never values
worms node trust                         # root on the node accepts the WORMS key
```

What each token may do: [7 Security](07-security.md#capabilities).

## 4. Build

```sh
worms build
echo "exit $?"
```

`worms build` is idempotent. It runs these steps in order ([3](03-manage-py.md#worms-build)):
1. secrets, init, platform, firewall, host network
2. with the devboxes shut down:
   1. under the internet window:
      - the private-internet and infrastructure VMs
      - the 14 configure playbooks
   2. after the window closes:
      - offline mode
      - the `clean` snapshot of the 11 service VMs

- **Success.** The run ends with `== setup complete (internet access denied) ==` and exit 0.
- **Failure.** The build stops at the step that failed, closes the internet window and starts the devboxes again. Fix the cause and run it again.

Then check the result and create the dashboard admin:

```sh
worms build ping                # every VM: SUCCESS
worms status                    # firewall on, VMs running, internet denied, the competition
worms secrets check             # each token: ok, exactly its rights
worms simulate                  # end-to-end checks; the sandbox checks FAIL until a round has built one
worms admin reset-password      # one-time password for "admin", printed once
```

The checks below run on the node:

```sh
test -e /run/worms/internet-allowed && echo OPEN || echo denied
systemctl is-active proxmox-firewall worms-host-firewall worms-firewall-watchdog.timer worms-mirror worms-round-agent worms-audit-gateway.timer
pvesh get /cluster/firewall/options --output-format json       # "enable":1, "policy_in":"DROP"
```

**Back up off the node:**
- the vault and `ansible/.vault_pass`
- `ansible/pki/worms/`
- `opentofu/stacks/*/terraform.tfstate`

Without them the platform has to be rebuilt. Nothing backs up the database ([6](06-background-processes.md#backups)).

## 5. Teams, devboxes, rounds

1. **Admin and teams.** Open `https://<public name>/`, sign in as `admin` and set a password. Create the teams under **Staff → Teams**, then the students under **Staff → Users** (one at a time, or a CSV). Hand out the one-time passwords shown.
2. **Devboxes.** Run `worms devbox provision`. It takes about 17 minutes for 3 teams; follow it with `tail -F /var/log/worms/jobs/latest.log` on the node. Teams then add SSH keys on their profile ([4](04-dev-environment.md)).
3. **Rounds.**
   - Create the round on **Staff → Rounds**. Its gameplan can follow later, until the round's first attempt starts ([5](05-live-environment.md#running-a-round)).
   - Commit a gameplan under `gameplans/` and deploy it with `worms build provision`, or with `worms build` when nothing else changed. This syncs it to the orchestrator and the node.
   - Within 10 minutes it shows as valid on **Staff → Rounds**. Choose it on the round's page.

## Troubleshooting

| Symptom | Do |
|---|---|
| `root on this node does not accept the WORMS key`, or tofu fails to authenticate over SSH | `worms node trust` |
| `the vault lacks ...`, `there is no vault` | `worms build secrets` |
| `proxmox-token: MISSING` | `worms secrets issue <name>` |
| `... overrides the vault's token for OpenTofu` | Delete that `*.auto.tfvars` |
| `PROXMOX_VE_SSH_... is set in your environment` | `unset` it |
| tofu cannot find a `.qcow2`; errors about `snippets`, `import`, `local-zfs-sandbox` | §2.5, §2.3 |
| New SSH or 8006 connections time out after the firewall step | §2.4, from an open session or the console |
| `got timeout`, `zfs create` timed out, storage lock, `Timeout (12s) waiting for privilege escalation prompt` | Run it again (a busy node) |
| `Warning: sch_htb: quantum of class ... is big` when a devbox starts | Harmless: the kernel's note on the devbox NICs' rate limit. The build starts the devboxes at its end whether it succeeded or not: look for `== setup complete` or the first `fatal:` above |
| `unknown job kind: 'gameplans'` in the orchestrator's journal | The node still runs the old round agent: the build stopped before its playbook. Run `worms build` again |
| `dataset already exists` | A leftover volume: `pvesm free <storage>:<volume>` |
| `known_hosts has lines OpenTofu's Proxmox provider cannot read` | Delete the lines it names from `/root/.ssh/known_hosts` |
| `Run Django migrations` fails, output hidden | `worms build provision -- --limit orchestrator -e hide_secrets=false` (prints secrets) |
| `!!! INTERNET ACCESS IS STILL ALLOWED` | `worms build internet-deny` |
| `warning: no snapshot of ...` | `worms build snapshot-services` before any devbox exists; later `worms build rebuild-services` |
| `stack devbox has outputs ... but no ansible_hosts` | A devbox apply stopped half way: see the job log, then `worms devbox provision` |
| Rerunning one playbook hangs on `apt`/`pip` | The internet window is closed: add `--skip-tags packages -e internet_enabled=false` |
| `worms-audit-gateway.service` fails every minute | Expected until `worms devbox provision`: there is no SSH gateway yet |
| The orchestrator gets `CERTIFICATE_VERIFY_FAILED` from Proxmox | The web UI certificate was replaced: put Proxmox's own back |

## Controller on a laptop

The laptop holds the checkout, the vault, the CA and the state of `platform`, `firewall`, `infrastructure/*` and `private_internet`. The node keeps the sandbox and devbox state in `/opt/worms`.

```sh
echo 'WORMS_SSH=root@<public name>' >> ~/.worms/config   # its SSH port goes in ~/.ssh/config
worms secrets && worms node trust                           # then `worms secrets issue ...`, `worms build`
```

- **Requirements.** Python ≥ 3.12, OpenTofu, Ansible, OpenSSH and rsync on the laptop. The node's SSH port and port 8006 must be reachable from it.
- **Moving an existing controller to the laptop:**
  1. Copy the vault and its key, `ansible/pki/worms/` and those five states to the same paths in the laptop's checkout.
  2. Run `worms node trust`.
  3. Run `worms build provision`.
