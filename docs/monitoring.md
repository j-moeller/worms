# Monitoring: network flows and audit logs

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

## Operations

```sh
worms-mirror status                       # on the node: every sandbox port and what it mirrors
ssh -i /etc/worms/ssh/worms debian@10.5.0.100 'systemctl status zeek zeek-shipper'
worms simulate                            # the whole chain end to end (docs/testing.md)
```

Tuning: `ansible/roles/applications/network_monitor/defaults/main.yml` and `host_mirror_*` in
`ansible/roles/host/host_network/defaults/main.yml`.
