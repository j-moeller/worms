# Security assessment

Two threat models. Both attackers know the whole architecture and no secret.

1. **A malicious program is root on a sandbox or devbox VM** and attacks whatever it can reach.
   On a devbox it also controls the team's other two VMs, its uploads and its registered keys.
2. **A dashboard user** (a team member) attacks the dashboard.

Finding IDs (`S*`, `D*`, `C*`, and `F*`/`G*` from the live runs in `docs/archive/`) are cited in
code comments. What is open is summarised in `docs/status.md` §4.

## Threat model 1: root on a team VM

### The containment layers, as built

| Layer | What it does | Where |
|---|---|---|
| Per-VM Proxmox firewall | Policy DROP in and out, `ipfilter` (each NIC pinned to its address), security groups per role | `opentofu/stacks/firewall`, the VM modules |
| Node nftables | Zone firewall; forwards only the allowed flows; per-VM rate limits for new TCP connections and UDP packets; drops gateway↔gateway on the services bridge; never routes `vmbr9*`↔`vmbr1*` | role `host_network`, `worms.nft.j2` |
| Gateways | Each environment reaches the services only through its gateway, SNATed to one pinned address; no default route | `sandbox-gw.tftpl`, `devbox-gw.tftpl` |
| Firewall watchdog | Every 15 s: if `proxmox-firewall` is down, a host table missing, a running team VM without rules, or a team VM runs while the internet window is open, it hard-stops all team VMs and logs `auth.crit` | `worms-firewall-watchdog.sh.j2` |
| Resources | Own storage `local-zfs-sandbox`, CPU weight, NIC and disk rate limits, 512 MB RAM | the sandbox and devbox stacks |
| CPU | `x86-64-v3`: no VMX, so no `/dev/kvm`; refused by the stack, the tests and the agent | below |
| Devices | No guest agent, serial console, display or USB; `qm stop` works without the guest | every VM module |
| Services | Scoring and logging: `POST /submit` only, body and payload limits, per-source rate limits with bans, INSERT-only roles, attribution fail-closed | role `sandbox_api` |
| Internet window | Refused while a team VM runs; the agent starts none while it is open; runtime-only state | `Internet.yml`, `pipeline.py`, the agent |

**Fixed earlier:** S2 anonymous writes to devpi and the registry (a channel across rounds); S3
the gateway's `team` account could rewrite its keys; S5 scoring and logging attribution failed
open; S6 the internet window could open with team VMs running; S7 a run could start before the
last teardown; S8 an unbounded DNS query log; S15 a replaced VM could lose its firewall; S16 a team
login got a shell on the SSH gateway; F14/G8 gateways without nftables; G5 Zeek stopped at its
first log rotation.

### Does it hold for a devbox? Checked 2026-11-06

Read-only on the node, plus harmless connect, ping, DNS and HTTP probes as root from
`devbox-t3-a` (VM 118). Nothing that sends forged frames was tried on the live bridges.

| Claim | Seen live | Verdict |
|---|---|---|
| Per-VM firewall on every devbox VM, gateway and the SSH gateway: on, DROP both ways, `ipfilter`, `macfilter` | All 13. Cross-team TCP and ping on vmbr6 and vmbr7 blocked | **Holds** (except ICMP errors, below) |
| The node accepts nothing from the devbox bridges | Ports 22, 111, 3128, 8006 on `10.6.0.1` and `10.7.0.1` filtered from a VM | **Holds.** But the node has IPv6 link-local addresses and `accept_ra=1` on vmbr6, vmbr7, vmbr23 and vmbr9*, and every VM firewall has `radv: 1`: only the node's nftables drop stands between them |
| Devbox never routed to the sandbox; gateway↔gateway dropped; only `teachllm:443` leaves | All in the live ruleset; `1.1.1.1:443` blocked | **Holds** |
| SSH gateway: one entry VM per key, no shell, root-owned keys, SSH only from the node | As documented; `AllowAgentForwarding no` | **Holds** |
| No secrets on team VMs; no SSH agent forwarded into them by Ansible or the agent | Cloud-init holds only the WORMS public key; neither sets `ForwardAgent` | **Holds** |
| DNS answers no outside name; the caches are offline and cannot proxy out | `example.com` does not resolve; apt-cacher-ng `Offlinemode: 1`, CONNECT fails; devpi offline; registry read-only | **Holds** (S13: the apt-cacher-ng report page and cve-search UI still answer without a login) |
| No `/dev/kvm`; CPU, RAM, NIC and disk caps | `x86-64-v3`, 1 core at weight 50, 512 MB, 10 MB/s NIC, 200 IOPS / 20 MB/s disk | **Holds.** Nesting is still on in the node, and `cve-search` (`cpu: host`) answers on :5000 from a devbox |
| **vmbr6 and vmbr7 are port-isolated** (README, `devbox.md`, `requirements.md` said so) | **All 22 ports `isolated off`; no VM has the hookscript.** From t3-A, ARP resolves other teams' A VMs, a gateway and the SSH gateway: all teams share layer 2 on both bridges | **Not implemented.** Only the per-VM firewall separates the teams there |
| **Conntrack capacity and timeouts are tuned** | **262 144 entries and 432 000 s (5 days) for established TCP**, not the role's 1 048 576 and 3 600: `host.fw` sets them and wins | **Ineffective** |
| **Team VMs on separate storage** `local-zfs-sandbox` | It is `pool rpool`, the same pool and free space as the database's disk. Team disks are thick (5 GB reserved each), which bounds each VM | **Not separate** |
| **KSM is off** (`resources.md` said so) | `ksmtuned` active; 583 420 pages (2.2 GB) merged | **Not off** (S9) |
| The firewall watchdog covers every team VM | It covers pool `devbox` (VMs and gateways); the SSH gateway (VM 115) is in no watched pool; it checks only that a VM's tap has rules, not their policy | **Partly** |
| A reset rolls back to a clean snapshot | No VM has a snapshot; the node runs the agent from before snapshots | **Not deployed** |

