# Fresh install on `worms`

From "Proxmox VE is installed and root has a password" to a running platform with devboxes for three teams. Everything runs on the node as root; the node is also the controller (the vault, the CA and the OpenTofu state live in `/root/app`). Written on 2026-11-06 from the code and from `worms-dev`, which was set up by hand. **A build on an empty node has never run** (`docs/status.md` §2). *Not verified* marks what comes from reading the code only. Updated the same day for the event tables and the merge of `end-to-end-tests` (`docs/merge-end-to-end-tests.md`): nothing new to do by hand, only new things to check (§4, §5) and one more pitfall (§6).

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
| Name | The node is called `worms` (it was planned as `worms-pve`; renamed on 2026-11-06). The name is the switch: `worms_environments` in `ansible/group_vars/all.yml` makes `worms` production and `worms-dev` development, with each one's public name and SSH ports; `worms` refuses any other name | `hostname; ls /etc/pve/nodes`: `worms` only |
| Public bridge | `vmbr0` holds the public address and the default route, as the installer sets it up. The name is hardcoded (`host_wan_interface`, `ansible/roles/host/host_network/defaults/main.yml:7`) | `ip -br addr show vmbr0; ip route show default` |
| DNS | `worms.mlsec.tu-berlin.de` points to the node. On 2026-11-06 it resolved to `141.23.117.59`, which answered on 22 and 8006 | `getent hosts worms.mlsec.tu-berlin.de`, on the node and a laptop |
| Free ranges | Nothing on the node or its networks uses 10.2.0.0–10.13.255.255, 10.64.0.0/10 or 10.128.0.0/10 (`opentofu/shared/shared-variables.tf`) | `ip -br addr; ip route` |
| Resolver | During builds the DNS VM forwards to `130.149.7.7`, TU Berlin's (`ansible/roles/dns/setup_dns/defaults/main.yml:7`) | Change that line outside TU Berlin's network |

The network in front of the node must let these through (a timeout from outside means it does not). Outbound, the node needs HTTP(S) and DNS (apt, PyPI, the OpenTofu registry, GitHub, the git server); team VMs need `teachllm.mlsec.tu-berlin.de` (141.23.117.34) on 443.

| Inbound port | From | Goes to |
|---|---|---|
| 443/tcp | everyone | host DNAT to the dashboard |
| 22/tcp | the teams | host DNAT to the devbox SSH gateway (`ssh_gateway_public_port`) |
| 2222/tcp, 8006/tcp | operators | the node: its own sshd (2.5), the Proxmox web UI. OpenTofu's provider, and the round agent's, log in on 2222 too, over loopback on the node (from a laptop: `worms.mlsec.tu-berlin.de`): `operator_ssh_port` in `worms_environments`, given to every stack's `node` block. The agent has only the WORMS key: `SetupRoundAgent.yml` authorizes it for root |

**Optional, before the build, on a laptop with Docker:** Fabian's rehearsal of the dashboard stack (nginx, gunicorn, the orchestrator, real grants, concurrent sign-ins) in a container, `docs/testing.md`, "Rehearsals without the node". It has not run on the merged `main`; this node has no Docker.

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

### 2.5 SSH on 2222 for operators, and the web UI

