# Writing a worm: the contract

What a team uploads and how it runs. The platform side is `docs/rounds.md`;
this is the part a team needs.

## The upload

A **`.zip`** with **`main.sh` at its top level** (not inside a folder), at most
100 MiB. Where it goes depends on the view you upload it in, and the two are
completely separate:

- **Live view:** your team's **latest upload before a round starts** is the one
  that runs in that round.
- **Dev view:** it **resets your devbox** at once, which rebuilds all three VMs and
  starts your captures over, and then runs on VM A. It never enters a round
  ([Developing against a devbox](#developing-against-a-devbox)).

The round agent copies the zip to a freshly built VM, unpacks it into
`/home/debian/worm/`, and, after a short fuse, runs:

```
bash main.sh        # working directory /home/debian/worm, user debian (with sudo)
```

So:

- the entry point is **`main.sh`**; everything else in the zip sits next to it,
  and `main.sh` starts whatever it needs from there (`python3 worm.py`, a binary);
- `main.sh` needs no execute bit, because `bash` reads it. **The zip's file modes
  are not kept**, so run anything else through its interpreter or `chmod +x` it first;
- the Debian image has `bash`, `curl` and `python3` (the standard library). No pip
  packages are installed for you: vendor anything else into the zip;
- you run as **`debian`**, which has passwordless `sudo`, so you can become
  root on your VM. The VM is yours to do with as you like; the containment is
  the network and the firewall, not in-guest privilege (`docs/security-assessment.md`);
- there is no time limit on `main.sh` itself; the **round** ends after its
  configured length (staff set it; the dashboard shows the countdown).

## Capturing a VM

Every sandbox VM holds a **flag** at **`/etc/worms/flag`** (readable by
anyone on the VM). You capture a VM by **submitting its flag** to the scoring
server:

```sh
curl -X POST https://scoring.worms.internal/submit \
  -H "Content-Type: application/json" \
  -d "{\"vm_flag\":\"$(cat /etc/worms/flag)\"}"
```

- **No token or team id.** Only one team runs at a time; the scoring server
  knows which team that is from where the request comes. Just send the flag.
- **TLS is verified** — the sandbox trusts the internal CA, so a normal HTTPS
  request to `scoring.worms.internal` works (no `--insecure`). The name
  resolves over the sandbox's own DNS.
- A flag counts once per VM. Your **score in a round is the number of VMs you
  captured**; teams are ranked per round by that (`docs/rounds.md`).

Your own VM's flag is there from the start — submitting it captures your start
VM. The other VMs' flags appear over the round (some only after a VM is
switched off), and you reach those VMs by moving through the sandbox network
from where you are, exactly as the worm theme intends. A flag you submit that
is not yet released, or belongs to a VM you cannot yet see, simply does not
count; keep going.

> **Reaching other VMs is up to your worm.** The VMs run only SSH (the
> operator's key), so there is no door the platform opens for you; a round
> with seeded services/vulnerabilities to pivot through is not built yet
> (`TODO`, `docs/rounds.md`). Until then a worm reliably captures its own
> start VM.

## Logging (optional)

To record anything for yourself during the round — what you found, where you
got — POST it to the logging server; you read it back afterwards through the
team API (`docs/team_api.md`) or replay it on the **PROGRESSION** page
(`docs/progression.md`):

```python
req = urllib.request.Request(
    "https://logging.worms.internal/submit",
    data=json.dumps({"log_data": {"event": "captured", "vm": "..."}}).encode(),
    headers={"Content-Type": "application/json"}, method="POST",
)
urllib.request.urlopen(req, timeout=10)
```

`log_data` is any JSON object, at most 8 KiB, nesting depth 8, 256 items.

## Limits (both servers)

They serve the sandbox and assume every request is hostile, so keep it
modest: `POST /submit` only, a JSON **object** body at most 4 KiB, a flag a
printable string ≤ 64 characters. There is a per-team rate limit (about 30
requests a minute, small bursts allowed); well past it you get `429` and a
short cooldown. A malformed body is `400`, the wrong method `405`, an
oversized body `413`.

## What you can and cannot reach

From a sandbox VM: the **other sandbox VMs** (the experiment), **scoring**,
**logging**, the **private internet** (a DNS server and read-only mirrors —
e.g. `wikipedia.worms.internal`) and nothing else. Not the Proxmox host, the
database, the dashboard, the orchestrator, the monitor, the real internet, or
another team's environment. Everything you do on the sandbox network is
recorded by a passive monitor and replayed on your PROGRESSION page.

## Developing against a devbox

You do not have to wait for a round. Each team gets an always-on **devbox** (a
private copy of the sandbox); it submits to the same scoring/logging servers,
attributed to your team, without touching a running round.

**Upload in the dashboard's Dev view** to try a worm the way a round runs it:

1. All three VMs are rebuilt, so everything on them is lost, including anything
   you left there over SSH, and your captures start over.
2. Your zip is copied to **VM A** and unpacked, and `bash main.sh` starts after
   10 seconds, as described above.
3. It runs for up to an hour. Then `main.sh` is stopped, but anything it started on
   B or C keeps running until the next reset. The VMs stay as they are, so you can
   look around over SSH.

Only your latest upload runs: a new one replaces the one before, and a Reset ends
it. **It waits while a round is running**, because the platform can only do one such
job at a time and a round's timing comes first. The Upload page shows where it is,
and the Progression page shows what it did.

You can also work on the VMs by hand over SSH. Setup, keys and how to connect:
`docs/devbox.md` and the dashboard's Dev view.

## Reaching the LLM endpoint

Your VMs can reach one server outside this system: `teachllm.mlsec.tu-berlin.de`,
on TCP 443. Nothing else outside is reachable. The name does not resolve on the
VMs, and its certificate is for the name, so give both:

```sh
curl --resolve teachllm.mlsec.tu-berlin.de:443:141.23.117.34 https://teachllm.mlsec.tu-berlin.de/
```

## A minimal worm

`worm.sh` in the repository is a working skeleton: it reads its own VM's flag,
submits it, logs one line, and exits. Rename it to `main.sh`, zip it, and
upload it.
