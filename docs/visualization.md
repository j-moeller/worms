# Infrastructure visualization (the TOPOLOGY view)

The dashboard's **TOPOLOGY** page (`/topology/`, staff only) shows the
infrastructure as it is configured right now, and replays any evaluated round
like a video: connections as moving dots, events as icons on the VMs, a
seekable timeline.

It is not a static drawing: the infrastructure comes from the orchestrator's
read-only Proxmox snapshot, the replay from the database (run events, the
monitor's flows, the scoring/logging records).

## What you see

### The picture (fixed layout)

```
 [Proxmox host]              [orchestrator]               [dashboard]
           [monitor]          [ database ]          ([wazuh])
                       [scoring]        [logging]
 ┌ Devbox team-a ◇┐  ┌──────── SANDBOX ◇ ─────────┐  ┌ PRIVATE INTERNET ┐
 │ A ── B ── C    │  │  vm  vm  vm                 │  │ dns              │
 └────────────────┘  │ (vmbr1000) (vmbr1001)       │  │ apt-cache        │
 ┌ Devbox team-b ◇┐  │  vm  vm  vm                 │  │ devpi …          │
 │ A ── B ── C    │  └─────────────────────────────┘  └──────────────────┘
 └────────────────┘
```

- **VMs are first-class nodes** with their Proxmox status: a green dot when
  running; dashed and grey when not (stopped, or for a replayed run: not yet
  built / stopped by the game plan / destroyed). A role whose VM is missing
  from the snapshot is drawn as a faded placeholder ("not in the snapshot").
- **Only operational connections**; management networks (every maintenance
  bridge, the devbox SSH fabric) and the mirror are never drawn. They still
  appear on the hover card, marked "not drawn".
  - **Database ingress** (blue arrows): read from the database VM's own
    firewall. Every accepted inbound tcp/5432 rule's source is resolved to a
    node (IP, CIDR, range, alias, `+ipset`), so the arrows show exactly who the
    firewall lets in: the Proxmox host (round agent / admin), orchestrator,
    dashboard, scoring, logging, monitor. Sources that resolve to nothing are
    listed in the side panel.
  - **Sandbox / each devbox → scoring, logging, private internet** (teal /
    green): from the group's gateway (the ◇ on the group's edge), verified
    against the target's inbound rules (scoring/logging admit the sandbox-gw
    and the devbox-gw block; the private internet admits the services
    bridge). Dashed = by design but **not verified** (no live gateway, e.g. no
    run in progress, or no matching rule). Devbox traffic runs up a shared
    trunk between the devbox column and the sandbox; everything bound for the
    private internet joins one lane above the groups.
  - **Sandbox networks** as hubs (`vmbr1NNN` with their CIDR), each VM linked
    to the networks it is on; **devbox links** A–B and B–C drawn directly,
    labelled with their bridge. Nothing ever links two teams, or a devbox and
    the sandbox.
- **Hover** a VM: its NICs with the bridge (`vmbr…`) and the bridge's kind,
  the firewall options (policies, ipfilter), the attached security groups
  (with the NIC they are on) and the effective rules. **Click** for the full
  tables in the side panel. Hover an edge for why it exists (the rule/group),
  a hub for its members, a ◇ for the gateway VM.
- **Auxiliary VMs** (checkbox): wazuh, the devbox SSH gateway and unclassified
  VMs. Gateways are always represented by their ◇.

### Tabs: a round, then a team

`Infrastructure (live)` · one tab per round of the current competition · then
one tab per team that has a run in that round (with the run's state). Picking a
team loads its run; the sandbox box then shows **that run's** VMs and networks
(from the run's records, so finished runs replay after their sandbox is gone)
and only **that team's** devbox. The infrastructure's status stays live (the
header says so).

### The player

| | |
|---|---|
| ▶ / ❚❚, space | play / pause |
| slider | drag or click anywhere to seek (like a video); event marks (captures, worm start, VM stops/starts, secrets, state changes) and a traffic-density strip with flag submissions and logs |
| −10s / +10s, ← / → (shift: 60 s) | step |
| ⏮ / ⏭, `,` / `.` | previous / next event (pauses and opens it) |
| speed | 1×–600×; the default plays the whole run in about 90 s |
| `F` / Fit, wheel, drag | fit / zoom / pan |
| `#run=<uuid>&t=<s>` | the URL: shareable, reopens the run at that time |

Time is in seconds from the worm's start (negative while the sandbox is
prepared and the worm deployed).

- **Dots**: every connection the monitor recorded, travelling along the drawn
  edges (VM → its network → the gateway → scoring / logging / a private
  internet VM, or VM → network → VM). Colour = destination (VM↔VM, scoring,
  logging, private internet/DNS); a ring = not established (unanswered or
  rejected); a dot to an unknown destination fades at the gateway. Dots travel
  for the connection's duration at the current speed (at least 0.8 s, at most
  3 s on screen).