Port 22 on the node's public address belongs to the teams: the build forwards it to the devbox SSH gateway (`ansible/roles/host/host_network/templates/worms.nft.j2`). The node's own sshd therefore listens on 2222 as well, for operators and OpenTofu; it keeps 22 for connections that do not come in on `vmbr0` (the node to itself, the VMs' networks). The build refuses to forward 22 while nothing listens on 2222 (role `host_network`, "Check that operators keep SSH to the node").

The build also turns on the Proxmox firewall with input policy DROP (`opentofu/stacks/firewall/main.tf`). From then on only the node's own subnet (`pve-firewall localnet`) may open new connections to it, plus the two rules below; open sessions survive. To close finding S22, add `--source <your networks>` to both; a wrong source locks you out once the build has run.

```sh
printf 'Port 22\nPort 2222\n' > /etc/ssh/sshd_config.d/worms-ports.conf
grep -nE '^\s*Port' /etc/ssh/sshd_config      # nothing: a Port line there as well would bind twice
sshd -t && systemctl reload ssh
ss -Htln 'sport = :2222'                        # one line per address family
pve-firewall localnet
pvesh create /nodes/$(hostname)/firewall/rules --type in --action ACCEPT --proto tcp --dport 2222 --enable 1 --comment "operators: SSH"
pvesh create /nodes/$(hostname)/firewall/rules --type in --action ACCEPT --proto tcp --dport 8006 --enable 1 --comment "operators: web UI"
pvesh get /nodes/$(hostname)/firewall/rules
```

From a laptop, from now on: `ssh -p 2222 root@worms.mlsec.tu-berlin.de`, or once in `~/.ssh/config`:

```
Host worms.mlsec.tu-berlin.de
    User root
    Port 2222
```

The `worms` CLI in laptop mode (`WORMS_SSH`) and plain `ssh`/`scp` then use 2222 too. Teams never need it: their command names no port.

### 2.6 The repository

Both remotes are private. Over HTTPS git asks for a user and an access token; do not put the token in the URL, it would end up in `.git/config`. Only what is pushed to `main` gets deployed.

```sh
git clone --branch main https://git.tu-berlin.de/mlsec/teach/winter-2026/worms/worms-platform.git /root/app
# or: git clone --branch main git@github.com:j-moeller/worms.git /root/app   (needs a key GitHub accepts)
cd /root/app && git log -1 --oneline
grep -A3 'variable "proxmox_instance"' opentofu/shared/shared-variables.tf   # default = "worms"
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

### 2.9 tmux

Work in tmux from here on, so a dropped SSH session does not stop the build: `tmux new -s worms` (back in later: `tmux attach -t worms`). No ssh-agent of your own is needed, and none is used: the build logs in everywhere with one key, the WORMS key from the vault (§3), which it loads into a private agent of its own (`src/worms_cli/session.py`). OpenTofu's provider uploads snippets to `root@<node>` with it, so root has to accept it: `worms node trust` in §3.

## 3. Secrets and tokens

**Make a fresh vault; do not copy worms-dev's.** Leave out its `ansible/group_vars/vault.yml`, `.vault_pass`, `ansible/pki/` and every `terraform.tfstate`: its tokens belong to worms-dev's Proxmox, its WORMS key would open both nodes' VMs, and its state describes worms-dev's VMs. A fresh vault means new database passwords, Django keys, WORMS key and CA (browsers see a new certificate), and no teams, users or uploads from worms-dev.

```sh
cd /root/app
worms secrets issue proxmox-token        # role WormsProvision, user worms-provision@pve, token on /
worms secrets issue orchestrator-token   # WormsSandboxPower on /pool/sandbox (Proxmox accepts the path before the pool exists)
worms secrets issue topology-token       # WormsTopology on /
worms secrets                            # the three tokens and ssh-key "set", (generated) 15/15 present
head -c 14 ansible/group_vars/vault.yml; echo    # $ANSIBLE_VAULT
worms node trust                         # root on this node accepts the WORMS key: "... accepts the WORMS key ...: ok"
```

`worms secrets` generates what is missing; every other command only reads the vault, and stops, naming what is missing and `worms build secrets`, if anything is. A new WORMS key is never generated while VMs of this checkout exist: they authorize the old one (restore the vault, or `worms secrets set ssh-key < key`).

`issue` runs `pveum` on the node and needs nothing beforehand (`issue` in `src/worms_cli/tokens.py`). Ignore its hint "Deliver it to its users with `worms build provision`": the first build delivers the tokens. The old `terraform-prov@pve` token and `credentials.auto.tfvars` are not needed; do not create them (`worms` refuses to run while any `*.auto.tfvars` exists).

## 4. Build

```sh
cd /root/app && worms build; echo "exit $?"
```

It runs secrets, init, platform (bridges, images, snippets, pools, hookscript), firewall and host network, then the infrastructure and the 14 configure playbooks under the internet window, offline mode, and the `clean` snapshot of the 11 service VMs, each a numbered step (`== step 12/27: SetupDbAndDashboard.yml`). **About an hour** (an estimate: never run on an empty node). `Update all packages` prints nothing for about 16 minutes. Success: the last lines are the host summary and `== setup complete (internet access denied) ==`, exit 0.

**When a step fails, the build stops there.** Nothing after it runs; the internet window is closed and the devbox VMs it shut down are started again. A missing value (a token, a vault entry, a platform output) stops it the same way, before the step that needs it. The last lines say which step and why, and the tool's own error is above them. Fix the cause, then go on from the failed step: `worms build --resume` (the progress is in `.worms-build.json`, with what the earlier steps found, such as which service VMs were rolled back). Another `worms build`, `provision`, `configure`, `infrastructure` or `rebuild-services` is refused meanwhile; `worms build <component> --restart` starts one over instead.

On the way it migrates the empty database through `core` 0001–0006 and then applies the grants, the event tables' included. That chain was run from an empty schema on worms-dev's PostgreSQL on 2026-11-06 (a throwaway schema, dropped afterwards): every migration applied, the event tables got their `written_by` default and CHECK constraints, and the models match the migrations. The collision queries in `docs/testing.md` are for upgrading a database with data, not for this. The orchestrator gets the Proxmox CA from `/etc/pve/pve-root-ca.pem` over the build's SSH connection and the node's name in its `/etc/hosts` (role `django_app`, `tasks/connectivity.yml`); the dashboard reaches the orchestrator as `orchestrator.worms.internal` the same way. Neither needs DNS.

| Symptom | Do |
|---|---|
| `root on this node does not accept the WORMS key`, `cannot log in ... with the WORMS key`, or tofu fails with an SSH authentication error | `worms node trust` (§3), then `worms build --resume` |
| `PROXMOX_VE_SSH_... is set in your environment` | `unset` it: OpenTofu would log in with it instead of the WORMS key |
| `the vault lacks ...` / `there is no vault` | `worms build secrets` |
| `proxmox-token: MISSING ...` / `orchestrator-token: MISSING ...` | `worms secrets issue <name>`, then `worms build --resume` |
| `` `worms build all` stopped at step ... `` on the next `worms build` | That build is waiting for `worms build --resume`; or `worms build all --restart` |
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
| From a laptop: `stack sandbox` / `stack devbox ... worms-node-run: No such file or directory`, then `NOT APPLIED` (now: `no round agent yet`) | Expected until the build has installed the round agent (`SetupRoundAgent.yml`): the two stacks live in its workspace `/opt/worms` and do not exist yet. The inventory is still written. If it persists after a full build: `ls -l /usr/local/sbin/worms-node-run` on the node. Any other failure to read them (SSH, tofu) aborts the build with `error: 'tofu output' of stack ...` |
| A devbox job fails at once: `build devbox-apply` ... `error: no ansible/inventory.generated.yml` in `/var/log/worms/jobs/<id>.log` | A `/root/.worms/config` on the node with `WORMS_SSH` (laptop mode) that the agent read. Remove that line on the node (the node needs no config, §2.8); `SetupRoundAgent.yml` now also empties it in the agent's environment |
| What runs after a failure | Nothing but the cleanup, announced by `error: a step failed, stopping the build: ... first`: regenerating the inventory after a failed apply, closing the internet window (`Internet.yml -e internet=denied`) and starting the devbox VMs it shut down. Then `build stopped: ...`, the step, and `worms build --resume`, exit 1. The step's own error is above those lines |
| `stack devbox has outputs (devbox) but no ansible_hosts: its last apply did not finish` | The apply stopped before the VMs (the job log has its `Error:`). Fix that, then `worms devbox provision` again |
| `Completely failed to parse inventory source .../inventory.yml` (or `inventory.generated.yml`) | Ansible could not read that file, and stops (`ansible.cfg`: `any_unparsed_is_failed`; by default it would go on without the file and skip every play on its groups). The `Failed to parse inventory with 'yaml' plugin` warning above it gives line and column. In a job (`/opt/worms/ansible/...`): fix the file in the checkout, then `ansible-playbook SetupRoundAgent.yml --tags agent-sync` while no job runs. A missing `inventory.generated.yml`: `worms build inventory` |
| `worms-audit-gateway.service` fails every minute (`systemctl --failed`, `ssh exited 255`) | Expected until `worms devbox provision`: there is no SSH gateway to read logins from yet |
| Rerunning a single playbook later waits for minutes on `apt` or `pip` | The internet window is closed: add `--skip-tags packages -e internet_enabled=false` (`docs/management.md`, "Build and deploy") |
| The orchestrator cannot reach Proxmox (topology empty, `CERTIFICATE_VERIFY_FAILED` or a name mismatch in its journal) | The web UI's certificate was replaced (§6). Put Proxmox's own back: `pvenode cert delete`, then `systemctl restart pveproxy` (not verified) |

**Afterwards**, in this order:

```sh
worms build ping            # 15 lines, each "SUCCESS"
worms status                # firewall enabled; every infrastructure and private-internet VM running; no sandbox or devbox VMs; internet denied; competition "WORMS 2026", 0 teams
worms secrets check         # 3 rows "ok: WormsProvision on /", "ok: WormsSandboxPower on /pool/sandbox", "ok: WormsTopology on /"
worms simulate              # all PASS but one FAIL, "sandbox deployed (client VM for the traffic)"; it exits 1 for that one
worms admin reset-password  # one-time password for "admin", printed once
```

**Back up**, off the node: the vault, `ansible/.vault_pass`, `ansible/pki/worms/` and `opentofu/stacks/*/terraform.tfstate` (`docs/accounts.md` §4.1). Nothing backs up the database yet: a nightly Proxmox backup job is drafted and off (`backup_storage`, `docs/backup.md`).

**On the dashboard**, from outside the node: open `https://worms.mlsec.tu-berlin.de/`, sign in as `admin`, set a password. Create the three teams on `/staff/teams/`, then the users on `/staff/users/` (one by one or as a CSV), and copy the one-time passwords from the page that follows (`docs/management.md`, "Accounts and teams"). Names of accounts and teams are case-insensitive since the merge: `Alice@x` and `alice@x` are one account.

`worms audit --hours 1` should now list, written by `dbuser_events_dashboard`: the admin's sign-in (`login.ok`), the password change (`password.changed`), each team and account created (`team.created`, `account.created`/`account.imported`), and nothing written by any other role. An empty list means the dashboard's events account does not work: the dashboard keeps running (events are best effort) and logs `event not recorded` in its journal.

**Devboxes:**

```sh
worms devbox provision
tail -F /var/log/worms/jobs/latest.log     # about 17 minutes for 3 teams on worms-dev
worms status                               # devbox VMs 12/12 running (as on worms-dev; the shared SSH gateway is not among them)
```

On `got timeout`, run it again (F5). Keys, connecting, uploads and resets: `docs/management.md`, "Devboxes", and steps 8–12 of `docs/manual-devbox-run.md`. Tell the teams what a worm is (`docs/worm-contract.md`): a zip with `main.sh` at its top level, started as `bash main.sh`. Zips made for the old `python3 main.py` do not run.

## 5. Checks after the build

```sh
# From a laptop
curl -sk -o /dev/null -w '%{http_code}\n' https://worms.mlsec.tu-berlin.de/      # 200 (by IP: 400; the internal CA's certificate, browsers warn)
ssh -p 22 -o BatchMode=yes -o StrictHostKeyChecking=accept-new team@worms.mlsec.tu-berlin.de true   # after `devbox provision`: Permission denied (publickey), from the gateway
ssh -p 2222 root@worms.mlsec.tu-berlin.de true && curl -sk -o /dev/null -w '%{http_code}\n' https://worms.mlsec.tu-berlin.de:8006/   # logs in, then 200

# On the node
test -e /run/worms/internet-allowed && echo OPEN || echo denied                   # denied
ssh -i /etc/worms/ssh/worms -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null debian@10.5.0.14 \
  'curl -sS -m 5 -o /dev/null https://deb.debian.org/ && echo OPEN || echo closed'  # curl: (28) Resolving timed out ..., closed
systemctl is-active proxmox-firewall worms-host-firewall worms-firewall-watchdog.timer worms-mirror worms-round-agent worms-audit-gateway.timer   # 6 x active
test -s /etc/worms/events.env && echo present                                    # the node's events account (worms-audit)
journalctl -t worms-firewall-watchdog --since today                              # -- No entries --
pvesh get /cluster/firewall/options --output-format json                         # "enable":1, "policy_in":"DROP"
cat /sys/module/kvm_intel/parameters/nested                                      # N
for id in $(qm list | awk 'NR>1 {print $1}'); do echo "$id $(qm listsnapshot $id | grep -c clean)"; done
#   1 for scoring, logging, monitor, dns and the 7 private-internet VMs; 0 for database, orchestrator, dashboard, syslog.
#   After `devbox provision`, each team's A, B and C show 1 too (never run live)
```

**The first product, once, with one student account of one team** (about 30 minutes; it resets that team's devbox):

1. Sign in with the one-time password, set a password, add an SSH key on the profile. The key job takes about 3 minutes (`tail -F /var/log/worms/jobs/latest.log` on the node).
2. From a laptop outside TU Berlin's network: the command on the Dev view (`ssh -A -J team@worms.mlsec.tu-berlin.de root@<A>`), then on to B and C.
3. Upload a zip whose `main.sh` is `echo hello; sleep 60` in the Dev view. The devbox resets and runs it (about 9 minutes on worms-dev): the upload page goes from "Resetting your VMs" to "Running". What the worm sends to the logging API and the tokens it captures appear on the progression page; `main.sh`'s own output does not: on A, `sudo journalctl -u worms-start`. On worms the provisioned VMs already have their `clean` snapshot, so this reset is a rollback (`scripts/round_agent/handlers.py:545`), the first that has ever run anywhere (`docs/status.md` §2). If it fails, the upload page says only "Could not be started"; the reason is in the job log on the node.
4. `worms audit --hours 1` lists the sign-in, the password change, the key (`credential.ssh_key_added`), the gateway login (`ssh_gateway.login`, with the key's owner and the laptop's public address in `Client`), and, if you also press Reset on the profile, `team.devbox_reset`. Uploads and their runs leave no event (`docs/feature-audit.md`). Run the gateway check after a minute: the collector runs once a minute.


## 6. Differences from worms-dev, and pitfalls

What differs between the two systems is one table, chosen by the node's name: `worms_environments` in `ansible/group_vars/all.yml` (`worms` = production, `worms-dev` = development). Nothing is changed by hand on either system. Django's `DEBUG` is off on both: `PRODUCTION=1` means "deployed", not "the production system"; `worms status` shows which system it is.

| | worms-dev (development) | worms (production) |
|---|---|---|
| `proxmox_instance` | `worms-dev`: set by `worms` from the node's name (the stacks have no default) | `worms`, the same way. Copy nothing |
| SSH ports (`operator_ssh_port`, `ssh_gateway_public_port`) | Node on 22, teams on 2222 | Node on 2222 (and 22 from inside), teams on 22. OpenTofu logs in to the node on that port, over loopback |
| `TF_VAR_proxmox_instance` | Not set | Do not set it (`worms` refuses one that is not the node's name). The round agent gets the name from `proxmox.node` |
| `cve_search` RAM | The repository's 4096 MB (VM 103) | The same |
| Dashboard name (`public_name`) | `worms-dev.mlsec.tu-berlin.de` | `worms.mlsec.tu-berlin.de`. An unlisted node name fails the dashboard play |
| Firewall rules for SSH and 8006, storage, images | Added by hand | Steps 2.4, 2.5, 2.7 |
| ssh-agent | None; the WORMS key is authorized for root | Needed for the first build (2.9) |
| `bpg/proxmox` provider | 0.113.1 (platform, private internet, infrastructure), 0.116.0 (firewall), 0.118.0 (devbox, sandbox) | The same: each stack pins its version since 2026-11-06 (`version =` in its `required_providers`). The lock files stay git-ignored |
| Vault, CA, state, data | Its own | New (§3) |
| `terraform-prov@pve` | Still there, with `Administrator` | Never created |
| The web UI's certificate (8006) | Proxmox's own, from its cluster CA | Keep it. The orchestrator trusts only `/etc/pve/pve-root-ca.pem` and connects by the node's name; a custom or ACME certificate for the web UI stops VM power and the topology |
| Event tables | Added to a running database on 2026-11-06; 46 earlier gateway logins imported | Created empty by the build; the gateway collector starts with the first login after `devbox provision` |

**Never tried:** a build from nothing, and with it the provider's first SSH login to a new node (its host-key handling is not verified) and the first `clean` snapshot of the service VMs (`docs/status.md` §2.2). No snapshot or rollback has run live, a devbox reset by rollback included. Expect first-run failures; each build continues where the last one stopped.

**Also:** `llm_access_ip` (141.23.117.34, `ansible/group_vars/all.yml:13`) and the DNS forwarder assume TU Berlin's network. A build stops every devbox for its length. KSM (D-16), SMT (D-15) and the open devbox security items S4, S18 and S19 (`docs/status.md` §4) are as on worms-dev.
