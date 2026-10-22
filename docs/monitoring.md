# Network monitoring

The monitor VM sees a copy of every frame on the sandbox networks and writes
one database row per connection (`network_flows`), plus application-layer
details and its own heartbeats (`network_events`).

```
 sandbox VM ──tap──┐                         ┌── monitor VM ────────────────────────────┐
 sandbox VM ──tap──┤ vmbr1xxx                │ net2 (capture, promiscuous, no address)   │
 sandbox-gw ──tap──┘   │                     │   └─ Zeek ── JSON logs ── zeek-shipper ───┼─ net0 ─▶ database:5432
                       │ worms-mirror:       │                                           │   (INSERT only)
                       │ tc ingress of every │ net1 (management, SSH from the host)      │
                       │ port, mirred ──────▶│ vmbr3 (mirror bridge, receive-only)       │
                       ▼                     └───────────────────────────────────────────┘
```

## Pieces and where they are configured

| Piece | Where | What |
|---|---|---|
| Monitor VM | `opentofu/modules/network_monitor_vm`, ephemeral stack (`module "monitor"`) | net0 ingest `10.12.0.100` (default route; `db_client`, `log_client`, `provisioning`), net1 mgmt-exposed `10.5.0.100` (`ssh`), net2 mirror bridge (no address, Proxmox firewall off). 2 vCPU, 2 GB, 20 GB. The capture NIC's MAC goes into the inventory (`monitor_capture_mac`). |
| Mirroring | `ansible/roles/host/host_network`: `worms-mirror` (service on the Proxmox host) | Copies the ingress of every port of every `vmbr1*` bridge into `vmbr3`. Re-applied on every link event and every 10 s. `worms-mirror status` shows each port. |
| Receive-only capture NIC | `worms-mirror` (tc drop on the mirror bridge's ports) + `worms.nft` (bridge table drops what enters `vmbr3`) | Whatever the monitor sends on net2 goes nowhere. |
| Zeek | `ansible/roles/applications/network_monitor` (`SetupNetworkMonitor.yml`, part of `make provision`) | Standalone Zeek on net2, JSON logs in `/var/lib/zeek/logs`, rotated hourly into `/var/lib/zeek/archive`, deleted after 48 h. |
| zeek-shipper | same role, `files/zeek_shipper.py` | Reads the logs, inserts into the database as `dbuser_network_monitor`. |
| Schema | `projects/dashboard/core/models/monitoring.py`, migration `0003` | `network_flows`, `network_events`. |
| Grants | `ansible/roles/database/setup_db_permissions/defaults/main.yml` | `dbuser_network_monitor`: INSERT on the data columns of the two tables, nothing else. Connection limit 4. |
| Firewall | already present before this work | Proxmox security group `database` accepts `monitor`; host nftables routes `monitor → database:5432`; `pg_hba` admits `dbuser_network_monitor` from `10.12.0.100/32` only, TLS + scram. |

## How to save "all network traffic" in a database: options

We can't store every packet, so the question is which summary to keep.

| Approach | One row per | Gives | Verdict |
|---|---|---|---|
| Full packet capture (pcap) | packet (blob) | everything | Too much data for a database, which is also a poor blob store. If ever needed, keep a rolling pcap ring buffer on the monitor's disk (`tcpdump -G/-C`) instead. |
| Per-packet metadata (e.g. `tshark -T fields`) | packet | exact timestamps/sizes per packet | Orders of magnitude more rows than flows, and a worm scan is millions of packets. Rejected. |
| Flow export (NetFlow/IPFIX: softflowd, pmacct, nProbe, Argus) | flow | 5-tuple, bytes, packets, start/end | Compact, and pmacct can even write to PostgreSQL directly. But it has little or no application-protocol detection (nDPI in pmacct is an add-on). |
| **Zeek** `conn.log` (+ protocol logs) | connection | 5-tuple, bytes and packets per direction, IP-level sizes, duration, TCP state and history, **application protocol detected from the payload on any port** (DPD), Community ID, MACs; plus dns/http/ssl/ssh/... logs | De-facto standard for network security monitoring; already planned in `TODO` ("Monitoring server (Zeek)"). **Chosen.** |
| Suricata `eve.json` (flow + alerts) | flow / alert | similar flows + IDS signature alerts | Heavier; a candidate to add later for alerts. |

How the Zeek output gets into PostgreSQL:

| Option | Verdict |
|---|---|
| Zeek writer plugin for PostgreSQL (`zeek-postgresql`) | Rejected: Zeek, the process that parses attacker traffic, would hold the database credentials; the plugin must be compiled against the exact Zeek version; no input validation or throttling. |
| Logstash / Vector / Fluent Bit | Heavy (JVM) or an extra moving part, and none of them write to PostgreSQL first-class. |
| **Own shipper** (`zeek_shipper.py`, ~400 lines, stdlib + psycopg2) | **Chosen.** Small and auditable. It runs as its own user (Zeek never sees the password), validates every value, inserts at least once, throttles instead of dropping, and writes a heartbeat. |

## What is stored

`network_flows`: one row per connection (Zeek `conn.log`), i.e. per
TCP connection, UDP "conversation" (ends after 1 min of silence) or ICMP exchange:

| Column | Meaning |
|---|---|
| `ts`, `duration` | start of the connection, duration in seconds |
| `src_ip`, `src_port`, `dst_ip`, `dst_port` | originator → responder (ICMP: type/code in the port columns). The sandbox VM's own address: the copy is taken before sandbox-gw's SNAT. |
| `transport` | `tcp`, `udp`, `icmp` |
| `service` | application protocol(s) recognised **from the payload** (`http`, `ssl`, `dns`, `ssh`, `ftp`, `smtp`, ...; comma-separated; empty if unknown), independent of the port |
| `src_bytes`, `dst_bytes` | payload bytes per direction |
| `src_pkts`, `dst_pkts`, `src_ip_bytes`, `dst_ip_bytes` | packets and bytes on the wire (IP header included) per direction: the "size" |
| `conn_state`, `history` | e.g. `SF` normal, `S0` unanswered SYN (scans), `REJ` rejected; packet history like `ShADadFf` |
| `missed_bytes` | bytes Zeek did not see (capture loss) |
| `uid` | Zeek's connection id (unique; joins `network_events`) |
| `community_id` | standard flow hash, the same in Suricata, Wireshark, ... |
| `src_mac`, `dst_mac` | layer-2 addresses |
| `inserted_at` | database time of the insert |

`network_events`: `log` (`dns`, `http`, `ssl`, `ssh`, `ftp`, `smtp`,
`dhcp`, `notice`, `weird`, `software`, `stats`, `capture_loss`, `shipper`),
`ts`, `uid`, and `data`, the Zeek record as JSON, at most 4 KB (long strings
and lists are cut). Examples: DNS names queried, HTTP host/URI/user agent/status,
TLS server name, SSH client/server versions.

Example queries:

```sql
-- Who talked to whom in a round (by time window), with volume.
SELECT src_ip, dst_ip, dst_port, transport, service, count(*) AS flows,
       sum(src_ip_bytes + dst_ip_bytes) AS bytes
FROM network_flows WHERE ts BETWEEN '2026-10-22 10:00Z' AND '2026-10-22 11:00Z'
GROUP BY 1, 2, 3, 4, 5 ORDER BY bytes DESC;

-- Scanning: unanswered or rejected connection attempts per source.
SELECT src_ip, count(*) FROM network_flows
WHERE conn_state IN ('S0', 'REJ', 'RSTOS0') GROUP BY 1 ORDER BY 2 DESC;

-- HTTP requests of one connection.
SELECT e.data FROM network_events e JOIN network_flows f USING (uid)
WHERE f.uid = 'CHhAvVGS1DHFjwGM9' AND e.log = 'http';

-- Is the monitor alive? (heartbeats every minute)
SELECT log, max(ts) FROM network_events WHERE log IN ('shipper', 'stats') GROUP BY 1;
```

## Decisions

- **Mirror the bridge ports, not the bridges.** Frames forwarded between two
  ports of a Linux bridge never pass the bridge device's qdiscs. The old
  `helpers/monitor_network.sh` mirrored the bridge device, so it only saw
  broadcasts and traffic to/from the host. `worms-mirror` mirrors the ingress
  of every port: each frame enters its bridge through exactly one port, so it
  is copied exactly once. Verified in `tests/test_worms_mirror.py`.
- **sandbox-gw's ports copy only replies from the services network.** Frames
  the router forwards between two sandbox networks were already copied on the
  first network; copying them again would double every routed packet.
  Replies of scoring/logging/DNS/private internet (source `10.2.0.0/24`) are
  only visible there, so those are copied.
- **The services bridge is not mirrored** (configurable:
  `host_mirror_extra_bridges`). Everything the sandbox sends there is already
  seen on the sandbox side, with the VM's real address instead of
  sandbox-gw's SNAT address. Infrastructure traffic is never mirrored.
- **Coexisting with Proxmox' rate limit.** Proxmox puts its NIC rate limit on
  the same ingress qdisc (filter pref 50). Our filter has pref 10 and ends in
  `continue`, so the rate limit still applies (with mirred's default `pipe`
  it would be skipped: tested). A new port is left alone for 5 s so
  `worms-mirror` never races Proxmox while it plugs a starting VM's tap.
- **u32 classifier**, not matchall/flower: present in every kernel, so the
  same filters run in the test suite's network namespaces.
- **Capture NIC without Proxmox firewall.** The mirrored frames are addressed
  to other VMs; with the VM firewall (input policy DROP) they would be
  dropped. Receive-only is enforced on the host twice instead (tc drop on the
  port, nftables bridge drop), and inside the VM the NIC has no address, no
  ARP, IPv6 off.
- **Zeek runs standalone** (no zeekctl), as user `zeek` with
  `CAP_NET_RAW/CAP_NET_ADMIN` only, read-only system, no IP networking
  (`IPAddressDeny=any`; capture uses packet sockets). `ignore_checksums`: the
  copies come from virtio NICs with checksum offload, and their checksums are
  often not filled in yet, so Zeek would discard them.
- **Zeek from the Zeek project's OBS repository.** Zeek is not in Debian (the
  old cloud-init `packages: zeek` would have made the whole package install
  fail). The key is fetched over HTTPS from the same repository while internet
  access is allowed. `network_monitor_zeek_package: zeek-8.0` pins the LTS line.
