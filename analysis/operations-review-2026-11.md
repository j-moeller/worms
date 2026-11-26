# WORMS operations review: state, brittleness, diagnosability

Static analysis of commit `b826710` (2026-11-26). Nothing was run against a live system. Findings marked
**(verify)** depend on runtime behaviour (systemd, the bpg/proxmox provider, Proxmox) and should be confirmed
once on the development system before acting on them. Documentation mismatches are out of scope except where
they mislead an operator during an incident.

Scope: day-to-day operation of the existing production and development systems: `worms status`,
`worms apply <component>`, `worms round start <id> [--resume]`, `worms round teardown`, `worms login`,
`worms check`; moving a system to a new git checkout; finding the cause of a failure quickly.

---

## 1. Why the system feels complex and brittle (root causes)

The individual pieces are carefully written and heavily commented. The brittleness comes from the
*shape* of the system, not from sloppy code. Seven structural causes:

**R1. State has no home.** There are roughly 40 distinct host state paths, plus state inside the
checkout, on every VM, in Proxmox and in the database (inventory in §3). None of the code records which
location owns which fact. A fact like "which commit runs" or "which sandbox exists" lives in 4–5 places at
once.

**R2. Three copies of the code run at the same time.** These are:
- the operator's checkout (CLI, controller stacks, Ansible);
- `/opt/worms` (the round agent's workspace: sandbox and devbox stacks, playbooks, gameplans);
- the VMs (Django apps, scoring/logging), plus the code frozen in each service VM's `clean` snapshot.

A round session imports Python from **two** of those trees in one process. Handlers come from
`<checkout>/scripts` (`rounds.py:1377`); every command they run uses `/opt/worms` (`rounds.py:1375`). It
also shells out to a third: a nested `python3 src/manage.py step …` in the workspace (`executor.py:290`).

**R3. Execution modes multiply the paths.** The same commands run:
- as host or laptop (`WORMS_SSH`);
- as controller or workspace, decided by whether a vault file exists (`session.py:187-197`);
- as a CLI-run round or an agent-run round (the old agent path is still registered:
  `handlers.py:1889`).

Every mode doubles the places an error can come from. Every mode also doubles what you must hold in your
head.

**R4. Fixes have been additive.** The recent T1–T13 work added more *records* and more *comparisons* of
those records:
- `deployed.json`, `.worms-commit` and `/run/worms-django/running-<app>`;
- a commit in each snapshot description and a serial in each published output;
- the `round-stopped.json` record.

`worms status` now reads 16 probe sections in one host round-trip (`manage.py:182-199`) plus SSH to several
VMs. It is a good diagnostic, but it treats the symptom (copies drift) rather than the cause (copies exist).
The code holds ~145 broad `except Exception` sites and ~70 dated incident comments ("on 2026-11-23 …"),
which also shows a pattern of local patches.

**R5. Implicit joins.** Components are tied together by naming conventions and duplicated constants instead
of declared interfaces:
- tofu module key = inventory group = component `group` = VM name with `_`→`-`;
- hard-coded `-target` addresses (`components.py:44-77`);
- `devbox-ssh-gateway`, `sandbox-gw` and the pool names spelled out in 5–10 files each;
- address ranges repeated in `generate_devbox.py`, `src/gameplan/definition.py`,
  `dashboard/views/debug.py` and `round_agent/startup.py`, despite `shared-variables.tf` claiming to be the
  single source.

**R6. Evidence is captured but scattered and truncated.** One incident can span:
- `~/.worms/runs` (per user, per machine: a laptop's runs are invisible on the host);
- `~/.worms/rounds/…/summary.log`;
- `/var/log/worms/jobs`;
- per-VM journals (wiped by the next snapshot rollback);
- `RoundRun.failure` (deliberately only *where*, not *what*, for quiet handlers: `handlers.py:203-225`);
- `host_jobs.result`.

Error strings are cut short in two ways:
- `BuildError` truncates the command label to 97 characters (`build.py:65-74`), which usually loses the
  playbook name and `--limit`;
- tofu errors keep only the last stderr line (`changes.py:473`).

**R7. Silent-success paths.** Several operations report success while having done nothing, or less than
claimed:
- **Ansible "no hosts matched":** discarded (`callback_plugins/worms.py:319-320`), after which `deployed.json`
  records the new commit anyway (`manage.py:1248`).
- **Role stamps:** a role whose *included* role changed is still skipped (`worms_role_gate.py:84-93`).
- **Scoring readiness:** the check sends a GET and accepts any status < 500 (`probe.py:177-187`); nginx
  answers a GET with 403 before the app is involved.
- **Gameplan checks:** environment checks of the gameplan itself are warnings, not gates
  (`handlers.py:963`).
- **Old gameplans result:** a failed gameplans check falls back to the last good job's result
  (`gameplans.py:84`).

These are the places where an operator spends hours, because the error surfaces far from its cause.

### What is already good and should be kept

- The host lock (`lock.py`).
- `summary.log` per round session.
- The `round-stopped.json` record and `worms status`'s "what is still stopped, and the command that starts
  it".
- The VM history ledger.
- `worms upgrade`'s resumable steps.
- Per-VM Ansible log files.
- Refusing a dirty checkout for rounds.
- `worms check`'s end-to-end devbox probes.

The tasks below build on these rather than replace them.

---

## 2. Component and dependency map (operationally relevant view)

```
checkout (controller)                       host /opt/worms (workspace)            VMs
─────────────────────                       ──────────────────────────             ───
platform.tfstate ──remote_state(rel. path)──► infra/persistent, ephemeral,
firewall.tfstate                              private_internet (controller)
      │ publish_outputs()
      ▼
/var/lib/worms/outputs/{platform,firewall}.json ──file()──► sandbox, devbox stacks
                                                             generated_vms.tf / generated_devboxes.tf
tofu output ─► generate_inventory.py ─► inventory.generated.yml ─► Site.yml ─► every service VM
vault.yml ──► TF_VAR_* , -e @vault ──► copied to /etc/worms/round-agent.env, events.env, VM env files
pki/ca.* ───► signs every TLS cert (on localhost) ──► copy in /opt/worms, VM trust stores
SetupRoundAgent --tags agent-sync: rsync checkout ─► /opt/worms  (+ .worms-commit)

worms round start (host, root, one process)
  ├─ Django ORM (checkout's models) ──► database VM  (orchestrator VM services STOPPED)
  ├─ round_agent handlers (checkout's scripts/) ─► tofu/ansible/qm in /opt/worms
  ├─ ssh ─► dashboard VM (fetch worm zip), scoring+logging (current-team file)
  └─ pvesh as root (VM power during gameplan)
```

Hard ordering, enforced only by convention:
1. platform must come before firewall, which must come before everything else.
2. `publish_outputs` must succeed before any sandbox or devbox apply.
3. The inventory must be regenerated after every VM stack apply, before Site.yml.
4. DNS must be up before every other VM.
5. Orchestrator migrations run before database grants.
6. The workspace must be synced before a round's code runs.

Hidden "exists" checks disagree with each other:
- the Proxmox VM name (`manage.py:798-805`);
- the local tfstate (`components.stack_built`);
- the inventory;
- the VM history.

---

## 3. State inventory

| Location | Contents | Owner (writer) | Readers | Survives a new checkout? | Backed up by `worms upgrade`? |
|---|---|---|---|---|---|
| checkout `opentofu/stacks/{platform,firewall,infrastructure/*,private_internet}/terraform.tfstate` | controller stack state (local backend, no `backend` block) | CLI tofu | CLI, `stack_built`, `vms_deployed`, inventory generator, `remote_state` (relative paths) | **No** (gitignored) | yes |
| checkout `ansible/group_vars/vault.yml`, `ansible/.vault_pass` | every secret | `worms secret` | CLI, Ansible, tofu env | **No** | yes |
| checkout `ansible/pki/worms/ca.{key,crt}` | internal CA | `ca_create` (creates a **new** CA silently if missing) | every TLS role, CLI DB TLS | **No** | yes |
| checkout `opentofu/modules/os_images/images/*.qcow2` | OS images | manual | platform stack | **No** | **no** |
| checkout `ansible/inventory.generated.yml`, `.facts/`, `.terraform/` | derived | generator, Ansible, tofu | Ansible | regenerable | n/a |
| `~/.worms/config` | `WORMS_SSH`, repo root | operator | CLI | per *user*, not per checkout | no |
| `~/.worms/runs`, `~/.worms/rounds`, `~/.worms/backups` | logs, dumps | CLI | operator | per user/machine | no |
| `/opt/worms` | workspace code, sandbox and devbox tfstate, `generated_*.tf`, `.worms-commit`, own inventory | agent-sync rsync, agent, round session | agent, round session, `worms status` | yes (host) | tfstates only |
| `/var/lib/worms/` | `outputs/*.json`, `deployed.json`, `vm-history.jsonl`, `devbox-teams`, `held-devboxes.json`, `window-stopped-devboxes.json`, `sandbox-build.json`, `sandbox-reset.json`, `runs/<uuid>.json`, `qemu-processes.json`, `round-stopped.json`, `upgrade.json`, `agent-paused`, `tofu-providers/` | CLI, agent, round session | same | yes | no |
| `/etc/worms/` | `round-agent.env`, `events.env` (secret **copies**), `ssh/worms` (key copy), `worms.nft` | Ansible | agent, host firewall | yes | no |
| `/run/worms/` | `worms.lock`, `internet-allowed` (tmpfs: **lost on reboot**) | CLI, Internet.yml | everyone | no (by design) | no |
| Proxmox `/etc/pve` | VM configs, `clean` snapshots (with commit in description), pools, `.fw`, provisioning group rules (internet window) | tofu, CLI, Internet.yml | everyone | yes | no |
| each service VM | `/var/lib/worms/stamps/*`, `/etc/sandbox-api/{current-team,devbox-teams}`, `/run/worms-django/running-<app>` | Ansible, round session, CLI | Ansible, scoring, status | rolled back with the snapshot | no |
| database | rounds, runs, host jobs, captures, `Round.session_seen_at`, GameplanFile (**two writers**: CLI and orchestrator) | dashboard, orchestrator, CLI, round session | all | yes | `db backup` |
| process memory (round session) | `Conductor.fresh/checked/accepted/resume_build` | session | session | lost on restart | n/a |

**Facts with several sources of truth:**

| Fact | Places | Consequence when they disagree |
|---|---|---|
| Which commit runs | `deployed.json`, VM history, snapshot description, `running-<app>`, `.worms-commit` | `worms status` version block needs 5 inputs to answer one question |
| Which sandbox exists, and for which round | `generated_vms.tf`, sandbox tfstate, Proxmox pool, `clean` snapshots, `sandbox-build.json` | **none records the round**: a second round silently destroys or reuses the first's sandbox (`handlers.py:449-451`) |
| Is the internet window open | `/run` flag (tmpfs), nft set, PVE provisioning rules (persistent) | after a reboot during the window the flag is gone but the PVE rules stay open, and the guards believe it is closed |
| Does a VM or component exist | Proxmox name, tfstate, inventory, history | plan says "reconfigure", Ansible matches no hosts, apply "succeeds" |
| Secrets | vault, `round-agent.env`, `events.env`, VM env files, PostgreSQL, PVE | rotating a token kills the old one immediately; the copies stay stale until the next sync or apply (`tokens.py:133-135`) |
| Network constants | `shared-variables.tf` + 4 Python/Jinja files | a change in one place silently breaks addressing elsewhere |

---

## 4. Breaking points by daily scenario

### 4.1 Moving to a new git checkout
1. **Fresh clone without the vault.** `secrets` generates a brand-new vault *and a new WORMS SSH key*,
   because `vms_deployed()` looks only at the local tfstate (`build.py:209`, `secrets.py:96-110`). Every
   later step then fails with authentication errors far from the cause.
2. **Fresh clone with the vault but without tfstate.**
   - `stack_built` returns false, so `worms apply X` plans platform and firewall as "create (dependency)"
     against existing bridges and pools.
   - VM components are "reconfigure" (they exist in Proxmox), but the inventory has no hosts
     (`generate_inventory.py:420-424`: a stderr note only).
   - Site.yml then matches no hosts, the callback hides it, and `deployed.json` records the new commit: a
     **fake success**.
3. **Fresh clone without `pki/`.** `ca_create` silently creates a new CA. The next TLS role re-signs
   certificates that no VM trusts.
4. **Partial inventory wipes DNS.** DNS records exist only for groups with hosts (`db.worms.internal.j2:12`).
   Any apply with a stack missing from the inventory re-runs `setup_dns` (its stamp hashes the inventory)
   and **deletes the scoring, logging and cache records**.
5. **Wrong system.** Nothing binds a checkout's state to a host. `WORMS_SSH` is per user
   (`~/.worms/config`). A checkout holding dev's state pointed at prod passes `host_name()` (it only checks
   the name is *one of* the known systems: `session.py:289-308`). Dev and prod use identical IPs.
6. **Stale checkout overwrites the host.** Running `SetupRoundAgent.yml` from an old checkout rsyncs
   (`--delete`) its code over `/opt/worms` (`round_agent/defaults/main.yml:7`).

### 4.2 `worms apply <component>`
- **No tofu changes for existing VMs.** `worms apply <vm>` runs tofu only for create, recreate or dependency
  (`pipeline.py:723-730`). Changes to an existing VM's `.tf` (RAM, disk, firewall rules) need `worms apply`
  alone, or `--all`. Nothing says so in the apply output.
- **(verify) Rollback undoes tofu changes.** `_apply_changed` applies the stack plan, then rolls service VMs
  back to `clean` (`manage.py:1158-1167` → `pipeline.py:712-717`). Proxmox snapshots include the VM config,
  so in-place tofu changes to service VMs are silently reverted. Every round build reverts them again.
- **Failure cleanup erases evidence.** After a failed step the service VMs are rolled back first
  (`pipeline.py:254`). The failed state is then gone, and an offline Site.yml run buries the original error
  in the terminal.
- **The round agent is not coordinated with `apply`.**
  - The agent does not take the host lock (`manage.py:2825-2832` skips it in `/opt/worms`).
  - `apply` neither pauses the agent nor waits for it before shutting devboxes down and rolling back
    services.
  - `agent-sync` rewrites `/opt/worms` under a running agent job.
- **Stamps skip included roles.** A change to an *included* role (`grow_disks` from `devpi` and `apt_cache`)
  does not invalidate the stamp, so the role is skipped. Only `--full` catches this.
- **`tofu-init` hides its errors.** The first init's output goes to `/dev/null` (`scripts/tofu-init:30`).
  Every failure is reported as "provider not in mirror", then retried against the registry, which fails
  offline with an unrelated error.
- **(verify) `-target` after a rename.** After a `moved` rename, the hard-coded `-target` names an address
  that no longer exists. Tofu likely plans nothing and exits 0.

### 4.3 `worms round start` / `--resume` / `teardown`
- **Gates checked only once.** The preconditions (orchestrator, agent and devboxes stopped; window closed)
  are checked once (`rounds.py:773`) and never re-checked during the round. After a host reboot the agent
  and orchestrator come back. Two capture writers then race, and an IntegrityError rolls back whole batches,
  which is only logged.
- **No version gate.**
  - Pending migrations only *print* a warning (`django_setup.py:37`), and a mid-round ORM error then kills
    the session.
  - The orchestrator, dashboard and scoring VMs' running commits are not compared.
  - Neither is the service VMs' snapshot commit.
- **Sandbox not tied to a round.**
  - Round B with another gameplan silently destroys round A's kept sandbox.
  - With the same gameplan, B reuses A's sandbox.
  - A change to the HCL generator between sessions makes the next `--resume` **destroy and rebuild the
    sandbox mid-round**: later teams get differently built VMs, and the terminal just says "built and
    configured".
- **A missing snapshot means a silent rebuild.** On `--resume` the sandbox is destroyed and rebuilt (about
  an hour) without asking (`handlers.py:488-506`).
- **No database reconnect.** There is no `close_old_connections` in the round path. One database blip
  mid-attempt fails the attempt, then `fail()`, then `abandon` (swallowed), and the session dies with the run
  left ACTIVE.
- **Stale VM status.**
  - `snapshots.take` starts VMs without waiting, and `prepare` reads pvestatd status (≈10 s stale), which
    can fail the first attempt with a misleading "rollback comes first".
  - `end()` swallows `PowerError` with no log (`conductor.py:948`).
- **Teardown blocked by scoring.** `teardown` clears the current team on scoring and logging *first*
  (`handlers.py:1431`). If scoring is unreachable, the sandbox VMs are never stopped, and the next preflight
  fails on "running sandbox VMs".
- **Kept sandbox boots.** It has no `on_boot` setting (`opentofu/stacks/sandbox/main.tf`); after a host
  reboot it may boot with the last team's disk state.
- **Terminal-bound session.** A dropped SSH session ends the round (warned if not in tmux).

### 4.4 "Why did this team score unexpectedly badly?"

**Ways a team can lose points without it being the worm's fault:**

1. **The scoring gate is blind.** The GET probe passes even when gunicorn is down (502), the database role is
   broken (503) or `SANDBOX_GW_IP` is wrong (403) (`probe.py:177-187`). The team scores 0 and the attempt
   ends FINISHED.
2. **(verify) Backgrounded worm processes die with `main.sh`.**
   - The worm runs as a transient `systemd-run` service with no `KillMode` and no `RemainAfterExit`
     (`handlers.py:1322-1335`; no properties are passed for rounds, `:1274`).
   - With systemd's defaults, everything `main.sh` backgrounds is killed when it exits. That includes
     `capture-vm` (`setsid -f nohup` leaves the session, not the cgroup).
   - The user guide promises otherwise.
3. **Nothing records rejections.** 400/403/409/429/503 responses are never stored. nginx has
   `access_log off` and `error_log … crit`. App rejection messages are rate-limited to 30/min in the scoring
   VM's journal.
4. **Rate-limit state carries over.** The limiter and ban state is in memory, keyed by team, and survives
   into that team's retry within 300 s.
5. **Clocks are mixed.** The window compares host-clock `worm_start_at` with database-clock `created_at`,
   with a 120 s grace (`captures.py:64-71`). Nobody measures the skew.
6. **Environment checks are warnings only.** sshd, the gameplan's users and services, its firewall and files
   are not gates (`handlers.py:963`). An attempt runs, and is scored, on a broken sandbox.
7. **The schedule drifts with host load.** Waits are relative and steps are serial. The churn loop's reboot
   count depends on timing (`round_one.py:352`). VMs are stopped one by one at the end.
8. **Service VMs are reset only at build, not per team.** apt-cacher-ng, DNS caches and scoring's limiter
   carry state from team N to team N+1.
9. **`worms test --live` has no guard against a running round.** It overwrites `current-team` and deletes
   that uuid's submissions (`Simulate.yml:232-256`, `cleanup.sql`).

**What an operator can see:** no single place shows these together.
- The worm's exit code and output are lost at the next rollback.
- A counted-or-not decision stores no reason.
- Check warnings are buried in `host_jobs.result`.

### 4.5 "Ansible failed somewhere"
- **Where the output is.** The per-VM log files are good (`<run>/<vm>.log`). But host-workspace plays reached
  over SSH do not inherit `WORMS_ANSIBLE_LOGS`, so those land interleaved in the run log.
- **The summary is truncated.** "build stopped: command failed (exit 2): (ansible) ansible-playbook -e
  worms_commit=… -e @group_vars/vault.yml -e internet_…" usually cuts off the playbook name and `--limit`.
- **Errors are hidden.**
  - Ignored and rescued failures go only to the per-VM file.
  - `grow_disks` treats rc 1 (NOCHANGE *or* a real error) as success.
  - The devbox handlers hide sshd restart failures.
- **Some tasks always report "changed".** This masks real changes: "Restore internet gate", the
  `pveum role modify` and ACL tasks, "Open the host gate".

---

## 5. Prioritized task list

Format: each task is **S**pecific (what exactly changes), **M**easurable (a "done when" that can be checked)
and **R**elevant (which daily scenario it fixes). Subtasks are ordered. File references point to where work
starts.

Priority logic: **P0** prevents state corruption and silent wrong outcomes. **P1** makes any failure
locatable in minutes. **P2** removes concepts from the mental model. **P3** covers fairness and hygiene.
Within a tier, top to bottom.

### Quick wins (≤ ½ day each, do first, independent of everything below)

| # | Change | Done when |
|---|---|---|
| Q1 | `callback_plugins/worms.py:319`: print `no hosts matched` for every play; `pipeline._configured`/`_record_deployed` skip recording when the play matched no hosts | a Site.yml run with an empty inventory prints the warning and `deployed.json` is unchanged |
| Q2 | `scripts/tofu-init:30`: keep the first init's stderr and print it when falling back | a syntax error in `generated_vms.tf` shows OpenTofu's error box, not "provider not in mirror" |
| Q3 | `build.py:65-74`: never truncate the playbook name and `--limit`; put the command first, the `-e` args last | the "build stopped" line always names the playbook and the limit |
| Q4 | `changes.py:473`, `build.py:257`: extract the whole `Error:` block from tofu stderr (the round agent's `failure.py` already does this; reuse it) | a failed plan shows the full error box in the apply output |
| Q5 | `conductor.py:948`: log the `PowerError` (VM name, error) instead of `pass` | a failed stop at an attempt's end appears in `round.log` and `summary.log` |
| Q6 | `changes.record_deployed`: refuse to overwrite on a parse error (rename the bad file to `deployed.json.corrupt-<ts>` and warn) and check the write's return code | corrupt JSON is preserved and reported, never silently replaced by `{}` |
| Q7 | `pipeline.simulate` (`worms test --live`): refuse while `round-stopped.json` names a live pid or any sandbox VM exists | `worms test --live` during a round exits 1 with a reason |
| Q8 | Sandbox stack: set `on_boot = false` for every sandbox VM | after a host reboot a kept sandbox stays stopped |

---

### P0: state integrity

#### T1. Give controller state one home, bound to one system (enables clean checkout migration)
- **Specific.**
  - Move every git-ignored stateful file out of the checkout into a per-system state directory on the host,
    `/var/lib/worms/controller/` (laptop mode: fetched or locked via SSH, or dropped: see T10). That covers
    the five tfstates, `vault.yml`, `.vault_pass`, `pki/` and the OS images.
  - Add a `system.json` there: host name, PVE cluster fsid or `/etc/machine-id`, the tfstate lineages, and
    the vault's WORMS public-key fingerprint.
  - The CLI resolves `WORMS_STATE_DIR` and refuses to run any changing command when `system.json` does not
    match the host it talks to.
  - The checkout then contains code only.
- **Measurable.**
  - `git clone` into a new directory, one line in `~/.worms/config` (or nothing, if the state dir is on the
    host), and `worms status` shows no "error" rows.
  - `worms apply --plan` shows "no changes" for every stack.
  - Pointing a dev state dir at the prod host fails before any tofu or Ansible call, with a message naming
    both systems.
  - `find <checkout> -name '*.tfstate' -o -name vault.yml -o -name .vault_pass` returns nothing.
- **Relevant.** Directly removes failure modes 4.1/1, 2, 3 and 5. "Migrate to a new checkout" becomes
  "clone + run". It also makes `worms upgrade`'s backup a copy of one directory.
- **Subtasks.**
  1. Inventory every path `upgrade.STATE_FILES`/`HOST_STATES` and `secrets.Vault` touch. Add the images and
     `/var/lib/worms/outputs`.
  2. Controller stacks: `backend "local" { path = var/…}` via `-backend-config=path=$WORMS_STATE_DIR/<stack>.tfstate`
     in `Runner.tofu`. Replace the `terraform_remote_state` relative paths
     (`infrastructure/*/main.tf:31-45`, `private_internet/main.tf:31-45`) with the published
     `outputs/*.json`, the same mechanism the host stacks already use (T2 of the previous round). This
     removes the cwd dependence too.
  3. `Vault(repo_root)` → `Vault(state_dir)`. `Config.default_ca()` and `ca_create` use
     `$WORMS_STATE_DIR/pki`. Make `ca_create` **fail** when a CA is expected (`system.json` exists) but
     missing.
  4. Write `system.json` on first `worms init`. Check it in `Session.__init__` for every LOCKED command.
  5. Make `secrets.vms_deployed()` ask Proxmox (any VM in the platform pools), not the local tfstate.
  6. One-time migration command, `worms state adopt <old-checkout>`: move the files, write `system.json`,
     print a `sha256sum` manifest before and after.
  7. Update `upgrade.py` (backup = tar of the state dir plus host state) and docs/02.

#### T2. Fail closed when the CLI's picture is incomplete
- **Specific.** Every step that derives "what exists" must refuse to continue on an incomplete picture,
  instead of continuing with fewer hosts.
- **Measurable.**
  - With one VM stack's state missing, `worms apply dns` stops before Site.yml with "inventory incomplete:
    stack X has no outputs, Y VMs exist in Proxmox pool Z".
  - The DNS zone file never loses a record because a group is empty (a test renders the template with an
    empty group and asserts that the run fails).
  - With every stack state present, behaviour is unchanged.
- **Relevant.** It stops the fake-success / DNS-wipe chain (4.1/2, 4.1/4), which today makes an incident
  look like a successful apply followed by mysterious sandbox failures.
- **Subtasks.**
  1. `generate_inventory.py:420-424`: exit non-zero when a stack has no outputs but Proxmox has VMs in its
     pool. Pass `--allow-partial` only for `--all` on a fresh host.
  2. Cross-check the inventory against the Proxmox VM listing: every VM in the platform pools must be in the
     inventory, and the reverse.
  3. `setup_dns` template: render a record for every `dns_entries` item, using the static IP
     (`shared-variables.tf`), whether or not the group is populated. Or assert that groups are non-empty.
  4. Q1 (no-hosts-matched warning), escalated to an error for Site.yml when `--limit` names a group.
  5. `components.plan`: if the Proxmox listing and the tfstate disagree on whether a component exists, show
     both in the plan and refuse the "create (dependency)" action for stacks.

#### T3. A sandbox belongs to exactly one round, and resume reconciles explicitly
- **Specific.**
  - Record `{round_id, gameplan_sha256, hcl_sha256, generator_commit, built_at}` in `sandbox-build.json` and
    in the description of every sandbox VM's `clean` snapshot.
  - `round start` for a different round **refuses** if another round's sandbox exists (with the teardown
    command), instead of destroying it silently (`handlers.py:449-451`).
  - `--resume` compares the record to the current definition and, on any difference, says exactly what
    differs. It asks before rebuilding, and states that teams already run will have had a different sandbox
    ("rebuild + replan all teams" vs "abort").
  - A missing snapshot on resume is reported as such, not silently rebuilt.
- **Measurable.**
  - A test plays out round A kept, then `round start B`: refused, nothing destroyed.
  - A changed generator between start and resume prompts, with the diff of the HCL hash.
  - The `summary.log` of any session names the sandbox's round id and build time.
- **Relevant.** Prevents silent mid-round rebuilds (an unfair round) and cross-round destruction (an hour
  lost). It turns the five sandbox markers into one authoritative record.
- **Subtasks.**
  1. Extend `_record_build` (`handlers.py:~310`) and `snapshots.take` description.
  2. `_build_sandbox`: replace the destroy branch with a `Refused` carrying the owning round.
  3. `Conductor.stale()`: add `hcl_sha256` and `generator_commit` to the staleness reasons.
  4. `worms status`: show "sandbox: round <name>, built <t>, snapshot <t>" from the record. Flag any
     disagreement among the record, tfstate, pool and snapshots as one row.
  5. `teardown`: stop all sandbox VMs (including `sandbox-gw`) *before* clearing current-team, and make the
     clearing failure a warning that does not block the stop (`handlers.py:1431`).

#### T4. Round session gate: versions, clocks, invariants, database resilience
- **Specific.**
  - **Before step 4 of `round start`, refuse (or ask with a red summary) when any of these holds:**
    - pending migrations, or a database ahead of the checkout;
    - the orchestrator, dashboard or scoring code running a different commit than the checkout
      (`running-<app>`, sandbox_api deployed commit);
    - a service VM's `clean` snapshot commit differs from `deployed.json`;
    - host↔database or host↔scoring clock skew above 10 s (read `now()` from PostgreSQL and `date +%s`
      via SSH).
  - **During the round, on every watch tick (10 s), re-check cheaply:**
    - the orchestrator units are inactive;
    - `worms-round-agent` is inactive;
    - no devbox is running;
    - the internet flag is absent.
    If one fails, stop at the next safe point with a named reason.
  - Wrap every ORM use in the conductor loop with `close_old_connections()` and one retry on
    `OperationalError`.
- **Measurable.**
  - A test with a fake pending migration: `round start` exits before stopping anything.
  - Starting the agent by hand mid-round: the session reports "round agent started during the round" within
    20 s.
  - A simulated database disconnect mid-attempt: the attempt continues, or fails with "database unreachable"
    and is left non-ACTIVE.
  - Each session's `summary.log` header lists the measured clock skews.
- **Relevant.** Removes three ways a round dies mid-way with an opaque error, and one way (skew) that scores
  silently wrong.
- **Subtasks.**
  1. `rounds._sync_workspace`: add the version comparisons (reuse `manage._version_lines` /
     `consistency.version_summary`).
  2. `django_setup._behind`: raise in round context instead of printing.
  3. Skew probe: one SQL `select extract(epoch from now())`, plus SSH `date +%s` to scoring and logging.
     Store it on the RoundRun (new JSON field or `failure`-like `env` field).
  4. Invariant checker in `Conductor.on_watch` (`conductor.py:299`) next to `_count`.
  5. `close_old_connections` + retry wrapper. Add a test with a killed connection.

---

### P1: find the failure in minutes

#### T5. One operations ledger and one "why" command
- **Specific.**
  - Every changing operation, wherever it runs, appends one JSON line on the host to
    `/var/log/worms/operations.jsonl` (flock, like `vm-history.jsonl`). That covers CLI commands, round
    sessions and their steps, and agent jobs. Each line holds:
    `{id, start, end, who, where (host|laptop:<name>), command, commit, exit, error_headline, log_path_on_host}`.
  - Run logs are written on the host (`/var/log/worms/runs/`) even when the CLI runs on a laptop: stream
    them over the existing SSH session, or copy them at the end.
  - `worms why [<id>|latest|round <r> [team]]` prints:
    - the failing tool (tofu, ansible, qm, ssh, Django);
    - host or VM;
    - task, role, file:line;
    - the full error;
    - the log path;
    - what ran just before on the platform (the ledger lines of the previous 30 min).
- **Measurable.**
  - For every failure type in the test suite (tofu error, Ansible task failure, SSH timeout, round step
    failure, agent job failure), `worms why latest` shows the tool, target and error on one screen.
  - Any operator logged in on the host sees runs started from a laptop.
- **Relevant.** Today the first 30–60 minutes of an incident go into *finding* the right log among
  `~/.worms/runs`, `~/.worms/rounds`, `/var/log/worms/jobs`, journals and DB fields. This makes it one
  command.
- **Subtasks.**
  1. Ledger writer in `runlog.Tee` start/stop, `rounds.LocalJobs`, and `round_agent/executor.JobLog`.
  2. Generalise round_agent `failure.py` (it already extracts Ansible's failed task with VM, file:line, msg,
     and OpenTofu's `Error:`). Use it for CLI runs too; store `error_headline` in the ledger.
  3. `worms why` (reads the ledger, then the log file). `worms status` attention block: "last failed
     operation: … (`worms why <id>`)".
  4. Store the full failure text, not only *where*, for the `_quiet` handlers in the host-only
     `failure.detail` (`handlers.py:203-225`). Keep it out of the team-visible fields.

#### T6. Preserve evidence before cleaning up
- **Specific.** When a build step fails, keep enough state to diagnose it *before* any automatic rollback,
  restart or offline re-run:
  - take a Proxmox snapshot `failed-<ts>` (service VMs) or keep the VM unrolled;
  - collect `journalctl -b -n 500` and `cloud-init status --long` from each affected VM into the run's
    folder;
  - only then perform `_back_to_snapshot`.
  Print the original error *again* after the cleanup output.
- **Measurable.**
  - After a forced failure of a service-VM role, the run folder contains `<vm>.journal` and `failed-<ts>` is
    listed in `qm listsnapshot`.
  - The run's last line repeats the original error.
  - `worms vm forget-failed` (or a status row) removes `failed-*` snapshots older than 7 days.
- **Relevant.** Today the failed VM state is rolled back away and the original error is buried under the
  cleanup Site.yml run (`pipeline.py:251-281`).
- **Subtasks.**
  1. Hook in `Pipeline._leave_after_failure` before `_back_to_snapshot`.
  2. Same for `conductor.fail()`: collect `systemctl status worms-start`, `journalctl -u worms-start` and
     `journalctl -t capture-vm` from the run's VMs before stop/rollback (feeds T7).
  3. Re-print the error headline at the end of `_execute`.

#### T7. Scoring correctness and an attempt report
- **Specific.**
  - **(a)** Replace the scoring/logging readiness probe with what `worms check` already does: POST `{}`,
    expect the app's 400, so a 403/409/502/503 fails the gate (`probe.py:177-187`). Run that probe in the
    preflight before *every* attempt, not only when no fresh snapshot was taken (`rounds.py:871`).
  - **(b) (verify first)** Decide the worm process contract. Either pass `--property=KillMode=process` (or
    `RemainAfterExit=yes` with an explicit stop at the end) to `_arm_worm`, or document that `main.sh` must
    stay in the foreground. Apply the same to devbox runs (`handlers.py:1594`) so dev and live behave
    identically.
  - **(c)** Persist per attempt:
    - the worm's unit result and exit code and the last 200 journal lines (collected before the VMs stop);
    - for every submission that was not counted, a reason (`wrong_team`, `outside_window`, `unknown_hash`,
      `old_hash_of_run_vm`, `duplicate`) stored on `FlagCaptureAttempt`;
    - per-minute rejection counters from the scoring app (status code → count), pushed with the attempt's
      team uuid.
  - **(d)** `worms round report <round> [<team>]` (and the staff run page) shows, for every team of the
    round:
    - identity: upload hash, zip lint (CRLF/BOM/shebang/`main.sh` location);
    - gameplan sha;
    - measured clock skews (T4);
    - every check, including warnings;
    - the worm's exit and output tail;
    - flags per VM (all hashes and windows);
    - submissions accepted, not counted (by reason) and rejected (by code);
    - captures;
    - schedule drift against `simulation.py`'s nominal times.
- **Measurable.**
  - Stopping gunicorn on scoring makes the next attempt's check fail with "scoring: POST /submit → 502".
  - For a test attempt, the report lists a not-counted submission with its reason.
  - Comparing two teams side by side fits on one screen.
  - Every field in the report comes from the DB or the session folder; no SSH to a rolled-back VM is needed.
- **Relevant.** This is the "why did they score badly?" workflow. Today it needs manual archaeology across
  the DB, Zeek flows and wiped journals, and can still not exclude platform faults.
- **Subtasks.**
  1. Probe change plus GATES entry. Add a unit test that a 403 / 502 / 503 fails.
  2. One live experiment on dev: `main.sh` that does `sleep 600 & exit 0`; check whether the sleep survives.
     Decide and implement (b) based on the result. Fix the `worm.sh` skeleton (`root`, `/root/worm`) and the
     user guide.
  3. Migration: `FlagCaptureAttempt.not_counted_reason`. Set it in `captures.process_flags`.
  4. Scoring app: in-memory counters flushed every 30 s to a small table or a log line with the team uuid.
     Turn on an nginx `access_log` for `/submit` with status only (no body).
  5. Zip lint at upload time (dashboard): warn the team on CRLF, BOM, non-bash shebang, nested `main.sh`.
  6. Report command and staff page section.

#### T8. Make Ansible failures and skips visible
- **Specific.**
  - The worms callback prints a per-run footer: failed tasks, *ignored* errors, *rescued* blocks, roles
    skipped by stamp, plays with no hosts. Each line has VM, role, task and file:line.
  - Host-workspace plays run over SSH get per-VM log files too: set `WORMS_ANSIBLE_LOGS` in `worms-host-run`
    and copy the directory back into the run folder.
  - Fix the tasks that report a change on every run (host_network "Restore internet gate", round_agent
    `pveum` tasks, Internet.yml "Open the host gate").
  - Fix `grow_disks` rc handling: distinguish NOCHANGE from errors.
- **Measurable.**
  - A second `worms apply infrastructure` with no code change shows `changed=0` on every VM.
  - The footer lists every ignored error of the run.
  - `worms log run <vm>` works for sandbox and devbox VMs configured from the host.
- **Relevant.** With "Ansible has an error somewhere", the operator should read one footer, not scroll 5,000
  lines. With "changed" meaning something again, drift becomes visible.
- **Subtasks.** Listed in the specific part; one PR each.

---

### P2: shrink the mental model

#### T9. One state registry, as code
- **Specific.**
  - Create `src/worms_cli/state_registry.py`: one table of every state location, each with:
    - path or pattern;
    - where it lives (checkout / state dir / host / VM / Proxmox / DB);
    - owner (the single writer);
    - readers;
    - whether it is a *source* or a *derived copy*, and how the copy is regenerated;
    - whether it is backed up.
  - `worms status`, `worms upgrade` (backup), T1's `state adopt`, and the docs table are generated from or
    checked against it.
  - A unit test greps the code for `/var/lib/worms`, `/etc/worms`, `/opt/worms` and `/run/worms` literals
    and fails if one is not in the registry.
- **Measurable.**
  - The test passes with the registry as the only list.
  - `worms state` prints the table with live presence/age per entry.
  - docs/01's state section is generated.
- **Relevant.** This *is* the mental model, written down once and enforced. New state can no longer appear
  without an owner.
- **Subtasks.**
  1. Seed the table from §3 of this document.
  2. Grep test.
  3. `worms state` command.
  4. Replace the ad-hoc lists in `upgrade.STATE_FILES` and `HOST_STATES`.

#### T10. Remove execution modes you do not use
- **Specific.** Decide, then delete:
  - **(a) Laptop mode (`WORMS_SSH`).** `round start` and `teardown` are already host-only. If day-to-day
    work happens on the host anyway, delete laptop mode: tunnels, the laptop inventory variant,
    `_check_inventory`, the per-user config, and the three network paths. If it must stay, make it a thin
    `ssh host worms …` wrapper that runs the CLI *on the host* instead of running tofu and Ansible locally.
  - **(b) Agent-run rounds.**
    - Remove the round handlers from the agent's `HANDLERS` (`handlers.py:1889`), along with their queue,
      timeout and heartbeat semantics in `LocalJobs`/`AttemptBackend`, and the unused
      `hold_devboxes`/`release_devboxes`.
    - Remove the devbox-restart branches in `_build_sandbox` and `snapshot`.
    - The agent then means "devbox jobs only".
  - **(c) Controller vs workspace by file existence.** With T1, the workspace is identified explicitly
    (`WORMS_ROLE=workspace` in `round-agent.env`), not by `has_vault()`.
- **Measurable.**
  - `grep -n "laptop" src/worms_cli` → 0 hits (option a), or every laptop command is `ssh host worms`.
  - The agent's HANDLERS contain only devbox, keys and gameplans-check jobs.
  - Net lines removed are reported in the PR (expected: several hundred).
  - docs/06 and docs/11 no longer mention `conduct_rounds`.
- **Relevant.** Each mode removed halves the "where did this run, with which code and which credentials?"
  question during an incident.
- **Subtasks.** One PR per mode. Run the existing tests after each.

#### T11. The round agent and the CLI never act on the platform at the same time
- **Specific.**
  - The agent takes the same host lock (`/run/worms/worms.lock`, shared or exclusive per job kind) around
    every job.
  - `worms apply` that enters `DEVBOXES_OFF` or rolls back services first pauses the agent and waits for its
    running job (reuse `services.wait_until_idle`).
  - `agent-sync` waits for an idle agent before rsyncing `/opt/worms`.
- **Measurable.**
  - With a devbox rebuild job running, `worms apply scoring` waits with "waiting for the round agent's job
    #n (devbox_rebuild alpha)" and proceeds after it.
  - Two concurrent tofu applies on the devbox stack are impossible (a test with two lock holders).
- **Relevant.** Removes a whole class of "it failed once and I cannot reproduce it" errors (mixed code,
  devboxes started during the internet window, tofu lock errors).

#### T12. Collapse duplicate truths
- **Specific.**
  - **(a) One version record.** `deployed.json` (written atomically, with lineage and checkout path) is the
    only record of what runs. The snapshot description, `.worms-commit` and `running-<app>` are *verified
    against it*, not consulted as alternatives. `worms status`'s version block reads one source and lists
    disagreements.
  - **(b) One network/name source.** Generate `src/worms_cli/constants.py` and an Ansible
    `group_vars/generated_network.yml` from `shared-variables.tf` (or move both to one YAML that tofu reads
    with `yamldecode`). Replace the literals in `generate_devbox.py`, `gameplan/definition.py`,
    `dashboard/views/debug.py`, `round_agent/startup.py`, and the pool, gateway and VM names.
  - **(c) One existence check.** "Does component X exist?" = Proxmox listing. The tfstate disagreeing with it
    is reported (T2), never used as a silent alternative.
  - **(d) One GameplanFile writer.** Only the round session or only the orchestrator refreshes it (pick the
    one that runs rounds: the CLI), and the other reads.
- **Measurable.**
  - `grep -rn "10\.128\.\|10\.64\.\|vmbr23\|devbox-ssh-gateway" src scripts projects --include=*.py` hits
    only the generated constants module.
  - `worms status` version block code path reads `deployed.json` once.
- **Relevant.** Fewer copies means fewer drift rows in `worms status` and fewer places to check during an
  incident.

#### T13. Internet window and secrets: one authority each, reconciled
- **Specific.**
  - **Internet window.** The PVE provisioning rules are the authority. At boot, a systemd unit runs
    "window close" (removes the provisioning rules, empties the nft set, deletes the flag) unless an operator
    explicitly re-opens it. `worms status` reads all three and reports any disagreement as an error.
  - **Secrets.** `worms secret issue <x>` and rotation immediately re-render every copy listed in the T9
    registry for that secret: `round-agent.env` via `SetupRoundAgent.yml --tags env`, the orchestrator env
    via a targeted role tag. Until then it refuses to delete the old Proxmox token (`tokens.py:133-135`).
    `worms check tokens` verifies each *copy*, not only the vault.
- **Measurable.**
  - Reboot during an open window: after boot `worms status` shows the window closed and the PVE rules
    absent.
  - `worms secret issue proxmox-token` followed immediately by a devbox job: the job authenticates.
- **Relevant.** Two "impossible" states (window half-open; agent with a dead token) disappear.

---

### P3: fairness and hygiene

#### T14. Equal conditions for every team
- **Specific.**
  - Before each attempt, restart `sandbox_api` on scoring and logging (clears the limiter and bans) and
    flush resolver and apt-cacher state that a worm can influence, or roll scoring, logging and DNS back to
    `clean` per attempt if the 1–2 minutes are acceptable.
  - Wait for every VM to report SSH after `snapshots.take`, as after a rollback (`snapshots.py:132-139`), so
    the first team boots like the others.
  - Use the live `qm status` (not pvestatd) in `prepare` and in `stop`.
  - Stop all run VMs in parallel at `ends_at`, or freeze them (`qm suspend`) at once, then stop.
  - Record each gameplan step's scheduled vs actual time as RunEvents; include the drift in T7's report.
- **Measurable.**
  - The report shows per-team drift.
  - With a synthetic load, the spread of step offsets across teams is below a threshold you choose (e.g.
    15 s).
  - The limiter state is empty at each worm start (a test against the scoring app).
- **Relevant.** Removes order and timing effects from scores; makes "the platform treated them differently"
  answerable.

#### T15. Ansible stamps and tofu ordering correctness
- **Specific.**
  - `worms_role_gate.py` hashes the files of included roles (`include_role`/`import_role` names) recursively.
  - Service-VM apply order: roll back to `clean` *before* applying tofu changes to that VM, then configure,
    then snapshot. A tofu config change then survives in the new snapshot.
  - `worms apply <vm>` also runs a targeted `tofu plan` for existing VMs and shows in-place changes (apply
    them if safe, as `_apply_changed` does).
- **Measurable.**
  - Changing `grow_disks` re-runs `devpi`'s and `apt_cache`'s roles without `--full`.
  - **(verify)** Raising the scoring VM's RAM via tofu persists after the next round build.
- **Relevant.** Removes the remaining "I changed it, apply said OK, but nothing changed" cases.

#### T16. Strict gates for what a gameplan promises
- **Specific.**
  - Classify every check in `handlers.py:963` (`GATES`) per gameplan as *required* (the attempt does not
    start) or *advisory*.
  - By default, gate on:
    - sshd on the VMs the gameplan exposes;
    - the gameplan's own users and services;
    - its firewall expectations;
    - its files.
  - Advisory warnings are shown in T7's report and on the staff page for every team, so differences between
    teams are visible.
- **Measurable.**
  - A sandbox with sshd stopped on an exposed VM does not start an attempt; the reason is shown.
  - Every advisory warning appears on the staff run page.
- **Relevant.** An attempt that runs on a broken sandbox costs a team its points *and* an operator a manual
  investigation.

---

## 6. Suggested order of work

1. **Week 1: quick wins Q1–Q8.** They are low risk and each removes a silent failure.
2. **T1, then T2.** Do this before the next checkout migration; the migration itself becomes the
   acceptance test.
3. **T3 + T4, then T7(a)(b).** Do this before the next live round.
4. **T5 + T6.** After this, every later incident is cheaper.
5. **T9, T10, T11.** This shrinks the system; do it after T1 so the registry describes the new layout.
6. **T12, T13, T14, T15, T16.**

## 7. How each daily command changes once this is done

| Command | Today | After |
|---|---|---|
| `worms status` | 16 probes, compares copies, version block from 5 sources | one version record, a state-registry check, "last failed operation → `worms why`" |
| `worms apply <c>` | may silently skip, record fake success, revert tofu by rollback, race the agent | fails closed on a partial picture, waits for the agent, shows the full error with playbook/limit, keeps evidence |
| `worms round start` / `--resume` | gates checked once, sandbox not owned, no version or clock check | versions, skew and invariants checked throughout; sandbox owned by the round; resume says exactly what differs |
| `worms round teardown` | can leave sandbox VMs running when scoring is down | stops first, clears after |
| `worms login` / `worms check` | unchanged (they work well) | `check` reuses the stricter scoring probe |
| new checkout | copy 5 tfstates, vault, key, pki, images by hand; risk of a new key or CA | `git clone`, done; a wrong system is refused |
