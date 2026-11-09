# 8. User guide (for teams)

This page covers:
- what a team sees and uses: the dashboard, the APIs your VMs reach and the team API;
- the worm contract: what to upload and how it runs.

## The dashboard

`https://worms.mlsec.tu-berlin.de/` (development: `worms-dev.mlsec.tu-berlin.de`).

- **Sign-in.** Use the account and one-time password from the organisers; you set your own password at the first sign-in.
- **Two views.** A switch in the header toggles between them. New sessions start in **Dev**.

| View | For | Home page |
|---|---|---|
| **Dev** | your devbox: develop and try your worm | the devbox: your VMs A–B–C, how to connect, captured tokens, **Reset devbox** |
| **Live** | the competition: rounds | the leaderboard |

| Page | Shows |
|---|---|
| Leaderboard (`/`, public) | **All**: points per team over finished rounds (Formula 1: 25, 18, 15, 12, 10, 8, 6, 4, 2, 1 by VMs captured; ties share a place; 0 captures, 0 points), ⭐ per round won. One tab per round: VMs captured / sandbox VMs. A round counts only once every team's run has finished |
| Upload (`/upload/`) | Upload a zip; your uploads in this view (hash: download it again; Live: rounds it ran in; Dev: the run's status) |
| Progression (`/progression/`) | A replay of each round run (Live) or of your devbox since its last reset (Dev): your VMs, captures, submissions, logs and, in rounds, network traffic. Download as zip or JSON; replay a downloaded file offline |
| Profile (click your team's name) | Your account, password change, the team's score, **SSH keys** (add/remove), **access tokens** for the team API (after registration closes) |
| Teams (`/teams/`) | During registration only: join or leave a team |

The status banner (Live view) shows the next round's countdown, a running round's time left, and a warning if your team has no upload yet.

## The worm contract

### What to upload

- **Format.** A **`.zip` with `main.sh` at its top level** (not in a folder), at most 100 MiB.
- **Quota.** At most 30 uploads an hour per team, and 2 GiB stored per team (the same zip counts once).
- **Where it runs.** The view you upload in decides:
  - **Live.** Your team's **latest Live upload before a round's start** is the one that runs in that round.
  - **Dev.** The upload **resets your devbox** (all three VMs back to clean, your dev score back to zero) and runs on VM A. It never enters a round, and a newer dev upload or a reset replaces it.
- **Skeleton.** [`worm.sh`](../worm.sh) is a working starting point: it submits its VM's flag and logs one line. Rename it to `main.sh` and zip it.

### How it runs

```
bash main.sh        # cwd /home/debian/worm, user debian (passwordless sudo), started 15 s (devbox: 10 s) after deploy
```

- **Unpacking.** The zip is unpacked with Python's `zipfile`: absolute paths and `..` are stripped, links become files, and **file modes are lost**. Run other files through their interpreter, or `chmod +x` them first.
- **Environment.** The platform sets no environment variables: no team id, token or target list.
- **Output.** `main.sh`'s stdout/stderr go to the VM's journal (`journalctl -u worms-start`), which nobody collects. Use the logging API to keep anything.
- **Image.** Debian 13 with `bash`, `curl`, `python3`. Anything else must be vendored into the zip, or be in the package caches. The devbox also has `python3-pip` and `unzip`.
- **Privileges.** You are `debian` and can become root. The containment is the network, not the VM.
- **Rounds.** The run lasts the round's duration per team (the dashboard shows it) and is then hard-stopped. If the platform fails during your run, your worm runs again from scratch: up to 4 attempts, with only the last successful one counted.
- **Devbox.** `main.sh` is stopped after an hour. What it started on B or C runs until the next reset.

### Flags

- **Where.** Every VM has a **flag** at `/etc/worms/flag`: `FLAG{<32 hex>}`, readable by anyone on the VM.
- **How to score.** Submit it to scoring. A flag counts once per VM.
- **In a round:**
  - Your score is the number of VMs you captured.
  - Flags are new for every run.
  - The round's gameplan decides how VMs connect. Flags may be removed and reappear later, so that only a worm that has spread can take them.
  - Results appear only after your run has finished successfully.
- **On the devbox:**
  - The score is the number of your three tokens submitted since the last reset.
  - Results appear at once.

### Moving between VMs

- **In a round.** The sandbox is a set of networks. Each VM can reach only the VMs it shares a network with, and only on the ports the round's gameplan opens. What a VM holds is part of the game: for example an SSH key for the next VM, or a note with a password.
- **On your devbox.** The chain is A–B–C, and B is the only way from A to C. Your team's registered SSH keys log in as root on all three VMs.

## Scoring and logging

Both APIs work the same way from a sandbox or a devbox VM, over HTTPS with the internal CA. Your VMs trust that CA.

```sh
curl -sS -X POST https://scoring.worms.internal/submit -H 'Content-Type: application/json' \
     -d "{\"vm_flag\":\"$(cat /etc/worms/flag)\"}"
curl -sS -X POST https://logging.worms.internal/submit -H 'Content-Type: application/json' \
     -d '{"log_data": {"event": "spread", "to": "10.64.1.12"}}'
```

| | scoring | logging |
|---|---|---|
| Endpoint | `POST https://scoring.worms.internal/submit` | `POST https://logging.worms.internal/submit` |
| Body | `{"vm_flag": "<1–64 printable characters>"}`, at most 2 KiB | `{"log_data": {…}}`: a non-empty JSON object, at most 8 KiB compact, depth 8, 256 items; body at most 16 KiB |
| Success | `201 {"status":"success","message":"Submission stored."}` | the same |

- **Answers.**
  - Scoring always answers 201 for a well-formed flag. Whether it counted shows later on the dashboard.
  - To log raw output, base64-encode it: `{"log_data": {"b64": "…"}}`.
- **No credentials.** The service knows your team from where the request comes: your gateway's address.
- **Errors.** `400` (malformed), `403` (unknown source), `409` (no round running), `413` (too large), `415` (not JSON), `429` with `Retry-After` (rate), `503`.
- **Limits.**
  - About 30 requests a minute per team, with bursts of 10.
  - 100 rejected requests within a minute bring a 5-minute ban.
  - New connections to the services are limited to 20 a second per VM.

## What your VMs can reach

| Name | Port | What |
|---|---|---|
| your own VMs | as the gameplan allows (round) / all (devbox links) | |
| `scoring.worms.internal`, `logging.worms.internal` | 443 | above. `<anything>.scoring.worms.internal` works too |
| DNS 10.2.0.11 | 53 | only `*.worms.internal` and the LLM name resolve |
| `apt-cache.worms.internal` | 3142 | Debian packages: only what is cached. Other `apt install`s fail |
| `pypi.worms.internal` | 3141 | `http://pypi.worms.internal:3141/root/pypi/+simple/`: a small seed. Preconfigured on devboxes |
| `wikipedia.worms.internal` | 8888 | kiwix |
| `manpages.worms.internal` | 80 | man pages |
| `cve-search.worms.internal` | 5000 | CVE search |
| `docker-registry.worms.internal` | 5000 | read-only, empty |
| `ntp.worms.internal` | UDP 123 | |
| `teachllm.mlsec.tu-berlin.de` | 443 | the LLM endpoint: the only server outside |

Nothing else is reachable: not the internet, the dashboard, other teams, or the sandbox from a devbox.
- **Recorded traffic.** All traffic in a round is recorded and shown on your progression page. Devbox traffic is not recorded.
- **VM resources.** CPU type `x86-64-v3`: AVX2, but no hardware virtualisation, so no `/dev/kvm`. Devbox VMs have 1 core and 512 MB; sandbox VMs are sized by the gameplan.

## Devbox

1. **Add your SSH public key on your profile.** One line, a known type (ed25519, rsa, ecdsa, sk-…); the comment may use only `A-Za-z0-9._@/+:,-`. It is installed in about 3 minutes.
2. **Connect** with the commands on the Dev home page, using your key in an agent:

   ```sh
   ssh-add -L                                            # your key must be listed
   ssh -A -J team@worms.mlsec.tu-berlin.de root@<A>      # development: team@worms-dev.mlsec.tu-berlin.de:2222
   ssh -A root@<B>                                       # from A; then from B: ssh root@<C>
   ```

   The gateway lets you through to your own A only, with no shell. `-A` forwards your agent into your VMs: use a key that opens nothing else.
3. **Reset.** **Reset devbox** (Dev home or profile) returns A, B and C to a clean state. A dev upload resets them too. It takes a few minutes, and waits while a round's sandbox is being built.

## Team API

These are read-only JSON endpoints for your own team's results ([`dashboard/api.py`](../projects/dashboard/dashboard/api.py)).

```sh
curl -sf -H "Authorization: Bearer $TOKEN" https://worms.mlsec.tu-berlin.de/api/v1/rounds/
```

- **Authentication.** Create a token on your profile. It is shown once and stored only as a hash. A token in the URL is refused (`400 token_in_url`).
- **Requests.** GET only; responses are `Cache-Control: no-store`.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/rounds/` | `{team, rounds: [{id, name, start, end, run, scoring, logging, progression, download}]}`. A round's `id` is a UUID. `run` = `{state, position, attempt, worm_start_at, ends_at}` |
| `GET /api/v1/rounds/<round id>/scoring/?limit=&cursor=` | `{team, round, run, summary: {captured, vms}, items: [{id, submitted_at, flag_hash, result, vm}], page}`. `result`: `captured`, `duplicate`, `not_counted` (outside the run's window), `invalid`, `pending`. `vm` is named only if you captured it. Limit 1–500 (default 100) |
| `GET /api/v1/rounds/<round id>/logging/?limit=&cursor=` | `items: [{id, uuid, received_at, log}]`. Limit 1–200 (default 50) |
| `GET /api/v1/rounds/<round id>/progression/` | The replay document (below) |
| `GET /api/v1/rounds/<round id>/progression/flows/` | `{count, truncated, available, reason, flows}` |
| `GET /api/v1/rounds/<round id>/progression/logs/<ref>/` | `{ref, received_at, data}` |
| `GET /api/v1/rounds/<round id>/progression.zip?logs=0\|1&network=0\|1` | `progression.json`, `logs/<ref>.json`, `network/flows.jsonl` |
| `GET /api/v1/devbox/progression/`, `…/logs/<ref>/`, `/api/v1/devbox/progression.zip?logs=0\|1` | The same for your devbox since its last reset (no traffic) |

- **Before a round's run has finished.** `scoring` and `logging` return no items, and the progression is withheld (`timeline.run.withheld: true`).
- **Paging.** Follow `page.next`, or pass `page.next_cursor` as `?cursor=`, until it is `null`. Items are ordered by time, then id, and a page holds at most 1 MiB.
- **Errors.** `{"error": {"code", "message"}}` with 400 (`bad_limit`, `bad_cursor`, `bad_flag`, `token_in_url`), 401, 404, 405, 503 (`unavailable`: the 3 s query limit; retry with a smaller `limit`). `429` comes from nginx.
- **Rate limits.** 5 requests/s per address (burst 20) and 3/s per token (burst 10); 8 connections per address.

**Progression document** (`format: "worms-progression"`, `version: 1`):

```
{format, version, generated_at, audience: "team", environment: "live"|"dev", round, team,
 clock: {t0, round_start, round_end, now, finished, t_min, t_max},
 scene, timeline: {run, nodes, networks, edges, vm_status, events, captures, truncated},
 logs: {count, truncated, included, path, url}, network: {available, reason, count, truncated, included, path, url}}
```

- **What it shows.** Your entry VM (once the worm started), the VMs you captured, scoring, logging and the private internet. It never shows VMs you did not capture.
- **Events:** state, VM started and stopped, flag released, worm deployed and started, captures, submissions, logs.
- **Size cap:** 20 000 connections, 5 000 submissions, 5 000 logs. Logs are included by reference.