- **Event icons** pop up on the VM they concern and fade after ~3 s:
  worm deployed / started (start VM), secret released, VM stopped / started
  (game plan), **captured** (the VM keeps a flag marker and a red outline from
  then on), flag submitted (on scoring ✓/✗ and on the submitting VM), log
  received (on logging and on the sending VM), run state changes (on the
  orchestrator; host-job states also on the Proxmox host), errors.
- **Click an icon or a dot**: playback pauses and the side panel shows the full
  details — for a log the (size-capped) log record, for a flag submission the
  result (captured / duplicate / not counted / invalid / pending), the hash
  prefix and the submitting VM, for a connection ports, protocol, state,
  duration, bytes/packets and the Zeek uid. "Continue" resumes.
- The side panel otherwise lists the run's summary and its events (click one
  to jump there).

Scoring and logging only see the gateway's address (SNAT), so the VM shown as
the sender of a flag or a log is **inferred** from the monitor: the run VM
whose connection to that server was open at the time. It is marked
"(inferred)"; with no matching connection the sender is "unknown".

### Download and open a JSON file (the replay bundle)

- **Download JSON** saves what is on screen as one self-contained file: the
  infrastructure model and — on a round/team tab — that run's timeline (on
  the live tab, the infrastructure only). It is built in the browser from
  the data the page shows; nothing else is needed to show it again.
- **Open JSON…** (or drop a file onto the view) shows such a file in its own
  tab, `File: <name>`, with the full player: the same picture, hover cards,
  dots, icons, details and slider. The file is read locally — it is never
  uploaded, and nothing is fetched while it is shown (the page works as a
  viewer even when the dashboard has no snapshot or the run is gone).
  Downloading again from a file tab gives the same data back.

Format (`version` 1):

```json
{
  "format": "worms-topology-replay",
  "version": 1,
  "exported_at": "2026-10-24T17:05:07.000Z",
  "source": {"mode": "round", "run": "<run uuid>", "from_file": null},
  "model":    { "host": {…}, "nodes": […], "groups": […], "networks": […],
                "edges": […], "security_groups": {…}, "unresolved": {…},
                "collected_at": "…", "stale": false, "collection_error": "" },
  "timeline": { "run": {…}, "nodes": […], "networks": […], "edges": […],
                "vm_status": {…}, "events": […], "flows": […],
                "captures": […], "truncated": {…} }
}
```

`model` is the infrastructure model below, `timeline` the run timeline below
(or `null`), each restricted to the fields the view uses (the address
index and the generator graph are not included). A file can come from
anywhere, so the page validates it like untrusted input — and so does every
server response, through the same code (`cleanModel` / `cleanTimeline` in
`app.js`):

- the envelope must match (`format`, a `version` the page knows);
- only known fields are kept, each coerced to its type (strings capped,
  numbers finite, times within ±10⁸ s, unknown roles → `other`), entries that
  are not objects or have no time are dropped, events and flows are sorted
  and event ids reassigned, the time range is recomputed;
- list sizes are capped (e.g. 500 000 flows, 200 000 events, 10 000 nodes)
  and the file is refused above 64 MiB (`data-max-file-bytes`);
- `__proto__` / `constructor` / `prototype` keys are ignored;
- everything is rendered as text, as always.

A file that fails is refused with a message; the view stays as it was.

## Data and code

```
Proxmox (read-only PVEAuditor token, GET only)
  └ orchestrator: manage.py collect_topology (timer, every 2 min)
       → topology_snapshots (one row)
            └ dashboard: dashboard/infra_view.py  analyze(snapshot)  → /topology/data/
database: run_events, virtual_machines, network_flows,
          flag_capture_attempts/flag_captures, logging_attempts/logging
            └ orchestrator/rounds/timeline.py  build(run, infra=address index) → /rounds/<uuid>/timeline/
round_runs/rounds  → /topology/runs/  (the tabs)
page: templates/topology.html  (endpoints; links the static CSS/JS)
      templates/topology/player.html  (markup only, no template tags)
      static/dashboard/css/topology.css  (the player's styles)
      static/dashboard/js/topology.js   (the scene, the player)
```

| Endpoint | View | Returns |
|---|---|---|
| `/topology/data/` | `views/topology.py: topology_data` | the infrastructure model + `collected_at`, `stale` (> 15 min), `collection_error`; 503 with a hint before the first collection |
| `/topology/runs/` | `views/topology.py: topology_runs` | rounds of the current competition (newest first) with their runs (uuid, team, state, position) |
| `/rounds/<uuid>/timeline/` | `views/rounds.py: round_timeline_data` | the run's timeline (below) |

All three are **staff only** (anonymous → login redirect, non-staff → 403),
`never_cache`.

### The infrastructure model (`dashboard/infra_view.py`, pure)

