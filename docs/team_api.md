# Team API: a team's scoring, logs and round progression (JSON, zip)

Teams download their own results with an access token. Read-only; served by
the dashboard (`dashboard/api.py`) under `/api/v1/`.

## Authentication

Create an access token on your **profile** (click your team's name; shown
once, stored only as its SHA-256, revocable there) and send it in a header:

```
Authorization: Bearer <token>
```

- The token selects the team: only that team's data is ever returned. Rounds
  are those of the team's competition (and its development competition, if
  any); any other round is a 404.
- A missing, malformed, unknown or revoked token is a `401` with
  `WWW-Authenticate: Bearer`. A token in the URL (`?token=` /
  `?access_token=`) is refused (`400`): URLs end up in logs and histories.
- GET only (`405` otherwise); responses are `Cache-Control: no-store`.

## Endpoints

| Path | Returns |
|---|---|
| `GET /api/v1/rounds/` | the team's rounds (id, name, start/end, `main`/`development`), the team's run in each (state, worm start, end) and links to the endpoints below (`scoring`, `logging`, `progression`, `download`) |
| `GET /api/v1/rounds/<id>/scoring/` | every flag the team's sandbox submitted during its run in that round, oldest first: `submitted_at`, `flag_hash` (as received), `result` (`captured` = scored, `duplicate` = that VM was already captured, `not_counted` = a VM's flag outside the run's live window, `invalid` = no VM of the run has it, `pending` = not processed yet), `vm` (whose flag it is — named only if the team captured that VM in the round; a VM it did not capture is never named). Plus `summary` (`captured`, `vms`). Devbox submissions are not part of a round and never appear here. |
| `GET /api/v1/rounds/<id>/logging/` | every log attributed to the team in that round, oldest first: `uuid`, `received_at`, `log` (the JSON the worm sent, as stored) |
| `GET /api/v1/rounds/<id>/progression/` | the round's **progression** (`docs/progression.md`): the clock (round start, end, and now while the round runs), the scene, every event — captures, submissions, logs (by reference), the run's state — of the VMs the team captured. Works for unfinished rounds and for Dev rounds (the devbox). |
| `GET /api/v1/rounds/<id>/progression/flows/` | its network traffic (`{count, truncated, available, reason, flows}`; not recorded for Dev rounds) |
| `GET /api/v1/rounds/<id>/progression/logs/<ref>/` | one log record of it: `{ref, received_at, data}` (404 for anything not the team's in that round) |
| `GET /api/v1/rounds/<id>/progression.zip?logs=0\|1&network=0\|1` | all of it as one zip: `progression.json`, `logs/<ref>.json`, `network/flows.jsonl` (both parts default to included; `0` leaves them out). Open it on the PROGRESSION page to replay it. |

Every response carries `team`, `round` and `run`; the paged ones `items` and
`page`:

```json
{
  "team": {"uuid": "…", "name": "team-a"},
  "round": {"id": 3, "name": "R1", "competition": "main", "start": "…", "end": "…"},
  "run": {"state": "finished", "position": 0, "worm_start_at": "…", "ends_at": "…"},
  "items": [ … ],
  "page": {"limit": 200, "count": 200, "next_cursor": "bDo0MTI=", "next": "/api/v1/rounds/3/logging/?limit=200&cursor=bDo0MTI="}
}
```

The progression endpoints are not paged: a progression is bounded by its
builder (20 000 connections, 5 000 submissions, 5 000 logs; log records only
by reference).

```bash
curl -sf -H "Authorization: Bearer $TOKEN" -o round.zip \
     "https://<dashboard>/api/v1/rounds/3/progression.zip?network=0"   # without the traffic
```

Errors: `{"error": {"code": "…", "message": "…"}}` with `400` (`bad_limit`,
`bad_cursor`, `bad_flag`, `token_in_url`), `401`, `404`, `405`, `429` (rate limit, from
nginx), `503` (`unavailable`: the request ran into the time limit — retry, or
use a smaller `limit`).

### Paging

`?limit=` sets the page size (scoring: default 100, max 500; logging: default
50, max 200). Follow `page.next` (or pass `page.next_cursor` as `?cursor=`)
until it is `null`. The cursor is opaque; it is specific to its endpoint.
Paging is stable: items are ordered (by time, then id) and the cursor resumes
after the last item returned, so nothing is skipped or repeated even if new
items arrive while you page.

```bash
TOKEN=…            # an access token of your team
URL="https://<dashboard>/api/v1/rounds/3/logging/?limit=200"
while [ -n "$URL" ] && [ "$URL" != "null" ]; do
  PAGE=$(curl -sf -H "Authorization: Bearer $TOKEN" "$URL") || break
  echo "$PAGE" | jq -c '.items[]'
  NEXT=$(echo "$PAGE" | jq -r '.page.next')
  [ "$NEXT" = "null" ] && break
  URL="https://<dashboard>$NEXT"
  sleep 0.3          # stay under the rate limit
done
```

## Limits (availability)

A team, or anyone with a token, can ask for a lot of data; none of it may
degrade the dashboard for others:

| Where | Limit |
|---|---|
| nginx (`ansible/roles/web/nginx_tls_django`, `nginx_team_api_*`) | `/api/` only on the dashboard: 5 requests/s per client address (burst 20) **and** 3/s per token (burst 10), 8 concurrent connections per address, GET only, 1 KiB request body, 15 s upstream timeout; excess → `429` before Django. The host publishes the dashboard by DNAT without SNAT, so the client address is real. |
| page size | at most `limit` items **and** at most 1 MiB of item data per page (`MAX_PAGE_BYTES`; at least one item, so paging always progresses). Log records are already capped at 4 KiB by the logging server. A progression is bounded by its builder (above): a few MiB at most, its zip much less. |
| query cost | keyset pagination over dedicated indexes (`flag_attempts_team_time`, `logging_team_round`, migration 0013): each page is one bounded index range scan, however deep you page — no `OFFSET`, no counting of everything. |
| query time | every query runs under `statement_timeout = 3 s` (`SET LOCAL`, this request only) → `503` instead of a stuck worker. |
| writes | none: the API never creates a session (the session middlewares skip `/api/`; otherwise every anonymous call would insert a session row), no CSRF state, the dashboard role only reads here. |
| rounds list | at most 500 rounds. |

## Security notes

- Tokens: 256-bit random (`secrets.token_urlsafe(32)`), stored hashed, looked
  up by hash (unique index), revocable on the profile. A token grants read
  access to its own team's round results only — nothing of other teams, no
  infrastructure data, nothing writable. A progression shows only the VMs the
  team captured, scoring, logging and the private internet
  (`docs/progression.md`).
- The returned log records and submitted hashes are the team's own worm's
  output (attacker-controlled by design); they are returned as JSON data, with
  `Content-Type: application/json` and `X-Content-Type-Options: nosniff`.
- Tested in `tests/test_team_api.py` through the dashboard's real URLs and
  middleware against PostgreSQL (authentication, scoping, results, paging,
  limits, the timeout, no session rows), the progression endpoints in
  `tests/test_progression.py`; the nginx limits in `tests/test_ansible.py`.