### Threats the docs did not cover

| # | Severity | Threat | Fix |
|---|---|---|---|
| S18 | **High**, not tested | **Conntrack exhaustion stops the whole node.** One table for every VM, 262 144 entries, established TCP kept 5 days. A team's own VMs may open 500 TCP connections a second and send 5000 UDP packets a second per address to each other (the bridge rate limits), and the node tracks them. Holding them open fills the table in minutes; then every new connection through the node fails: the dashboard, the database, scoring, the agent's SSH | Per-source `ct count` limits for team addresses (as for syslog), the timeouts set in `host.fw` where they hold, ideally a conntrack zone per environment |
| S19 | **Medium–High**, from the ruleset, not tested | **ICMP errors pass every per-VM rule.** Proxmox's guest table accepts ICMP and ICMPv6 error types before any VM's chain, so `ipfilter`, `macfilter` and the DROP policy do not apply to them. With port isolation off, a devbox VM can send them, with any source address and MAC, to every other team's VM, the SSH gateway and the node on vmbr6 and vmbr7: a channel between teams, and a forged source MAC that points the bridge's table at the attacker, so frames for the SSH gateway stop reaching it and every team loses SSH | Port isolation (S4), plus a bridge rule that drops frames whose source MAC is not the port's own |
| S20 | Medium | **The database's disk shares a pool with team disks**, with no reservation. Bounded per VM today, but snapshots let a VM's disk use up to twice its size, and sandboxes grow with each round | A reservation for the infrastructure's volumes, or a quota on a dataset of its own for team disks |
| S21 | Low–Medium | **The SSH gateway's login slots.** All teams share the account `team` and `MaxStartups 10:30:60`, reachable from the internet on :2222, so idle unauthenticated connections can lock every team out | A per-source connection limit on the DNAT, or fail2ban on the gateway |
| S22 | Medium, outside this model | The node's own SSH and API (8006) accept any source on its public interface (`host.fw`: `IN SSH(ACCEPT)`, `IN ACCEPT -p tcp -dport 8006`). Not reachable from a team VM | Restrict to the admins' networks |
| S23 | Low | IPv6 rests on one layer: VMs may send router advertisements (`radv: 1`), the node accepts them on untrusted bridges | `accept_ra=0` and IPv6 off on the node's untrusted bridges, `radv: 0` |

**Already known and still open:** S4 port isolation (it needs the hookscript attached as
`root@pam`, or `bridge link set … isolated on` by the node's own tooling), S9 KSM, S17 a new team
VM runs without its firewall while it is built (up to 28 minutes measured; the node's own rules
still apply), SMT (L1TF/MDS between hyperthreads), nesting on with `cve-search` on `cpu: host`.

### The LLM endpoint

`teachllm.mlsec.tu-berlin.de` (`141.23.117.34`, TCP 443) is **the only exception** to "neither
sandbox nor devbox may have internet access". It is on the local network and assumed robust,
since any team code can reach it. One value, `llm_access_ip` in `ansible/group_vars/all.yml`,
drives every layer, and each allows only this address and port: the VM's Proxmox firewall
(`llm-access`), the node's bridge table, the gateway's firewall and nftables (a `/32` route via the
node, still no default route), and the node's forward chain (only from the gateways' management
addresses, then masqueraded). The private DNS answers this one outside name; it has no recursion
and no forwarders. Checked live on 2026-11-05 from all 9 devbox VMs: `teachllm:443` connects, its
other ports, neighbouring addresses, `1.1.1.1` and `8.8.8.8:53` do not, also with routes added
through other NICs or the gateway's address. Not seen from a sandbox.

