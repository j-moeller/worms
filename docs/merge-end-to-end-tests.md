# Merge of `https/end-to-end-tests` into `main`, 2026-11-06

**Asked:** fetch `https/end-to-end-tests` up to its latest commit `69ee3b0` (2026-11-05), merge its
changes to the private internet stack if `main` does not have them, document the merge and what
is new, and re-provision what they affect.

**Result:** `main` already had every private-internet change of the branch, so no code was merged.
What the branch adds is information: a live check of the private internet from all three teams'
devboxes. It is kept unchanged in `docs/archive/private-internet-test-2026-11-05.md`, with the
branch's own merge notes in `docs/archive/reconciliation-2026-11-05.md`, and its findings are below
and in `docs/status.md`. The rest of the branch is outside the private internet and was **not**
merged (the end of this document).

## The branch

| | |
|---|---|
| Tip | `69ee3b0` "Record private-internet live checks from three teams", 2026-11-05 |
| Where it split from `main` | `211d6ac` (the branch itself merged `main` up to there, and `private-internet-hardening` up to `0eb8042`) |
| Commits not on `main` | 30, 27 of them by Fabian, 1 by Stefans |
| Commits on `main` since the split | 25, among them the snapshots, the CPU fix, the LLM endpoint and the docs |

## The private internet stack: already on `main`

Paths compared: `opentofu/stacks/private_internet`, `opentofu/modules/private_internet_vm`, the
firewall stack's groups, `opentofu/shared/shared-variables.tf`, `ansible/roles/private_internet/*`,
`ansible/SetupPrivateInternet.yml`, `helpers/offline-mirror`. Between the split and the branch's
tip, **none of these files changed**. The seven private-internet commits on the branch came in
before the split, and `main` holds their effect:

| Commit | What | On `main` |
|---|---|---|
| `64c8e41` | Hardening of every private-internet service (systemd sandboxing) | Yes: the same directives in all seven units, plus later fixes (devpi's memory, CGI support) |
| `c02ab6c` | One firewall group per service instead of one for all | Yes: `private_internet_service` groups from `private_internet_service_ports` |
| `2b49c72`, `573f778` | kiwix: writable-path note, netlink for `getifaddrs` | Yes, identical |
| `ad2fcc3` | netlink for devpi and cve-search | Yes, identical |
| `62c7fa2` | All private-internet services enabled again | Yes, identical |
| `0eb8042` | apt-cacher-ng's first boot: start the service only after its data directory is chowned | Yes, differently: `main` has one cloud-init template for all VMs and the `apt_cache` role creates the user and sets the owner before installing (`docs/archive/pi-failures.md`, finding 1) |

The branch's reconciliation notes say the same: "Main independently contains the effective
hardening changes".

## What is new: the private internet seen from three devboxes

Fabian's live check on 2026-11-05 ran from `devbox-t1-a`, `t2-a` and `t3-a`, with the internet
closed. It is the first check of these services from team VMs rather than from the service VMs
themselves.

| Service | Seen | New? | State now |
|---|---|---|---|
| DNS | All seven names resolve, from every team | | Works |
| apt | Indexes and a real install work through the cache | | Works. Earlier failures were the VMs running out of memory (below) |
| **Team VM memory** | apt processes killed by the OOM killer: the devbox VMs had a 256 MB balloon floor; with 512 MB the same install passed | **New** | Fixed since: devbox VMs have floor and ceiling 512 MB (checked live 2026-11-06) |
| pip | `tabulate` installs; `tqdm` fails (`502`): devpi holds one file | Confirms | devpi still holds only its seed (`docs/status.md`, D-6, D-7) |
| Docker registry | `/v2/` answers, catalog empty, the helper's `httpd:2.4.49` is not there | Confirms | Empty |
| **Man pages** | The front page answers, but its own browse link and search form get **`403`**: Debian's `man2html.conf` has `Require local`. The CGI socket failure was seen too | **New**: only the CGI `503` was known | Open. Needs `Require all granted` (or the services network) for the CGI path, besides the CGI fix (D-8) |
| cve-search | The UI and `/api/dbinfo` answer, with no data. Starting `mongosh` on the VM made the OOM killer kill `mongod`: the guest had about 455 MB of its 1024 MB | **New**: MongoDB needs guaranteed memory | The VM now has 4096 MB with a 2048 MB floor (the repository's value). Still no data |
| Wikipedia | Port 8888 refuses: no dataset, so `kiwix-serve` does not start | Confirms | Not running |
| NTP | Answers from every team, stratum 10, no upstream | Confirms | As before |
| Ports | From each team to each service address, only the five running services' ports accept | | Works |
| The outside | `1.1.1.1:443` and `130.149.7.7:53` time out | | Works |

Its follow-ups, and where they stand: memory for package operations (done for devbox VMs; the
sandbox's 512 MB floor matches); a real PyPI catalog (open, D-6); the man pages' access rule and
CGI (open); filling the registry, cve-search and Wikipedia inside the preparation window, without
opening the internet around student devboxes (open, D-7; a build now shuts the devboxes down for
its window); the same checks from a real sandbox (open: none has been built).

## Re-provisioning

Since nothing in the private internet stack changed, nothing there needed re-provisioning for the
merge itself. The private-internet VMs were re-provisioned anyway, with everything else, by the
`worms build` that deployed the current `main` on 2026-11-06 (`docs/status.md`).

## The rest of the branch, not merged

Outside the private internet, the branch carries Fabian's reconciliation and test work. None of
it is on `main`, and merging it now would conflict with `main`'s work since the split in at least
`dashboard/views/{devbox,uploads,staff_rounds}.py`, `orchestrator/api.py`, `topology.js`,
`docs/testing.md` and `tests/test_round_agent.py`:

| Area | Commits | What |
|---|---|---|
| Accounts and teams | `20b9295`, `dea0958`, `f96add2`, `849170b` | Enrollment and CSV-import races, upload recovery, concurrent key updates, serialized round planning and staff edits |
| Migrations | in `9308a2f` | `0002_case_insensitive_accounts_and_teams`, `0003_canonical_ssh_key_fingerprints`; they stop on collisions instead of merging records |
| Orchestrator | `7423263`, `711fe95`, `451c77e` | Daemons reconnect after a database outage; HTTPS through the service trust store; CA bundle overrides |
| Deployment | `aa16e69`, `111a3d9`, `9308a2f` | collectstatic's environment, TLS trust and host aliases in role `django_app` (a different way to what `main` does), a Zeek rotation fix (`main` has its own, G5) |
| Dashboard | `291e8e8`, `db9be86` | SSH key revocations across rebuilds, a lost click in the replay |
| Tests | `e879f8d`, `6707251`, `8c9c397`, `f6cc9e8` | Isolated dashboard rehearsals, a full Linux-stack test in a container, Chromium/Firefox/WebKit journeys; 807 tests passed in their container |

Worth deciding soon, since some of it fixes races real users can hit. Its own notes add that
uploads made for `python3 main.py` need replacing, because worms now start as `bash main.sh`.
