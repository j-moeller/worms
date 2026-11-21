# 9. The SSH gateway

How a team reaches its devbox over SSH: one shared gateway VM, `devbox-ssh-gateway`, that every team jumps through to its own VM A. What is stored where, how a key gets there, and what keeps one team out of another's devbox. The devbox itself: [4](04-dev-environment.md); the security findings referred to below: [7](07-security.md).

## Concepts

- **One gateway, no shell.** Every team logs in to the same gateway as the same unprivileged user, `team`, and can do one thing there: open a TCP forward to port 22 of its own VM A (`ssh -J`). No shell, no command, no `scp`, no other forward.
- **The key says which team.** There is no per-team account. Each line of the gateway's `authorized_keys` is one key of one team, and the line's own options bind it to that team's VM A. A key belongs to exactly one team (unique across all teams).
- **A is the way in, B and C are the worm's.** The gateway forwards to A only. From A a team goes on to B and C over its devbox's own network (`ssh -A root@<B>`), as a worm does in a round's sandbox.
- **Management is a separate road.** Operators and Ansible reach every VM (the gateway too) over the management network with the WORMS key; teams never come that way, and the gateway accepts it only from the host.

## A login, hop by hop

What a team types (the Dev page shows it, [`views/devbox.py`](../projects/dashboard/dashboard/views/devbox.py)):

```
ssh -A -J team@worms.mlsec.tu-berlin.de root@10.7.<i>.11      # production: port 22
ssh -A -J team@worms-dev.mlsec.tu-berlin.de:2222 root@10.7.<i>.11
```

```
student ──internet──▶ host :22 (prod) / :2222 (dev) ──DNAT, masquerade──▶ gateway 10.7.0.2:22
   gateway (user team, key line: permitopen="10.7.<i>.11:22") ──forward──▶ team's A 10.7.<i>.11:22 (root)
   A ──devbox network──▶ B, C
```

1. **The host** ([`worms.nft.j2`](../ansible/roles/host/host_network/templates/worms.nft.j2)): on the WAN, the gateway's public port (`ssh_gateway_public_port`, [`group_vars/all.yml`](../ansible/group_vars/all.yml): 22 on production, where operators use 2222; the other way round on dev) is logged (`worms-ssh-gw:`, rate-limited, for the audit trail) and DNATed to the gateway's `10.7.0.2:22`. The connection is masqueraded on the way in: the gateway sees every team as the host, `10.7.0.1`, so a VM on that bridge can never pass for a team. Published only with `host_publish_ssh_gateway` ([`inventory.yml`](../ansible/inventory.yml)), and refused unless the host's own sshd listens on the operators' port, so publishing port 22 cannot lock operators out.
2. **The gateway** authenticates the key as user `team`. The key's line allows only `port-forwarding` to `permitopen="10.7.<i>.11:22"`, that team's A on the SSH fabric (`vmbr7`, 10.7.0.0/16).
3. **VM A** accepts the same key as `root`, from its team key file. A's fabric NIC accepts port 22 from the gateway's address only.
4. **B and C** are not on the fabric's reach from the gateway: a key's `permitopen` names A alone.

## What is stored where

| What | Where | Written by |
|---|---|---|
| A team's public keys | table `devbox_ssh_keys` ([`core/models/devbox.py`](../projects/dashboard/core/models/devbox.py)): the key, its name, its SHA-256 fingerprint (unique over every team), who added it | the dashboard (Dev page), `worms key add` |
| The gateway's address, port, user per devbox | the `devboxes` row (`ssh_gateway_host`, `_port`, `_user`), from the orchestrator's settings (`DEVBOX_SSH_GATEWAY_HOST`, [`group_vars/orchestrator.yml`](../ansible/group_vars/orchestrator.yml)) | the orchestrator, when a devbox job completes |
| Every team's keys on the gateway | `/home/team/.ssh/authorized_keys`: one line per key, `restrict,port-forwarding,command="/usr/sbin/nologin",permitopen="10.7.<i>.11:22" <key> <comment> team=t<id>`; the home, `.ssh` and the file root's (0755, 0755, 0644): the team cannot change them | role `ssh_gateway` ([`authorized_keys.j2`](../ansible/roles/applications/ssh_gateway/templates/authorized_keys.j2)); `worms key sync` ([`keys.py`](../src/worms_cli/keys.py)) |
| A team's keys on its A, B, C | `/root/.ssh/authorized_keys_worms_team` (0600), named by the drop-in `20-worms-devbox.conf` as a second `AuthorizedKeysFile` | role `devbox` ([`keys.yml`](../ansible/roles/applications/devbox/tasks/keys.yml)); a reset, over SSH ([`handlers.py`](../scripts/round_agent/handlers.py), `_write_team_keys`); `worms key sync` |
| The gateway's sshd settings | `/etc/ssh/sshd_config.d/10-devbox-gateway.conf`, from cloud-init ([`ssh-gateway.tftpl`](../opentofu/stacks/devbox/templates/ssh-gateway.tftpl)); the role only checks it is there | OpenTofu (the devbox stack, on the host) |
| The operators' key on the gateway | `debian`'s `authorized_keys`, from cloud-init: the WORMS key | OpenTofu |
| Who logged in | table `ssh_gateway_logins` (time, fingerprint, the client's public address when the host's log has it, else none) | the round agent's login collector, every 60 s |
| The gateway VM | the devbox stack ([`opentofu/stacks/devbox/main.tf`](../opentofu/stacks/devbox/main.tf)): 1 core, 512 MB, `net0` on the SSH fabric (`vmbr7`, 10.7.0.2), `net1` on the services network (management) | the round agent (`devbox_provision`) |