**What it leaves open:** a channel out. Team code can send anything to `teachllm` and read its
answers, so whatever that server does with requests (logging, tools, fetching URLs) is part of
the containment. Its operators should know.

### CPU: no `/dev/kvm` on team VMs

With `/dev/kvm`, root on a VM reaches KVM's nested-virtualisation code in the node's kernel, a
history of guest-to-host escapes (CVE-2021-29657). Devbox VMs ran `cpu: host` for 9 hours on
2026-11-05, with no round running. Now:

| Guard | State |
|---|---|
| Sandbox and devbox VMs use `x86-64-v3` (no VMX; AVX2 and AES kept) | Live on all devbox VMs; measured: no VMX bit, `kvm_intel` refuses, `/dev/kvm` absent, a hand-made device node gives `ENODEV` |
| The devbox stack refuses `host` and `max`; tests refuse `host`, `max`, `+vmx`, `+svm`, `nested-virt` | Done |
| The agent places no worm unless the node's config says exactly `cpu: x86-64-v3` | Done. It does not catch a VM changed by hand and used over SSH |
| Nesting off on the node (`/etc/modprobe.d/worms-no-nesting.conf`) | **Written, not active** until a restart (`docs/status.md`, D-14) |
| `cve-search` | **Still `cpu: host` with `/dev/kvm`**, and team VMs reach its port 5000: break into it, then nest (D-17) |

**No CPU type changes these:** this node's CPU (Haswell, i7-4900MQ) reports `SMT vulnerable` for
L1TF and MDS, and L1TF Reloaded has been demonstrated against KVM: a guest may read what its
sibling hyperthread has in the L1 cache, including the node. Only hyperthreading off closes it
(D-15). Bugs in QEMU's devices (virtio NIC and disk, balloon, the cloud-init CD-ROM) and KVM's
core remain, closed only by updates. Details: `docs/archive/cpu-types.md`.

## Threat model 2: a dashboard user

No XSS, CSRF, IDOR or privilege escalation was found. Team scoping is enforced server-side in
every view and API endpoint, autoescaping is on, the JavaScript builds the DOM with
`textContent`, and the bearer-token API is the only `csrf_exempt` path.

| ID | State | Finding |
|---|---|---|
| D1 | Fixed | Anonymous requests created a session row each |
| D2 | Fixed | A devbox reset could pile up rebuilds; now one per team |
| D3 | Partly | Sign-in is rate-limited per address (10/min); no per-account lockout |
| D4 | Partly | Joins lock rows, so teams cannot overfill; joining needs no consent of the members |
| D5 | Open | A voluntary password change does not ask for the current password |
| D6 | Fixed (High) | A registered SSH key's comment was rendered as a Jinja template as root on the node. Now: strict validation on both sides and `!unsafe` in the agent's variables |
| D7 | Fixed | One key in two teams, keys never re-pushed, reset without keys, leavers keeping access. Residual: the cross-team check races in the app; the agent then fails closed |
| D8 | Open | No Content-Security-Policy (no XSS sink found) |
| D9 | Fixed | Joining, leaving and the first password change failed under the real grants |
| D10 | Design | Staff pages on the public dashboard. The role cannot mint staff (a trigger) or delete what a team or round owns; a stolen staff session can still create rounds and accounts |
| D11 | **Open** | **Uploads have no quota or rate**: 100 MiB each, stored on the dashboard VM, whose disk has 2.4 GB free. One member can fill it with about 25 distinct zips and stop the dashboard (`docs/status.md`, D-20) |
| — | Fixed | View as: admin only, session-bound, read-only, ends on any account change (`docs/accounts.md`); the admin could be left in a Dev view without a team (2026-11-05) |

## Cross-cutting

| ID | State | Finding |
|---|---|---|
| C1 | Open, deliberate | The Proxmox provider uses `insecure = true`; from a laptop the provisioning token goes to `:8006` without verification |
| C2 | Open | VM host keys are not checked (`StrictHostKeyChecking no`, `host_key_checking = False`), D-13 |
| C3 | Accepted | The migrator credentials live only on the orchestrator |
| C6 | Good | Users key-only, root `prohibit-password`, least-privilege tokens |
| S1 | Mitigated | One shared database disk (100 GB) with no retention for flows and devbox logs |
| S11 | Partly | Syslog is plain TCP by source; a VM can forge lines in its own file only; 4 connections and 1 MB/s per sender |
| S14 | Moot | Wazuh and its credential bundle were replaced by rsyslog |
| S15/F26 | Partly | A replaced VM could run with its firewall off; now rewritten on replace, but nothing watches infrastructure VMs for it |
| — | Open | The old provisioning token `terraform-prov@pve` still has `Administrator` (`docs/status.md` §2.9) |
| — | Open | No database backup |
