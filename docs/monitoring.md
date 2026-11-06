# Monitoring: network flows, audit logs and event tables

## Network monitor

The monitor VM sees a copy of every frame on the sandbox networks and writes one database row
per connection (`network_flows`), plus application details and heartbeats (`network_events`).
Devbox bridges are not mirrored, so devbox traffic is not recorded.

```
 sandbox VM ──tap──┐ vmbr1xxx               monitor VM
 sandbox-gw ──tap──┘   │ worms-mirror:      net2 (capture, promiscuous, no address) ── Zeek ── JSON logs
                       │ tc ingress of      net0 (ingest) ◀── zeek-shipper (INSERT only) ──▶ database
                       └─ every port ─────▶ vmbr3 (mirror bridge, receive-only)
```

| Piece | Where | What |
|---|---|---|
| Monitor VM | `opentofu/modules/network_monitor_vm`, ephemeral stack | net0 ingest `10.12.0.100`, net1 management `10.5.0.100`, net2 on the mirror bridge (no address, no Proxmox firewall). 2 vCPU, 2 GB |
| `worms-mirror` | role `host_network`, a service on the node | Copies the ingress of every port of every `vmbr1*` bridge into `vmbr3`, re-applied on link events and every 10 s. `worms-mirror status` |
| Receive-only | `worms-mirror` (tc drop) and `worms.nft` (bridge drop on `vmbr3`) | Whatever the monitor sends on net2 goes nowhere |
| Zeek | role `network_monitor` | Standalone, as `zeek` with `CAP_NET_RAW`/`CAP_NET_ADMIN` only and no IP networking. Logs rotated hourly, kept 48 h |
| `zeek-shipper` | same role, `files/zeek_shipper.py` | Its own user; validates every value, inserts at least once (`ON CONFLICT DO NOTHING` on Zeek's `uid`), throttles rather than drops, writes a heartbeat |
| Database | `dbuser_network_monitor`, INSERT only on the two tables, 4 connections | |

**Why Zeek:** one row per connection with bytes and packets per direction, TCP state, and the
application protocol detected from the payload on any port, plus DNS, HTTP, TLS and SSH details.
Full packet capture or per-packet rows would be too much for a database; flow exporters lack
protocol detection. A Zeek-to-PostgreSQL plugin was rejected because Zeek, which parses attacker
traffic, would then hold database credentials.

**Decisions:** mirror bridge ports, not bridges (frames between two ports never pass the bridge
device); sandbox-gw's ports copy only replies from the services network, so routed frames are not
counted twice; the services bridge is not mirrored (`host_mirror_extra_bridges`); the mirror
filter coexists with Proxmox's NIC rate limit (pref 10, `continue`).

**Stored:** `network_flows`: start, duration, addresses and ports (the VM's own address, copied
before SNAT), transport, `service`, bytes and packets per direction, `conn_state`, `history`,
`uid`, `community_id`, MACs. `network_events`: `dns`, `http`, `ssl`, `ssh`, `notice`, `weird`,
`stats`, `shipper` and more, each as JSON of at most 4 KB.

```sql
SELECT src_ip, dst_ip, dst_port, service, count(*) FROM network_flows
WHERE ts BETWEEN '2026-10-22 10:00Z' AND '2026-10-22 11:00Z' GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC;
SELECT log, max(ts) FROM network_events WHERE log IN ('shipper', 'stats') GROUP BY 1;   -- alive?
```

**Limitations:** records arrive when connections end (UDP after a minute of silence); blocked
attempts are recorded too, since the copy is taken before the firewall; the first ~5 s of a new
NIC are not copied; everything is attacker-generated and can be forged; one Zeek process handles
the sandbox's rate-limited NICs, more would need a cluster. **No retention:** at the shipper's
cap of 2000 flows/s that is about 3.6 GB an hour on the shared database disk (finding S1). **No
round has run**, so the monitor has seen only test traffic; it ran live on 2026-11-04, where a
log-rotation bug (G5) was found and fixed.

## Audit logs

`SetupAudit.yml` (part of the build):

- **syslog VM** (persistent stack, siem bridge, 20 GB data disk for `/var/log/remote`): rsyslog on
  TCP 514 writes one file per sending VM, `/var/log/remote/<VM address>/audit.log`, keyed by the
  connection's address, not a hostname a compromised VM could forge. Rotated daily, kept 30 days.
  The node allows each sender at most 4 connections and 1 MB/s to it.
- **Every infrastructure VM** (database, dashboard, orchestrator, scoring, logging, monitor, DNS,
  the private internet, syslog itself): `auditd` records executed commands and changes to
  accounts, sudo, SSH, systemd units, cron and kernel modules (keys `exec`, `identity`, `config`,
  `persistence`, `modules`), and rsyslog ships them, queueing on disk while the syslog VM is
  unreachable.
- **Not** the sandbox, devbox or SSH gateway VMs (`docs/status.md`, decisions).

```sh
ssh -i /etc/worms/ssh/worms debian@10.5.0.30
sudo ls /var/log/remote/
sudo grep 'key="exec"' /var/log/remote/10.11.0.16/audit.log     # every command on the database
```

## Event tables in the database

For a forensic investigation: who did what, when, and from where. One table per kind of event,
each with the columns that kind needs, and only the important events, so they stay small: tens
to a few hundred rows a day. Read them together on the console:

```sh
worms audit                            # all tables as one timeline, the last 24 hours, newest first
worms audit --hours 168 --kind login   # a week of sign-ins (a prefix: login, password.reset, team.joined, ...)
```

| Table | Written by | Holds |
|---|---|---|
| `login_events` | dashboard | Sign-ins (`ok`, `failed`) and sign-outs, with the client's address. A failed attempt keeps the name tried only if that account exists, so a password typed into the name field is never stored; at most 30 failures a minute per dashboard process, the rest counted (`not_recorded_before`) |
| `password_events` | dashboard, console | A user set their own password (`changed`, and whether it replaced a one-time password); staff, the admin or `worms admin reset-password` issued a one-time password (`reset`, with the target's role). `password_resets` keeps its own row of a reset too |
| `impersonation_events` | dashboard | The admin viewed the dashboard as a user (`started`, `ended`) |
| `account_events` | dashboard, console | Accounts made or changed by staff (`created`, `imported`, `updated`, `activated`, `deactivated`), staff accounts from the console (`staff_created`) |
| `team_events` | dashboard, console | `created`, `deleted`, `member_added`, `member_removed` (staff), `joined`, `left` (the user), `devbox_reset` |
| `credential_events` | dashboard | Devbox SSH keys (`ssh_key_added`, `ssh_key_removed`, with sshd's `SHA256:` fingerprint) and team API tokens (`access_token_created`, `access_token_revoked`; never the token) |
| `competition_events` | dashboard | Staff changes to the competition and its rounds |
| `ssh_gateway_logins` | node | A team's SSH login through the devbox gateway: when, the key's fingerprint (`worms audit` names who registered it) and the client's public address |
| `platform_events` | node, console | The internet window (`internet_allowed`, `internet_denied`), the firewall watchdog stopping team VMs (`watchdog_stopped`, with the reason), `database_reset` (`worms reset-database` keeps every event table) |

Not recorded, on purpose: page views, API reads, uploads (the `uploads` table has them), the
platform's own SSH logins to VMs, unauthenticated SSH attempts on port 2222.

**Accounts:** each writing component has a database account of its own, used for nothing else:
`dbuser_events_dashboard` (the dashboard's `events` connection; its main role cannot write these
tables) and `dbuser_events_node` (`/etc/worms/events.env` on the node; not the round agent's
role). The console writes as the admin. An account may only `INSERT`, only into its component's
tables, only the columns it fills, from its own VM's address, over TLS. The firewall is unchanged:
both VMs already reach the database on 5432.

**What the database guarantees:** it fills `created_at` (now) and `written_by` (the inserting
account), which no writer may supply, so no row is backdated or claims another writer. No service
may read, change or delete a row; only the admin reads them. Rows are size-bounded, and hold names
and addresses, never a password, hash or token. A compromised dashboard can therefore add false
rows to its own tables, but can neither read nor rewrite the trail, nor write the node's.

**The SSH gateway:** it sees only the node, which masquerades every team. So the node logs each
new connection to port 2222 (`worms-ssh-gw:` in its kernel log, rate-limited), and
`worms-audit-gateway.timer` (every minute) reads the gateway's sshd journal, records each accepted
team login, and finds its client by the source port, which masquerading keeps. When it cannot,
`client_ip` is empty.

Code: `core/models/events.py`, `core/events.py` (dashboard and console), `scripts/round_agent/events.py`
and `/usr/local/sbin/worms-audit` (node), `queries.event_rows` (`worms audit`), the grants in
`setup_db_permissions`. Tests: `tests/test_events.py`, `tests/test_database.py`.

## Operations

```sh
worms-mirror status                       # on the node: every sandbox port and what it mirrors
ssh -i /etc/worms/ssh/worms debian@10.5.0.100 'systemctl status zeek zeek-shipper'
worms simulate                            # the whole chain end to end (docs/testing.md)
```

Tuning: `ansible/roles/applications/network_monitor/defaults/main.yml` and `host_mirror_*` in
`ansible/roles/host/host_network/defaults/main.yml`.