- **zeek-shipper is a separate user and process.** It reads the logs (group
  `zeek`, read-only), holds the database password (root `0600` env file read
  by systemd), may only reach the database (`IPAddressAllow`), and sees no
  packets.
- **At-least-once, deduplicated.** A file position is saved only after the
  rows read up to it are committed. Flows are inserted with
  `ON CONFLICT DO NOTHING` on the unique `uid`, which needs no SELECT
  privilege (tested), so a replay never duplicates a flow.
- **Throttle, don't drop.** The shipper caps rows per second (2000 flows,
  1000 events; bursts 10×). What is over the cap stays in the log files: the
  48 h archive is the buffer. The backlog is reported in the `shipper` event
  every minute.
- **Typed flows, JSON details.** Flows are queried by address, port and time,
  so they get real columns (`inet`) and indexes. The per-protocol logs have
  very different fields, so they go into `jsonb`, size-limited.
- **No round reference.** A flow belongs to the round whose time window
  contains `ts`. The monitor has no business reading the rounds table.

## Operations

```
make host-network                 # worms-mirror on the host (part of make)
make infrastructure               # the monitor VM (ephemeral stack, part of make)
make provision                    # Zeek + shipper (SetupNetworkMonitor.yml, needs internet)
worms-mirror status               # on the host: every sandbox port and what it mirrors
journalctl -t worms-mirror        # changes it made
ssh debian@10.5.0.100 'systemctl status zeek zeek-shipper; journalctl -u zeek-shipper -n 20'
make simulate                     # end-to-end check (docs/testing.md)
```

