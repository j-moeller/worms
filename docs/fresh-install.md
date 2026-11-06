# Fresh install on `worms-pve`

From "Proxmox VE is installed and root has a password" to a running platform with devboxes for three teams. Everything runs on the node as root; the node is also the controller (the vault, the CA and the OpenTofu state live in `/root/app`). Written on 2026-11-06 from the code and from `worms-dev`, which was set up by hand. **A build on an empty node has never run** (`docs/status.md` §2). *Not verified* marks what comes from reading the code only.

## 1. Before you start

### Hardware

```sh
/lib64/ld-linux-x86-64.so.2 --help | grep x86-64-v3   # "x86-64-v3 (supported, searched)": required
grep -m1 -ow avx2 /proc/cpuinfo                        # avx2: required
grep -m1 -owE 'vmx|svm' /proc/cpuinfo                  # vmx (Intel) or svm (AMD): required
cat /sys/module/kvm_intel/parameters/nested            # AMD: kvm_amd. Any value now; step 2.3 makes it N (AMD: 0)
cat /sys/devices/system/cpu/smt/control; grep -l 'SMT vulnerable' /sys/devices/system/cpu/vulnerabilities/*   # write both down
pvesm status                                           # local (dir) and local-zfs (zfspool), active
free -g; zpool list rpool                              # see the table below
```

Team VMs have CPU type `x86-64-v3` (`opentofu/stacks/devbox/main.tf:185`) and do not start without AVX2. SMT `on` with `mds` and `l1tf` listed (as on worms-dev) means L1TF/MDS between hyperthreads; turning SMT off is decision D-15. `local-zfs` is hardcoded (`opentofu/modules/infrastructure_vm/main.tf:86`): without it, reinstall with ZFS.

| Three teams | RAM (ceilings) | Disk |
|---|---|---|
| 8 infrastructure VMs | 8 GB | up to 170 GB, thin |
| 7 private-internet VMs | 10.5 GB | up to 123 GB, thin |
| 3 devboxes + SSH gateway (13 VMs) | 6.5 GB | 60 GB thick, 5 GB thin |
| A default round, on top | +4.5 GB | +45 GB, thick |

Ballooning reclaims unused guest memory, so worms-dev runs all of this, without a round, in 32 GB (24 GB used, 6 GB free, 3.2 GB ZFS cache). **Minimum 32 GB; 48 GB to also run a round.** Disk: worms-dev uses 125 GB of 472 GB; with every thin disk full it would need about 400 GB. CPU: worms-dev has 4 cores, 8 threads.

### Network

| What | Required | Check |
|---|---|---|
| Name | The node is called `worms-pve`. The OpenTofu default (`opentofu/shared/shared-variables.tf:4`) and `dashboard_public_names` (`ansible/group_vars/all.yml:31`) depend on it | `hostname; ls /etc/pve/nodes`: `worms-pve` only |
| Public bridge | `vmbr0` holds the public address and the default route, as the installer sets it up. The name is hardcoded (`host_wan_interface`, `ansible/roles/host/host_network/defaults/main.yml:7`) | `ip -br addr show vmbr0; ip route show default` |
| DNS | `worms.mlsec.tu-berlin.de` points to the node. On 2026-11-06 it resolved to `141.23.117.59`, which answered on 22 and 8006 | `getent hosts worms.mlsec.tu-berlin.de`, on the node and a laptop |
| Free ranges | Nothing on the node or its networks uses 10.2.0.0–10.13.255.255, 10.64.0.0/10 or 10.128.0.0/10 (`opentofu/shared/shared-variables.tf`) | `ip -br addr; ip route` |
| Resolver | During builds the DNS VM forwards to `130.149.7.7`, TU Berlin's (`ansible/roles/dns/setup_dns/defaults/main.yml:7`) | Change that line outside TU Berlin's network |

The network in front of the node must let these through (a timeout from outside means it does not). Outbound, the node needs HTTP(S) and DNS (apt, PyPI, the OpenTofu registry, GitHub, the git server); team VMs need `teachllm.mlsec.tu-berlin.de` (141.23.117.34) on 443.