- `nodes` — the Proxmox host (id `proxmox`; its addresses are its bridge
  addresses) and every guest (`vm:<vmid>`): `role` (proxmox, orchestrator,
  dashboard, database, scoring, logging, monitor, wazuh, dns,
  private_internet, sandbox, sandbox_gw, devbox, devbox_gw, ssh_gateway,
  other — from the VM name, tags and pool as OpenTofu sets them), `status`,
  `active`, `team`/`devbox_role` for devbox VMs, `aux`, operational
  `addresses`, `nics` (bridge, bridge kind, address, firewall flag) and
  `firewall` (options, attached `groups`, expanded `rules`).
- `groups` — `group:sandbox` (always present, possibly empty) and
  `group:devbox:<slug>` per team, each with its `gateway` and `members`. The
  view adds `team_name`/`label` from the teams table (devbox VMs only carry
  the slug `t<team id>`).
- `networks` — the sandbox / devbox bridges with their members and CIDR.
- `edges` — `db`, `service` (`verified`, `targets` for the private
  internet), `lan` (devbox), `lan_hub` (sandbox).
- `bridges` — every bridge with its kind (from the live name and the comment
  OpenTofu sets: change a comment and the kind follows); `security_groups` —
  the cluster groups (for replayed sandbox VMs' hover cards);
  `unresolved.database_sources`; `index.by_ip` / `by_role`.

`address_index(model)` gives the timeline an address → node map for the far
end of the sandbox's connections.

### The timeline (`orchestrator/rounds/timeline.py`)

`run` (incl. `team_slug`, `duration`, `t_min`/`t_max`), `nodes` (the run's
VMs: addresses, networks, start VM, `captured_at`), `networks`, `edges`,
`vm_status` (each VM's power state over time: running once built, the game
plan's stops/starts, stopped at ENDING, destroyed at the end), `events` (run
events + derived `flag_submitted` on `role:scoring` and `log_received` on
`role:logging`; each has an `id`, `t`, `node`, `vm`, `data`), `flows` (every
connection with an end in the run, the far end mapped through the address
index: `kind` lan / scoring / logging / dns / private_internet / other,
duration, state, bytes, packets, uid), `captures`, `architecture`,
`truncated`. Limits: 20 000 flows, 5 000 submissions, 5 000 logs, 4 000
characters per log record.

## Security

- Staff only, because addresses and firewall rules are sensitive. The dashboard
  never talks to Proxmox (the orchestrator's read-only token does, GET only,
  config fields whitelisted, no cloud-init secrets).
- Flows, log records and submitted flags are **attacker-controlled** (they come
  from the sandbox). The page renders every value with `textContent` / SVG
  text nodes — nothing is parsed as HTML — and the browser test feeds it an XSS
  payload in a log record to prove it (`tests/test_topology_page.py`).
- The timeline caps what it returns (flows, submissions, logs, log size) so a
  noisy worm cannot blow up the page.
- A downloaded bundle contains the same staff-only data as the page
  (addresses, firewall rules, the run's traffic): handle it like the page.
  An opened file is untrusted input: validated, size-capped, rendered as
  text, never uploaded (above).

## Tests

| File | What |
|---|---|
| `tests/test_infra_view.py` | The model on a realistic snapshot (`tests/infra_fixture.py`): roles and status, no management network as an edge, database ingress from its firewall (and following rule changes), unresolvable sources reported, groups and networks, devbox A–B/B–C links never crossing teams, sandbox hubs, verified / unverified service edges, hover data, the address index, firewall-spec resolution and port matching. |
| `tests/test_rounds.py` | The timeline against PostgreSQL with the real conductor: VM status over time, flows mapped to scoring / the private internet, flag submissions (captured / invalid, the other team's not shown) and logs attributed to the VM whose connection was open, attacker text passed through as data, log truncation; the three endpoints (staff only, 503 before a snapshot, team names). It also pins the browser test's fixture to the builder's real schema. |
| `tests/test_topology_page.py` | The page in Chromium (Node Playwright, `tests/browser/topology_player.js`) with fixture data: the layout rules above, live status, the hover card, round/team tabs, playback and dots, a badge click pauses and opens the event, a captured VM is marked, a VM stopped and restarted by the game plan, a dot click, next-event and the URL hash, the live refresh keeping the chosen speed and zoom, the XSS payload shown as text, a run that fails to load, back to live, no script errors. The replay bundle (`tests/browser/topology_replay.js`): download from a round and from the live tab, open the file in a page whose endpoints all fail (offline viewer: playback, badges, nothing fetched), a lossless round trip, an infrastructure-only file, broken files refused with the view kept (not JSON, wrong format, unknown version, bad structure, too large), and a hostile file (XSS strings, `__proto__` keys, unknown roles, junk entries, non-finite times) opened without executing anything. `WORMS_SCREENSHOTS=<dir>` keeps the screenshots. |

Not covered: a real Proxmox snapshot and a real round's traffic (no node
here); the layout with very large sandboxes or many teams is only exercised
with the fixtures (it scales by growing the boxes; zoom/pan/fit handle the
rest).
