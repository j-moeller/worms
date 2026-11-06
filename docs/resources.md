# Memory: fitting the platform on one node

The whole platform shares one Proxmox node. `worms-dev` has 32 GB; on 2026-11-06, with 28 VMs
running (15 infrastructure and private internet, 13 devbox), 24 GB were in use and 6 GB
available. `worms-pve`'s hardware has not been checked.

## How memory is budgeted

**Ballooning everywhere.** Every VM has a ceiling (`dedicated`) and a floor (`floating`); above
about 80 % use the node reclaims each guest's unused pages down to its floor, so it commits the
working set, not the sum of ceilings. The floor defaults to half the ceiling (`ram_min`).

| VM | Ceiling | Floor (MB) |
|---|---|---|
| database | 2048 | 1024 |
| dashboard, orchestrator | 1024 | 512 |
| monitor | 2048 | 1024 |
| scoring, logging, dns, syslog | 512 | 256 |
| wikipedia | 1024 | 512 |
| devbox VM | 512 | 512 (set by the operator; the stack keeps it) |
| devbox gateway, SSH gateway | 512 | 256 |
| sandbox VM | the round's **RAM per VM**, default 512 | 512, or half a larger ceiling |

**Rough budget:** the infrastructure commits about 4.4 GB; a team's devbox about 2 GB (its VMs
keep their whole 512 MB); a default round's 8 sandbox VMs about 4 GB. **Nothing checks free RAM
before a round or a devbox build**: with 13 devbox VMs up and about 7 GB free, 8 sandbox VMs fit
and 16 do not. Each additional team costs about 2 GB, so this node has room for about 3 more
teams, or 1 while a default round runs. The devbox addressing caps the platform at **16 teams**: the
teams' link networks (10.128.0.0/19, `scripts/generate_devbox.py`) end at index 15, although the
services block would have 32 addresses. `worms devbox provision` refuses a 17th team.

**ZFS ARC** is capped at 3.2 GB (`zfs_arc_max`, the Proxmox installer's 10 % default).

## Levers not used

- **KSM** (same-page merging) saves memory across the identical VMs, but it is a cross-VM side
  channel for hostile guests. It is **not off**: `ksmtuned` is active and switches it on under
  memory pressure; on 2026-11-06 it shared 583 420 pages (2.2 GB). Turning it off is a security
  decision (`docs/status.md`) and costs that memory.
- Merging scoring and logging into one VM; Alpine for the pure-router gateways (about 30 MB instead
  of 512).
- Guest swap or zram, so floors could go lower.