| Inbound port | From | Goes to |
|---|---|---|
| 443/tcp | everyone | host DNAT to the dashboard |
| 2222/tcp | the teams | host DNAT to the devbox SSH gateway |
| 22/tcp, 8006/tcp | operators | the node: SSH, Proxmox web UI |

## 2. Prepare the node

### 2.1 APT repositories and upgrade

Log in as root with the password (`ssh root@<node>`), then:

```sh
pveversion        # pve-manager/9.x. These steps are for 9 (Debian 13 trixie), as on worms-dev (9.2.25)
cd /etc/apt/sources.list.d; ls
for f in pve-enterprise.sources ceph.sources; do [ -e "$f" ] && mv "$f" "/root/$f.disabled"; done
cat > /etc/apt/sources.list.d/proxmox.sources <<'EOF'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
apt update && apt full-upgrade -y
```

### 2.2 Packages, OpenTofu, Ansible

The same packages worms-dev got by hand. OpenTofu comes from its own Debian repository, as on worms-dev:

```sh
apt install -y git rsync make tmux python3-venv ansible libguestfs-tools apt-transport-https ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://get.opentofu.org/opentofu.gpg > /etc/apt/keyrings/opentofu.gpg
curl -fsSL https://packages.opentofu.org/opentofu/tofu/gpgkey | gpg --no-tty --batch --dearmor -o /etc/apt/keyrings/opentofu-repo.gpg
chmod a+r /etc/apt/keyrings/opentofu.gpg /etc/apt/keyrings/opentofu-repo.gpg
echo 'deb [signed-by=/etc/apt/keyrings/opentofu.gpg,/etc/apt/keyrings/opentofu-repo.gpg] https://packages.opentofu.org/opentofu/tofu/any/ any main' > /etc/apt/sources.list.d/opentofu.list
apt update && apt install -y tofu
tofu version                    # worms-dev: v1.13.2
ansible --version | head -1     # worms-dev: core 2.19.11 (Debian package ansible 12.0.0)
ansible-galaxy collection list 2>/dev/null | grep -E 'ansible.posix|community.crypto|community.postgresql'   # all three
dpkg -l proxmox-firewall | tail -1   # "ii". If missing: apt install -y proxmox-firewall (not verified)
```

The Debian package `ansible` brings the three collections the playbooks need; `ansible-core` alone does not. Install it before the first `worms secrets issue`: without `ansible-vault`, the vault is written in plain text (`src/worms_cli/secrets.py:180-183`). `libguestfs-tools` brings `virt-customize` for step 2.7.

### 2.3 Nesting off, then reboot

The build writes the same file (role `host_network`), but it takes effect only when `kvm_intel` or `kvm_amd` loads. Before any VM exists a reboot costs nothing, and it starts the upgraded kernel.

```sh
printf 'options kvm_intel nested=0\noptions kvm_amd nested=0\n' > /etc/modprobe.d/worms-no-nesting.conf
reboot
cat /sys/module/kvm_intel/parameters/nested     # after the reboot: N  (AMD: kvm_amd, 0)
```

### 2.4 Storage

```sh
pvesm set local --content iso,vztmpl,backup,snippets,import
pvesm add zfspool local-zfs-sandbox --pool rpool --content images,rootdir --sparse 0
pvesm status                    # local, local-zfs, local-zfs-sandbox: active
```

Nothing in the code creates these; worms-dev has them by hand. `snippets` holds the cloud-init files and the hookscript (`opentofu/modules/cloud_init/main.tf:40`, `opentofu/stacks/platform/main.tf:51`), `import` the OS images (`opentofu/modules/os_images/main.tf:2`). `local-zfs-sandbox` holds the devbox and sandbox disks, thick (`opentofu/stacks/devbox/main.tf:62`, `opentofu/stacks/sandbox/main.tf:63`). On worms-dev it is `zfspool`, pool `rpool`, `content rootdir,images`, `sparse 0`, `mountpoint /rpool` (Proxmox adds the last line itself).

### 2.5 Keep SSH and the web UI reachable

The build turns on the Proxmox firewall with input policy DROP (`opentofu/stacks/firewall/main.tf:29-50`). From then on only the node's own subnet (`pve-firewall localnet`; on worms-dev 141.23.117.32/27) may open new connections to 22 and 8006; open sessions survive. worms-dev has two rules for this, added by hand. To close finding S22, add `--source <your networks>` to both; a wrong source locks you out once the build has run.

