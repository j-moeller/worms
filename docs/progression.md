# Round progression: the format, the PROGRESSION page, the downloads

A team's **progression** through a round is everything that happened to it in
that round, on one clock: the VMs its worm captured, the flags it submitted,
the logs it sent and its network traffic. One format serves every use:

- the **PROGRESSION** page (navigation, signed-in teams) replays it like a
  video — the team's view of the round player;
- the staff **TOPOLOGY** page replays any run with the same player
  (`docs/visualization.md`), from the same document with everything in it;
- the **team API** serves it as JSON, piece by piece, and as one **zip**
  (`docs/team_api.md`);
- the player opens a downloaded `.zip` or `.json` again, offline.

The builder is `orchestrator/rounds/progression.py` (pure read model, writes
nothing); the player is `dashboard/static/dashboard/js/topology.js`.

## What a team sees — and never sees

A team's document (`audience: "team"`) is **built** from what the team may
see, not filtered in the browser:

| Shown | Never shown |
|---|---|
| the sandbox VMs it **captured in the round** (Live) — a VM appears faint until the moment of its capture | sandbox VMs it did not capture: not as a node, not as the far end of a connection, not in an event (an event or a flow involving one is dropped; a submitted flag's VM or an inferred sender that is not visible is blanked) |
| its **own devbox** VMs whose token it submitted during the round (Dev) | other teams' devboxes, other teams' data |
| scoring, logging, the private internet (DNS and the mirrors) — what the sandbox can reach | the database, orchestrator, dashboard, monitor, wazuh, the Proxmox host, the gateways (traffic to them is dropped) |
| its VMs' addresses and networks (by CIDR, under an opaque id: `net-1`, …) | bridge names (`vmbr…`), Proxmox VMIDs, maintenance and SSH-fabric addresses, pools |
| run state changes (`{state}` only), worm start, secrets released and VM stops/starts **of its visible VMs**, its captures, submissions (result, hash prefix of what it submitted) and logs | firewall rules, security groups, NICs, run errors, the generator's architecture, internal ids (run uuid, attempt ids), secrets, tokens, passwords |

Staff documents (`audience: "staff"`) carry the run's whole timeline; the
staff page draws it against the live infrastructure model.

## Live and Dev

- **Live** (the competition): one run per round and team (`round_runs`).
  The document is that run's timeline (`orchestrator/rounds/timeline.py`):
  run events, submissions and logs within the run's window, the monitor's
  connections. Its clock's zero is the worm's start.
- **Dev** (the team's development competition): there are no runs — the
  devbox is always on. The document is what the team's devbox submitted and
  logged during the round's window (from its start up to now, or its end).
  A devbox VM counts as captured in the round when a submission in the window
  carried its token (scored, or already scored at this devbox version). The
  monitor records the sandbox only, so **a Dev document has no network
  data** (`network.available: false`). Its clock's zero is the round's start.

Which one a round is follows from its competition (the team's development
competition → Dev). The PROGRESSION page shows the rounds of the environment
switched on in the header (Live / Dev).

## The document (`worms-progression`, version 1)

```json
{
  "format": "worms-progression",
  "version": 1,
  "generated_at": "2026-10-24T12:00:00+00:00",
  "audience": "team",
  "environment": "live",
  "round":    {"id": 3, "name": "R1", "competition": "main", "start": "…", "end": "…"},
  "team":     {"uuid": "…", "name": "team-a"},
  "clock":    {"t0": "2026-10-24T10:05:00+00:00", "round_start": -300.0, "round_end": 28500.0,
               "now": 900.0, "finished": false, "t_min": -60.0, "t_max": 640.0},
  "scene":    {"host": {}, "nodes": […], "groups": […], "networks": […], "edges": […]},
  "timeline": {"run": {…} | null, "nodes": […], "networks": […], "edges": […],
               "vm_status": {…}, "events": […], "captures": […], "truncated": {…}},
  "logs":     {"count": 12, "truncated": false, "included": false, "path": null,
               "url": "/api/v1/rounds/3/progression/logs/{ref}/"},
  "network":  {"available": true, "reason": null, "count": 4210, "truncated": false,
               "included": false, "path": null, "url": "/api/v1/rounds/3/progression/flows/"}
}
```

**clock** — every `t` in the document is seconds from `t0`.
`round_start` / `round_end` are the round's bounds on that axis; `now` is set
while the round is **not finished** (`finished: false`) — the round can be
downloaded while it runs, and a running run plays up to now; `[t_min, t_max]`
is the span to play.

**scene** — what is drawn around the timeline, in the shape of the
infrastructure model (`dashboard/infra_view.py`): for a team, scoring
(`role:scoring`), logging (`role:logging`), the private internet
(`pi:<name>`) and one group (`group:sandbox`, or `group:devbox:own` with its
captured devbox VMs and their A–B / B–C links). `null` in a staff document
served to the page (the page has the live model); a staff download embeds the
model it was viewed against.

**timeline** — `orchestrator/rounds/timeline.py`'s shape:

- `run` — the team's run (state, position, worm start, end, start VM,
  duration), `null` without one (a Dev round, a run not planned yet);
- `nodes` — the VMs shown (`group`: `sandbox` / `devbox`, addresses,
  networks, start VM, `captured_at`), `networks`, `edges` (VM → network),
  `vm_status` (power state over time);
- `events` — each `{id, t, kind, node, vm, data}`: `state`,
  `worm_deployed`, `worm_started`, `secret_released`, `vm_stopped`,
  `vm_started`, `captured`, `flag_submitted` (on scoring: `result`
  captured / duplicate / not_counted / invalid / pending, `flag_of`,
  `hash_prefix`, the sender `source_vm` inferred from the monitor),
  `log_received` (on logging: `stored`, the inferred sender, and the
  **reference** `log: {ref, size}`);
- `flows` — **optional** (see below): each connection `{t, d, src, dst,
  src_role, dst_role, src_name, dst_name, kind, src_port, dst_port,
  transport, service, conn_state, bytes, pkts, uid}`;
- `truncated` — whether a limit cut flows, submissions or logs.

### Log records: by reference

A worm can log a lot, so the document carries only each log's time and a
reference: `data.log = {"ref": "<uuid>", "size": <characters>}`. The record
itself — `{"ref", "received_at", "data"}`, `data` being the JSON the worm
sent — is

- fetched on demand from `logs.url` (with `{ref}` replaced): the player does
  this when a log event is opened;
- or, in a zip, the member `logs.path` (`logs/{ref}.json`) when
  `logs.included` is true — leave the records out for a lean zip.

### Network traffic: optional

`network.available` says whether traffic is recorded at all (not for Dev:
`reason` says why). The connections are

- inline in `timeline.flows` (a single JSON file), or
- in the zip's `network.path` (`network/flows.jsonl`, one JSON object per
  line) when `network.included` is true, or
