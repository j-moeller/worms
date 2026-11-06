# For teams: writing a worm

What a team uploads, how it runs, and what its VMs can reach. The platform side is
`docs/rounds.md` and `docs/devbox.md`.

## The upload

A **`.zip`** with **`main.sh` at its top level** (not in a folder), at most 100 MiB, on the
dashboard's **Upload** page. Your team may upload at most 30 times an hour and keep at most 2 GiB
of uploads (the same zip twice counts once). The view you upload in decides where it runs, and the two are
separate:

- **Live view:** your team's latest upload before a round starts is the one that runs in that
  round.
- **Dev view:** it **resets your devbox** at once (all three VMs back to a clean state, everything
  on them lost, your dev score back to zero) and then runs on VM A. It never enters a round.

The zip is unpacked into `/home/debian/worm/` and, after a short fuse, started:

```
bash main.sh        # working directory /home/debian/worm, user debian (passwordless sudo)
```

- Everything else in the zip sits next to `main.sh`, which starts it (`python3 worm.py`, a binary).
- **File modes are not kept**: run other files through their interpreter, or `chmod +x` them first.
- The image has `bash`, `curl` and `python3` with its standard library. Vendor anything else
  into the zip; what the package caches hold is limited (below).
- You are `debian` and can become root. The VM is yours; the containment is the network, not
  in-guest privilege.
- In a round, the run ends after its configured length (the dashboard shows the countdown). On the
  devbox, `main.sh` is stopped after an hour; what it started on B or C keeps running until the
  next reset.

`worm.sh` in the repository is a working skeleton: it submits its own VM's flag, logs one line
and exits. Rename it to `main.sh`, zip it, upload it.

## Capturing a VM

Every VM holds a **flag** at `/etc/worms/flag`, readable by anyone on it. Submit it to scoring:

```sh
curl -X POST https://scoring.worms.internal/submit \
  -H "Content-Type: application/json" \
  -d "{\"vm_flag\":\"$(cat /etc/worms/flag)\"}"
```

- No token or team id: scoring knows your team from where the request comes. TLS is verified
  against the internal CA, which your VMs trust.
- A flag counts once per VM. **In a round, your score is the number of VMs you captured.** On the
  devbox it is the number of your three tokens submitted since the last reset.
- In a round, your start VM's flag exists from the start. Others appear over the round, some only
  after a VM is switched off, so only a worm that has spread can get them (`docs/rounds.md`, game
  plan).

> **How a worm gets from one VM to the next is not settled yet.** Sandbox VMs run only SSH with
> the operator's key, so today a worm in a round can capture only its start VM; seeded
> vulnerabilities are planned. On your devbox, B and C accept your team's registered SSH keys as
> root, so a worm that carries a private key you registered can log in to them.

## Logging

To record anything for yourself, POST it to logging. You read it back on the **Progression** page
or through the team API (`docs/team_api.md`):

```python
req = urllib.request.Request(
    "https://logging.worms.internal/submit",
    data=json.dumps({"log_data": {"event": "captured", "vm": "..."}}).encode(),
    headers={"Content-Type": "application/json"}, method="POST",
)
urllib.request.urlopen(req, timeout=10)
```

## Limits

Both servers accept `POST /submit` only, a JSON object of at most 4 KiB, a flag of at most 64
printable characters, and `log_data` of at most 8 KiB, depth 8, 256 items. About 30 requests a
minute per team with small bursts; beyond that `429` and a short cooldown. A malformed body is
`400`, an oversized one `413`.

## What your VMs can reach

| Reachable | |
|---|---|
| Your other VMs | In a round: the sandbox networks. On the devbox: A–B and B–C |
| `scoring.worms.internal`, `logging.worms.internal` | Your team's own names also work: `<team>.scoring.worms.internal` |
| DNS | Only `*.worms.internal` and the LLM endpoint's name resolve |
| `apt-cache.worms.internal` | Debian packages: only those already cached. `apt install` of anything else fails |
| `pypi.worms.internal` | Python packages: a small seed only |
| `wikipedia.worms.internal`, `manpages.worms.internal`, `cve-search.worms.internal`, `docker-registry.worms.internal`, `ntp.worms.internal` | Read-only mirrors. Some are still empty or broken (`docs/status.md`) |
| `https://teachllm.mlsec.tu-berlin.de/` | The LLM endpoint, TCP 443: the one server outside this system |

Nothing else: not the internet, the dashboard, the database, the Proxmox node, another team's VMs,
or the sandbox from a devbox. Everything a round's VMs send is recorded by a passive monitor and
shown on your Progression page; devbox traffic is not recorded.

Sandbox and devbox VMs have the CPU `x86-64-v3` (AVX2 and AES, no hardware virtualisation, so no
`/dev/kvm`) and 512 MB of RAM by default.

## Your devbox

Each team has three always-on VMs to develop against; they submit to the same scoring and logging,
attributed to your team, without touching a round. Add an SSH key on your **profile**, then
connect with the commands in the **Dev** view's **Connect** panel:

```sh
ssh-add -L                                  # your key must be in your SSH agent
ssh -A -J team@<public name> root@<A>  # then on to B and C from there
```

**Reset devbox** on your profile, or a dev upload, returns all three VMs to a clean state. An
upload waits while a round is running, because the platform runs one such job at a time and a
round's timing comes first. The Upload page shows where it is; the Progression page shows what it
did.
