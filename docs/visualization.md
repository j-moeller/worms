# Infrastructure visualization (the topology view)

The dashboard's **Network topology** page (`/topology/`, staff only) draws the
whole infrastructure and the sandboxes: the network security zones, the Linux
bridges, the VMs and the Proxmox firewall between them. It is **not static** —
it renders whatever the orchestrator last read from Proxmox, so it always
matches what is actually configured.

Work on branch `claude/jolly-ride-5az76k`.

## The pipeline (unchanged, for context)

```
Proxmox (read-only PVEAuditor token, GET only)
  └─ orchestrator: proxmox_inventory.collect()  →  TopologySnapshot(source="proxmox").data   (systemd timer, every 2 min)
       └─ dashboard: topology.analyze(data)  (pure function)  →  /topology/data/ JSON
            └─ templates/topology.html + templates/topology/topology.js  (SVG render)
```

- `orchestrator/proxmox_inventory.py` — collects bridges (incl. `comments`,
  `cidr`, `bridge_ports`, `vlan_aware`, `active`), per-guest firewall
  (options, rules, ipsets, aliases), the cluster firewall (options, security
  groups, rules, aliases, ipsets), the node firewall and the firewall-daemon
  states. Everything the view shows is already in here; it is dynamic.
- `dashboard/topology.py` — `analyze(data)` turns the snapshot into the graph:
  `bridges`, `zones`, `endpoints` (with per-NIC firewall + effective rules),
  `groups`, `flows` (computed reachability), `api_access`, `warnings`.
- `dashboard/views/topology.py` — `topology()` renders the page; `topology_data()`
  returns the JSON (adds `collected_at`, `stale`, `collection_error`).

## Network security zones (added here)

Proxmox has no "zone" object; a zone is a design concept (see the README and
`opentofu/shared/shared-variables.tf`). `analyze()` derives it from the **live**
data each bridge carries, not from a hardcoded map:

- `topology.zone_of(bridge)` reads the leading phrase of the bridge's **comment**
  (set in `opentofu/modules/network/main.tf`: "DMZ: …", "Core: …", "Ingest: …",
  "SIEM: …", "Sandbox services: …", "Mirror …", "Management …"). Sandbox
  networks are `vmbr1NNN` with a "Sandbox network …" comment
  (`opentofu/stacks/sandbox/main.tf`) and collapse into one `sandbox` zone.
  No comment / unrecognised → the bridge stands alone (routed / uplink /
  isolated by whether it has a host IP or a physical port).
- Consequence: **change a bridge's comment and the zone label follows**, with
  no code change. Zones that span several bridges (management's three, sandbox's
  one-per-network) are grouped.
- Output: a top-level `zones` list (`{id, label, kind, bridges, comment}`,
  canonical display order) and a `zone` id on every bridge. `kind` ∈
  `routed | services | sandbox | mirror | management | uplink | isolated`
  and only drives the colour.

Covered by `tests/test_topology.py` (pure, no Django/DB).

## Rendering (`templates/topology/topology.js`)

- **Zone regions**: a shaded, labelled band per zone, drawn in a bottom SVG
  layer (`layers.zones`) as the bounding box of the zone's visible bridges and
  the VMs whose data NIC (first bridged NIC) lives in it. Recomputed from live
  positions each render, so dragging a node reshapes its band. Bridges are laid
  out left-to-right grouped by zone (`orderedBridgeIds`) so bands stay coherent.
  Toggle with "Zone regions"; per-zone show/hide in "Network security zones"
  (hides the zone's bridges). Colours are CSS vars `--z-<kind>` /
  classes `.z-<kind>` in `topology.html` (light + dark).
- **Firewall rules on hover**: a floating tooltip (`.topo-tip`) shows, without
  clicking, the firewall rules for whatever is under the pointer — a VM (policy
  + per-NIC filtered state + its effective rules, e.g. `ACCEPT IN tcp/5432
  from 10.12.0.0/24`), a bridge (zone + what it accepts inbound), a zone
  (member bridges + aggregate ingress accepts) or a flow (egress/ingress
  decision). Built from the same snapshot, so it matches the live config.
  `attachTip(element, builder)`; builders `tipEndpoint/tipBridge/tipZone/tipFlow`.
  The click-through detail pane on the right is unchanged and still shows the
  full breakdown.
- Interactive elements carry `aria-label` instead of SVG `<title>` so the
  custom tooltip is the only one that shows.

### Gotcha fixed here

`.topo-empty` (the "everything is filtered out" overlay, `inset:0`) sets
`display:flex`, which **overrides the `hidden` attribute** (class vs. UA
stylesheet, equal specificity → author wins), so it stayed on top of the
canvas and swallowed every hover and click over the graph. Fixed with
`.topo-empty[hidden]{display:none}`. Any absolutely-positioned overlay added
over the canvas needs the same `[hidden]` guard.

## Extending it: round / team overlays (planned, not built)

The data shape is kept generic so these can layer on **top** of the static
view without reshaping it. The per-round timeline data already exists at
`GET /rounds/<uuid>/timeline/` (`orchestrator/rounds/timeline.py`: `nodes`,
`networks`, `edges`, `events`, `flows`, `captures`, `architecture`) — see
`docs/rounds.md`. A future player should:

- Reuse this static view as the backdrop (zones + bridges + VMs) and overlay
  the round's sandbox graph from `architecture`.
- Animate `flows` as dots moving between nodes on a single clock (seconds from
  worm start), mark VMs `captured` from `captures`, and show VMs
  active/stopped over time from `events`.
- Keep it a separate layer/toggle; do not fold round state into `analyze()`
  (that stays the live infrastructure snapshot).

The player itself is deliberately **not** implemented yet.