Tuning: `ansible/roles/applications/network_monitor/defaults/main.yml`
(logs shipped, rates, retention, rotation) and `host_mirror_*` in
`ansible/roles/host/host_network/defaults/main.yml`.

## Limitations

- **Records arrive when connections end.** Zeek writes a connection when it
  closes or times out: short TCP connections within seconds, UDP after a
  minute of silence, long-lived connections only at their end.
- **Blocked attempts are recorded too.** The copy is taken where a frame
  enters the bridge, before the Proxmox firewall and the host's rate limits.
  A flow row means a VM *tried*; whether it got through shows in
  `conn_state`/`history` (e.g. `S0`). This is what you want for worm scanning.
- **The first ~5 s of a new NIC are not copied** (settle time, see
  Decisions). VMs take longer than that to boot.
- **sandbox-gw itself is hardly visible**: frames it forwards between sandbox
  networks and its own ICMP errors are not copied a second time.
- **Everything is attacker-generated**, the monitor's own input included.
  Values are validated and size-limited, but they can be forged (by a sandbox
  VM, or by a compromised sandbox-gw for the services replies).
- **Capacity:** one Zeek process with libpcap. That's fine for the sandbox's
  rate-limited NICs (10 MB/s each). Much more traffic would need AF_PACKET
  fan-out and a Zeek cluster. Storage: roughly 0.5 KB per flow including
  indexes; at the 2000 flows/s cap that is ~3.6 GB/hour, so plan retention
  (e.g. delete or partition by `ts` per round).
- **Not run live yet.** This was built and tested without a Proxmox host.
  Zeek could not be installed here (OBS was not reachable). On the first
  `make provision`, Zeek parses the configuration before it is installed
  (`zeek --parse-only`); `make simulate` then verifies the whole chain.
