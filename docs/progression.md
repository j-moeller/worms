# Progression: the replay format, the PROGRESSION and TOPOLOGY pages

A **progression** is everything that happened to a team in a round (or on its devbox) on one
clock: the VMs its worm captured, the flags it submitted, its logs and its network traffic. One
format serves every use:

- the team's **PROGRESSION** page replays it like a video;
- the staff **TOPOLOGY** page replays any run with the same player, against the live
  infrastructure;
- the **team API** serves it as JSON and as a zip (`docs/team_api.md`);
- the player opens a downloaded `.zip` or `.json` again, offline.

Builder: `orchestrator/rounds/progression.py` (read-only), on `orchestrator/rounds/timeline.py`.
Player: `dashboard/static/dashboard/js/topology.js`.

## What a team sees, and never sees

A team's document (`audience: "team"`) is **built** from what it may see, not filtered in the
browser:

| Shown | Never shown |
|---|---|
| Live: the sandbox VMs it captured, and its entry VM from the worm's start | Any other sandbox VM: not as a node, a connection's far end or in an event |
| Dev: every VM of its own devbox | Other teams' devboxes or data |
| Scoring, logging, DNS and the private internet | Database, orchestrator, dashboard, monitor, syslog, the node, the gateways |
| Its VMs' addresses and networks (by CIDR, under an opaque id) | Bridge names, VMIDs, maintenance and SSH-fabric addresses, pools |
| State changes, worm start, secrets and stops of its visible VMs, its captures, submissions (result and hash prefix) and logs | Firewall rules, NICs, run errors, the generator's architecture, internal ids, secrets |

**Live:** one document per round and team, from that run's timeline; zero is the worm's start.
**Dev:** the devbox has no rounds, so there is one document (`round: null`) from the last reset up
to now, never finished, with no network data (devbox traffic is not recorded). Rounds of a
development competition are still served by the API as a window on the devbox, but the page
does not use them.

## The document (`worms-progression`, version 1)

```json
{
  "format": "worms-progression", "version": 1, "audience": "team", "environment": "live",
  "round": {"id": 3, "name": "R1", "competition": "main", "start": "…", "end": "…"},
  "team":  {"uuid": "…", "name": "team-a"},
  "clock": {"t0": "…", "round_start": -300.0, "round_end": 28500.0, "now": 900.0, "finished": false, "t_min": -60.0, "t_max": 640.0},
  "scene": {"host": {}, "nodes": […], "groups": […], "networks": […], "edges": […]},
  "timeline": {"run": {…}, "nodes": […], "networks": […], "edges": […], "vm_status": {…}, "events": […], "captures": […], "truncated": {…}},
  "logs":    {"count": 12, "included": false, "url": "/api/v1/rounds/3/progression/logs/{ref}/"},
  "network": {"available": true, "count": 4210, "included": false, "url": "/api/v1/rounds/3/progression/flows/"}
}
```

- Every `t` is seconds from `clock.t0`; `now` is set while the round is not finished.
- `timeline.events`: `state`, `worm_deployed`, `worm_started`, `secret_released`, `vm_stopped`,
  `vm_started`, `captured`, `flag_submitted` (result captured / duplicate / not_counted / invalid
  / pending), `log_received` (a **reference** `{ref, size}` to the record).
- Scoring and logging only see the gateway's address, so the VM shown as a submission's sender is
  **inferred** from the monitor: the VM whose connection to that server was open at the time.
- `flows` are optional: inline in a single JSON, in the zip's `network/flows.jsonl`, or fetched
  from `network.url`.

**The zip** (`…/progression.zip?logs=0|1&network=0|1`): `progression.json`, `README.txt`,
`logs/<ref>.json`, `network/flows.jsonl`.

**Limits:** 20 000 connections, 5 000 submissions, 5 000 logs, a log record at most 4 KiB, so a
document is a few MiB at most. The team API's rate limits and 3 s statement timeout apply.

## Endpoints

| | Team API (bearer token) | PROGRESSION page | Staff (TOPOLOGY) |
|---|---|---|---|
| rounds | `/api/v1/rounds/` | `/progression/rounds/` | `/topology/runs/` |
| document | `/api/v1/rounds/<id>/progression/` | `/progression/rounds/<id>/` | `/rounds/<run>/progression/` |
| connections / one log | `…/flows/` / `…/logs/<ref>/` | the same | the same |
| zip | `/api/v1/rounds/<id>/progression.zip` | `…/download/` | `…/download/` |
| devbox | `/api/v1/devbox/progression/` (`.zip`) | `/progression/devbox/` | |

## The TOPOLOGY page (staff)

`/topology/` shows the infrastructure as configured now, in a fixed layout: the node, orchestrator
and dashboard on top, the database, scoring and logging, then the devboxes, the sandbox and the
private internet. VMs carry their Proxmox status. Only operational connections are drawn:
database ingress as its firewall allows it, sandbox and devbox → scoring, logging and the private
internet (verified against the firewall), the sandbox networks and the devbox links. Hovering a
VM shows its NICs, bridges, security groups and rules.

Tabs pick a round, then a team, to **replay that run**: connections as dots along the edges,
events as icons on the VMs (click to pause and see details), a seekable timeline,
`#run=<uuid>&t=<s>` in the URL. **Download** saves the run as a zip or what is shown as one JSON;
**Open file…** views either again, offline.

Data: the orchestrator runs `manage.py collect_topology` every 2 minutes with the read-only
Proxmox token (GET only, config fields whitelisted, no cloud-init secrets) and upserts one row in
`topology_snapshots`; the dashboard analyses it (`dashboard/infra_view.py`) and serves it at
`/topology/data/`. Staff only, because addresses and firewall rules are sensitive.

## Security

- What a team sees is decided on the server, for every endpoint and the zip.
- Log records, flows and submitted hashes are attacker-controlled. The player renders every value
  as text, and every file and response passes the same sanitizer (`cleanProgression`,
  `cleanModel`, `cleanTimeline`): known fields only, coerced types, capped sizes and counts,
  `__proto__` keys ignored, a zip member that inflates beyond its claim refused, a file's URLs
  never fetched.
- A staff download holds staff-only data (addresses, rules, traffic).

Tests: `tests/test_progression.py` (visibility rules, Dev windows, the clock, the zip, every
endpoint), `tests/test_infra_view.py` (the infrastructure model), `tests/test_topology_page.py`
(both pages in Chromium, with hostile files). Not covered: a real Proxmox snapshot and a real
round's traffic.