- fetched from `network.url` (`{count, truncated, available, reason, flows}`):
  the player draws the picture at once and adds the dots when they arrive.

`network.count` always gives their number.

## The zip

`GET …/progression.zip?logs=0|1&network=0|1` (both default to 1):

```
progression.json        the document (flows not inline; logs/network say what is included)
README.txt              what is in the zip
logs/<ref>.json         one log record per file            (logs=1)
network/flows.jsonl     the connections, one per line      (network=1)
```

Name: `worms-<round>-<team>-<live|dev>.zip`. Open it on the PROGRESSION (or
TOPOLOGY) page with **Open file…**: it is read in the browser (nothing is
uploaded); the log records are read from the zip when opened.

## Endpoints

| | Team API (bearer token) | PROGRESSION page (signed in) | Staff (TOPOLOGY) |
|---|---|---|---|
| rounds | `/api/v1/rounds/` | `/progression/rounds/` | `/topology/runs/` |
| document | `/api/v1/rounds/<id>/progression/` | `/progression/rounds/<id>/` | `/rounds/<run>/progression/` |
| connections | `…/progression/flows/` | `…/flows/` | `…/flows/` |
| one log record | `…/progression/logs/<ref>/` | `…/logs/<ref>/` | `…/logs/<ref>/` |
| zip | `/api/v1/rounds/<id>/progression.zip` | `…/download/` | `…/download/` |

The page's endpoints serve the rounds of the environment switched on (Live:
the competition, Dev: the development competition); another round is a 404.
A log record is served only if it is the team's and part of the round's
progression (the run's window, or the Dev round's window). The documents link
to their own connections and log records (`network.url`, `logs.url`).

## Limits

The builder caps what it reads (`timeline.py`): 20 000 connections, 5 000
submissions, 5 000 logs; a log record is at most 4 KiB (the logging server).
A document is therefore a few MiB at most; the zip, deflated, much less. The
team API's limits apply (rate limits per address and token, a 3 s statement
timeout; `docs/team_api.md`).

## The page

Without JavaScript the PROGRESSION page lists the rounds (links), the selected
round's captured VMs and counts, and the downloads. With it, the player
replaces that part: the rounds as tabs, the scene, the timeline with the
round's start / end / now marks, playback, event details (a log record is
fetched when opened), **Download** (the round as a zip, with or without log
records and traffic; or what is shown as one JSON) and **Open file…**. A
round that is still running is reloaded every minute, keeping the view and
the selection; a player sitting at the end follows "now".

Switching Live / Dev reloads the page (its player is a script of its own).

## Security

- What a team sees is decided on the server (above), for every endpoint and
  the zip alike; the player only draws what it is given.
- Log records, flows and submitted hashes are attacker-controlled (the
  worm's output). The player renders every value as text; files and
  responses pass the same sanitizer (`cleanProgression` / `cleanModel` /
  `cleanTimeline`): known fields only, coerced types, capped sizes. A zip is
  parsed in the browser with caps on members and sizes (a member that
  inflates beyond what it claims is refused). A file's URLs are never
  fetched.
- Tests: `tests/test_progression.py` (the builder's visibility rules for
  each "never shown" item, Dev windows, the clock, log scoping, the zip, the
  team API, the page's endpoints, the staff endpoints) and
  `tests/test_topology_page.py` (both pages in Chromium, zips and hostile
  files).