```sh
pve-firewall localnet
pvesh create /nodes/$(hostname)/firewall/rules --type in --action ACCEPT --macro SSH --enable 1 --comment "operators: SSH"
pvesh create /nodes/$(hostname)/firewall/rules --type in --action ACCEPT --proto tcp --dport 8006 --enable 1 --comment "operators: web UI"
pvesh get /nodes/$(hostname)/firewall/rules
```

### 2.6 The repository

Both remotes are private. Over HTTPS git asks for a user and an access token; do not put the token in the URL, it would end up in `.git/config`. Only what is pushed to `main` gets deployed.

```sh
git clone --branch main https://git.tu-berlin.de/mlsec/teach/winter-2026/worms/worms-platform.git /root/app
# or: git clone --branch main git@github.com:j-moeller/worms.git /root/app   (needs a key GitHub accepts)
cd /root/app && git log -1 --oneline
grep -A3 'variable "proxmox_instance"' opentofu/shared/shared-variables.tf   # default = "worms-pve"
find opentofu -name '*.auto.tfvars'                                         # prints nothing
```

### 2.7 The OS images

```sh
cd /root/app/opentofu/modules/os_images
make images/debian-13-generic-amd64-with-guest-agent.qcow2 images/resolute-server-cloudimg-amd64-with-guest-agent.qcow2
ls -l images/*-with-guest-agent.qcow2       # two files, about 0.8 and 1.2 GB
```

They are git-ignored (`opentofu/.gitignore:9`), so a clone has none, and the platform stack fails without them (`opentofu/modules/os_images/main.tf:7,17`). This downloads the Debian 13 and Ubuntu `resolute` cloud images and adds `qemu-guest-agent` with `virt-customize` (the `Makefile` there). How long it takes was not measured. A plain `make` builds nothing. Alternatively, copy the two files from the same directory on worms-dev.

### 2.8 Python for the CLI

```sh
python3 -m venv /root/app/.venv-test
/root/app/.venv-test/bin/pip install -q -r /root/app/tests/requirements.txt
echo 'worms() { /root/app/.venv-test/bin/python /root/app/src/manage.py "$@"; }' >> /root/.bashrc
. /root/.bashrc && worms --help | head -3
```

This is the venv `worms test` would create (`src/manage.py:507-512`). The database commands and `worms status` need its Django, psycopg2 and proxmoxer; `worms build` uses the system Python. The node needs no `~/.worms/config`: with `WORMS_SSH` empty, the CLI treats its own machine as the node.

### 2.9 tmux and an ssh-agent

OpenTofu's provider logs in to `root@<node>` with an ssh-agent's keys only (`opentofu/stacks/platform/main.tf:14-17`). The WORMS key is authorized for root only at the end of the first build (`ansible/roles/host/round_agent/tasks/workspace.yml:138`), so the first build needs an agent with a key root already accepts: root's own `/root/.ssh/id_rsa`, which Proxmox creates and authorizes, as on worms-dev. Work in tmux from here on, so a dropped SSH session does not stop the build: `tmux new -s worms` (back in later: `tmux attach -t worms`), then inside it:

```sh
eval "$(ssh-agent -s)" && ssh-add /root/.ssh/id_rsa
grep -qF "$(cut -d' ' -f2 /root/.ssh/id_rsa.pub)" /root/.ssh/authorized_keys && echo authorized   # else: cat /root/.ssh/id_rsa.pub >> /root/.ssh/authorized_keys
ssh -o BatchMode=yes -o IdentityFile=none -o StrictHostKeyChecking=accept-new root@"$(hostname -i)" true && echo ok
```

Without `/root/.ssh/id_rsa` (not verified on a fresh 9.x): `ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519`, append its `.pub` to `authorized_keys`, `ssh-add` it. Each new tmux window needs the agent again. Later builds need none: the WORMS key logs in then, as on worms-dev. `worms node trust` is for laptops only (it refuses without `WORMS_SSH`).

## 3. Secrets and tokens

