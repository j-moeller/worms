# Memory: fitting the stack on a small node

The whole platform shares one Proxmox node (as little as **32 GB**). This
document records how RAM is budgeted and the levers that keep it in bounds.

## The problem

Every VM used to have a **fixed** allocation (`memory { dedicated = N }`, no
ballooning), so the sums had to fit in physical RAM at all times, and nothing
was ever reclaimed. The baseline (infrastructure + one private-internet VM)
alone was ~17 GB, before any devbox (2 GB/team) or a round's sandbox (~11 GB
for 8 VMs) — so the node could not hold more than a trivial deployment, and
ZFS ARC (uncapped, up to ~50 % of RAM) competed on top.

## What is done

### Ballooning everywhere (the structural win)

Every VM now declares a floor as well as a ceiling:

```
memory {
  dedicated = <ceiling>          # the maximum the guest may use
  floating  = <floor>            # the host may reclaim down to this under pressure
}
```

The Debian cloud image ships the virtio balloon driver (+ guest agent), so once
the node passes ~80 % memory use it reclaims each guest's unused pages down to
its floor. The node therefore commits the **working set**, not the sum of
ceilings. The floor defaults to **half the ceiling** (`ram_min` on the VM
modules overrides it); a service rarely needs its whole ceiling, so this alone
roughly halves the committed footprint while leaving headroom for spikes.

### Right-sized ceilings

Ceilings were generous defaults, not measurements. Trimmed to what each service
actually uses (idle RSS in brackets):

| VM | ceiling | floor | note |
|---|---|---|---|
| database | 2048 | 1024 | PostgreSQL (~250 MB + shared_buffers) |
| dashboard | 1024 | 512 | gunicorn + nginx (~300 MB) |
| orchestrator | 1024 | 512 | gunicorn + 3 daemons (~350 MB) |
| scoring / logging | 512 | 256 | Flask (1 worker) + nginx (~150 MB) |
| dns | 512 | 256 | dnsmasq (~40 MB) |
| syslog | 512 | 256 | rsyslog (~10 MB) |
| monitor | 2048 | 1024 | Zeek — kept high (buffers under traffic) |
| wikipedia | 1024 | 512 | kiwix-serve (mmaps the ZIM) |
| devbox VM | 512 | 512 | a team's playground; the floor as the operator set it |
| devbox gateway | 512 | 256 | nftables router |
| SSH gateway | 512 | 256 | sshd jump host |

### Sandbox VM RAM is a round parameter

The sandbox generator used to hand each VM 1024/2048/4096 MB at random (avg
~1.4 GB → ~11 GB for 8 VMs). It now takes a single **ceiling per VM**, set per
round on the dashboard (Staff → Rounds, "RAM per VM"; `sandbox_params.vm_ram`,
`core/schedule.py`, validated again by the round agent). The default is **512
MB**, and the floor is 512 MB as on a devbox VM (half the ceiling on a larger VM),
so a default round's 8 VMs cost ~4 GB of ceiling and ~4 GB committed. Nothing
checks the node's free RAM before a round: with 13 devbox VMs up and about 7 GB
available, 8 VMs fit and 16 do not. A worm that needs more is a per-round decision.

### rsyslog instead of Wazuh

Wazuh (manager + indexer + dashboard — a JVM indexer) was the heaviest
always-on VM (~1.8 GB real inside a 2 GB box). It was replaced by a plain
rsyslog server: the **syslog** VM (512 MB ceiling, 256 MB floor) stores the
audit logs of every infrastructure VM as files (README, "Central logging").

## Rough budget after these changes

Committed (floor) baseline drops from ~17 GB to **~4.4 GB**; a default round's
sandbox from ~11 GB to **~4 GB**; a team's devbox stays at ~2 GB, because its VMs
keep their whole 512 MB. The sum of **ceilings** for a modest deployment (infra + a few teams
+ one round) now sits around ~20–26 GB, and the host only commits a fraction of
that at a time — so 32 GB holds several teams and a running round with room to
spare.

## Still available (not yet done)

- **Cap ZFS ARC** (`zfs_arc_max`): with ZFS on the node, ARC defaults to up to
  ~50 % of RAM and competes with the VMs. A host-level cap (2–4 GB) reclaims up
  to ~13 GB. The sandbox/devbox disks are ephemeral and do not need a big cache.
  This is the single biggest remaining lever.
- **Merge scoring + logging** (same role/trust) into one VM; **Alpine** for the
  pure-router gateways (~30 MB vs 512). Both trade a little isolation/clarity
  for RAM — deliberate follow-ups, not defaults.
- A small **guest swap/zram** as an OOM safety net so ballooning can push
  floors lower.
- **KSM** (same-page merging across the homogeneous fleet) is a real saver but a
  cross-VM side channel for the hostile sandbox/devbox guests
  (`docs/security-assessment.md`, S9) — left off.