## How a key gets there

- **A team adds or removes a key on the dashboard.** The dashboard checks it ([`core/ssh_keys.py`](../projects/dashboard/core/ssh_keys.py): one of 7 key types, at most 8192 bytes, a plain comment, at most 64 keys a team), stores it, writes a credential event, and asks the orchestrator (`POST /devbox/<team>/reconfigure/`). The orchestrator queues a `devbox_reconfigure` job with every team's keys (the gateway's file is written whole). The round agent checks every key again ([`validation.py`](../scripts/round_agent/validation.py): the same rules, and no key of two teams), and runs `DevboxKeys.yml`: the gateway's file, every devbox VM's team key file. A key reaches Ansible tagged `!unsafe`: a `{{ }}` in it is never templated.
- **An operator: `worms key add|remove <team> <key>`**, the same checks, then `worms key sync`: over the management SSH, directly, without the round agent: the gateway's file (one line per key) and the team key files. `worms key` reads them back on every VM and compares fingerprints.
- **A reset** rolls a team's VMs back to their snapshot (from before any team key) and writes the team key file again, over SSH; the gateway is not touched (its lines did not change).
- **A member who leaves** loses the keys they added, and a push follows. Keys an operator added stay.

## What makes it secure

**The key's own line** (per key, on the gateway):
- `restrict`: no agent forwarding, no X11, no tty, no port forwarding; `port-forwarding` then allows TCP forwarding back, bounded by `permitopen`;
- `permitopen="10.7.<i>.11:22"`: one host, one port, that team's A;
- `command="/usr/sbin/nologin"`: no shell, no command, no `scp`/`sftp` (`restrict` does not stop those; a jump opens no session, so it still works).

**The gateway's sshd** (its drop-in): `AllowTcpForwarding local` and `PermitListen none` (no remote or listening forward), no agent, stream-local, X11 or tunnel forwarding, no `GatewayPorts`, no root login, keys only, `AllowUsers team debian@<the host>`, `MaxSessions 10`, `MaxStartups 10:30:60`; a client that stops answering is dropped after 10 minutes (`ClientAlive` 300 s × 2). The `team` user's shell is `/usr/sbin/nologin`, its home root's.

**The firewalls** ([`opentofu/stacks/firewall/main.tf`](../opentofu/stacks/firewall/main.tf), Proxmox's per-VM firewall with `ipfilter`):
- the gateway accepts port 22 from the host's fabric address only, and may send to port 22 on the SSH fabric only;
- a devbox VM's fabric NIC accepts port 22 from the gateway only;
- `ipfilter` pins each VM to its addresses: a devbox VM cannot answer as the host or the gateway;
- the host drops new traffic from the SSH fabric towards itself.

**One key, one team.** A fingerprint is unique over all teams (the database, and the round agent again): a team cannot register another team's key to get its line.

**Management apart.** The gateway takes `debian` from the host's management address only, with the WORMS key, which the CLI holds in an agent of its own and never forwards.

**An audit trail.** The host logs every new connection to the gateway's port (the client's public address); the round agent's collector reads the gateway's sshd journal every 60 s (`Accepted publickey for team`), matches the host's log by source port, and inserts each login into `ssh_gateway_logins` with an INSERT-only role; `worms show audit` names who registered the key.

**What `worms check` verifies** ([`validate.py`](../src/worms_cli/validate.py)): as the team does, with the public keys alone: the gateway takes the team's keys and forwards to its A; the team's `permitopen` is exactly its A; A, B, C take the keys as root. And whether OpenSSH's per-source penalties are on and have dropped connections (below).

## Known weak spots

- **Per-source penalties.** Every team reaches the gateway as one address (the masquerade), so OpenSSH's per-source penalties (on by default since 9.8) can shut every team out after a few failed logins. Nothing configures them yet; `worms check` warns while they are on and fails once they dropped a connection. A decision is pending.
- **No layer-2 isolation on the SSH fabric.** Findings S4 and S19 ([7](07-security.md)): a team's VM could try to poison the gateway's ARP on `vmbr7`; `ipfilter` limits what it can send, the bridge itself is not isolated.
- **`MaxStartups`** can be exhausted from the internet (S21): new logins are refused for everyone meanwhile.
- **Not recorded:** failed logins, where a session forwarded to, when it ended. The gateway's own clock is not synced (it is not in `Site.yml`, so it gets neither `timesync` nor `auditd`).
- **Open sessions survive** a key's removal or a team's deletion; `worms team delete` pushes no keys (the next push or `worms key sync` drops the lines).
- **Trust on first use.** Teams are not given the gateway's host key fingerprint.

## Operating it

| | |
|---|---|
| `worms key` | every team's keys as the gateway and its VMs have them, against the database |
| `worms key add`, `worms key remove`, `worms key sync` | change one, push them all (no round agent) |
| `worms check <team>` | the team's whole way in, as it logs in |
| `worms show audit --kind ssh_gateway` | the gateway's logins, who registered each key |
| `worms log ssh-gateway` | its journal (sshd) |
| `worms login ssh-gateway` | a shell there, as `debian` (management) |