**Make a fresh vault; do not copy worms-dev's.** Leave out its `ansible/group_vars/vault.yml`, `.vault_pass`, `ansible/pki/` and every `terraform.tfstate`: its tokens belong to worms-dev's Proxmox, its WORMS key would open both nodes' VMs, and its state describes worms-dev's VMs. A fresh vault means new database passwords, Django keys, WORMS key and CA (browsers see a new certificate), and no teams, users or uploads from worms-dev.

```sh
cd /root/app
worms secrets issue proxmox-token        # role WormsProvision, user worms-provision@pve, token on /
worms secrets issue orchestrator-token   # WormsSandboxPower on /pool/sandbox (Proxmox accepts the path before the pool exists)
worms secrets issue topology-token       # WormsTopology on /
worms secrets                            # the three tokens and ssh-key "set", (generated) 13/13 present
head -c 14 ansible/group_vars/vault.yml; echo    # $ANSIBLE_VAULT
```

`issue` runs `pveum` on the node and needs nothing beforehand (`issue` in `src/worms_cli/tokens.py`). Ignore its hint "Deliver it to its users with `worms build provision`": the first build delivers the tokens. The old `terraform-prov@pve` token and `credentials.auto.tfvars` are not needed; do not create them (`worms` refuses to run while any `*.auto.tfvars` exists).

## 4. Build

```sh
cd /root/app && worms build; echo "exit $?"
```

It runs init, secrets, platform (bridges, images, snippets, pools, hookscript), firewall and host network, then the infrastructure and the 14 configure playbooks under the internet window, offline mode, and the `clean` snapshot of the 11 service VMs. **About an hour** (an estimate: never run on an empty node). `Update all packages` prints nothing for about 16 minutes. Success: the last lines are the host summary and `== setup complete (internet access denied) ==`, exit 0. It is safe to repeat after any failure, and the internet window closes by itself.

| Symptom | Do |
|---|---|
| `cannot log in ... OpenTofu's Proxmox provider`, or tofu fails with an SSH authentication error | Step 2.9 in this shell |
| `... overrides the vault's token for OpenTofu: remove it` | Delete that `*.auto.tfvars` |
| tofu cannot find a `.qcow2` | Step 2.7 |
| tofu errors about `snippets`, `import` or `local-zfs-sandbox` | Step 2.4 |
| New SSH or 8006 connections time out after the firewall step | Step 2.5, from an open session or the server console |
| `got timeout`, `zfs create` timed out, `can't lock file '/var/lock/pve-manager/pve-storage-…'` (F5) | Run the same command again |
| `dataset already exists` | A leftover volume: the loop under "Stop and remove things" in `docs/management.md`, then `pvesm free <storage>:<volume>` |
| `Run Django migrations` fails, output hidden | Build again: `lock_timeout` turns a lock into a failure, not a hang. To see the error: `worms build provision -- --limit orchestrator -e hide_secrets=false` (prints secrets) |
| After a Ctrl-C the next run waits on the dpkg lock | apt goes on in the VMs, up to 15 minutes (F2) |
| `!!! INTERNET ACCESS IS STILL ALLOWED` | `worms build internet-deny` |
| `warning: no snapshot of ...` on a rerun | VMs from an earlier, failed run. Before any devbox has existed: `worms build snapshot-services`; later: `worms build rebuild-services` |
| `Kernel network settings` fails on `net.netfilter.*` (not verified) | `modprobe nf_conntrack`, then build again |

**Afterwards**, in this order:

```sh
worms build ping            # 15 lines, each "SUCCESS"
worms status                # firewall enabled; every infrastructure and private-internet VM running; no sandbox or devbox VMs; internet denied; competition "WORMS 2026", 0 teams
worms secrets check         # 3 rows "ok: WormsProvision on /", "ok: WormsSandboxPower on /pool/sandbox", "ok: WormsTopology on /"
worms simulate              # all PASS but one FAIL, "sandbox deployed (client VM for the traffic)"; it exits 1 for that one
worms admin reset-password  # one-time password for "admin", printed once
```

**Back up**, off the node: the vault, `ansible/.vault_pass`, `ansible/pki/worms/` and `opentofu/stacks/*/terraform.tfstate` (`docs/accounts.md` §4.1). Nothing backs up the database.

**On the dashboard**, from outside the node: open `https://worms.mlsec.tu-berlin.de/`, sign in as `admin`, set a password. Create the teams on `/staff/teams/`, then the users on `/staff/users/` (one by one or as a CSV), and copy the one-time passwords from the page that follows (`docs/management.md`, "Accounts and teams").

**Devboxes:**

```sh
worms devbox provision
tail -F /var/log/worms/jobs/latest.log     # about 17 minutes for 3 teams on worms-dev
worms status                               # devbox VMs 13/13 running
```

On `got timeout`, run it again (F5). Keys, connecting, uploads and resets: `docs/management.md`, "Devboxes", and steps 8–12 of `docs/manual-devbox-run.md`.

## 5. Checks after the build

```sh
# From a laptop
curl -sk -o /dev/null -w '%{http_code}\n' https://worms.mlsec.tu-berlin.de/      # 200 (by IP: 400; the internal CA's certificate, browsers warn)
ssh -p 2222 -o BatchMode=yes -o StrictHostKeyChecking=accept-new team@worms.mlsec.tu-berlin.de true   # after `devbox provision`: Permission denied (publickey)
ssh root@worms.mlsec.tu-berlin.de true && curl -sk -o /dev/null -w '%{http_code}\n' https://worms.mlsec.tu-berlin.de:8006/   # logs in, then 200

# On the node
test -e /run/worms/internet-allowed && echo OPEN || echo denied                   # denied
ssh -i /etc/worms/ssh/worms -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null debian@10.5.0.14 \
  'curl -sS -m 5 -o /dev/null https://deb.debian.org/ && echo OPEN || echo closed'  # curl: (28) Resolving timed out ..., closed
systemctl is-active proxmox-firewall worms-host-firewall worms-firewall-watchdog.timer worms-mirror worms-round-agent   # 5 x active
journalctl -t worms-firewall-watchdog --since today                              # -- No entries --
pvesh get /cluster/firewall/options --output-format json                         # "enable":1, "policy_in":"DROP"
cat /sys/module/kvm_intel/parameters/nested                                      # N
for id in $(qm list | awk 'NR>1 {print $1}'); do echo "$id $(qm listsnapshot $id | grep -c clean)"; done
#   1 for scoring, logging, monitor, dns and the 7 private-internet VMs; 0 for database, orchestrator, dashboard, syslog.
#   After `devbox provision`, each team's A, B and C show 1 too (never run live)
```

## 6. Differences from worms-dev, and pitfalls

| | worms-dev | Fresh worms-pve |
|---|---|---|
| `proxmox_instance` in `shared-variables.tf` | Local change to `worms-dev`, never to be committed | The repository's `worms-pve`. Copy nothing |
| `TF_VAR_proxmox_instance` | Not set | Do not set it. The round agent gets the name from `proxmox.node` |
| `cve_search` RAM | The repository's 4096 MB (VM 103) | The same |
| Dashboard name | `worms-dev.mlsec.tu-berlin.de` | `worms.mlsec.tu-berlin.de`, from `dashboard_public_names`. An unlisted node name fails the dashboard play |
| Firewall rules for SSH and 8006, storage, images | Added by hand | Steps 2.4, 2.5, 2.7 |
| ssh-agent | None; the WORMS key is authorized for root | Needed for the first build (2.9) |
| `bpg/proxmox` provider | 0.113.1 (platform, private internet, infrastructure), 0.116.0 (firewall), 0.118.0 (devbox, sandbox) | The same: each stack pins its version since 2026-11-06 (`version =` in its `required_providers`). The lock files stay git-ignored |
| Vault, CA, state, data | Its own | New (§3) |
| `terraform-prov@pve` | Still there, with `Administrator` | Never created |

**Never tried:** a build from nothing, and with it the provider's first SSH login to a new node (its host-key handling is not verified) and the first `clean` snapshot of the service VMs (`docs/status.md` §2.2). No snapshot or rollback has run live, a devbox reset by rollback included. Expect first-run failures; each build continues where the last one stopped.

**Also:** `llm_access_ip` (141.23.117.34, `ansible/group_vars/all.yml:13`) and the DNS forwarder assume TU Berlin's network. A build stops every devbox for its length. KSM (D-16), SMT (D-15) and the open devbox security items S4, S18 and S19 (`docs/status.md` §4) are as on worms-dev.
