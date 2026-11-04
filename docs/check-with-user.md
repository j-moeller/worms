# Steps that need you

What is waiting on you after the infrastructure setup and the end-to-end run, as of 2026-11-04.
Background is in `docs/e2e-run-2026-11-04/protocol.md`, `docs/e2e-run-2026-11-03/protocol.md`, `docs/progress-setup-infrastructure.md` and
`docs/pi-failures.md`.

## Do these

### 1. Retire the old provisioning token

- [ ] Tell me to do it.

The end-to-end run answered the question that the earlier throwaway test was for: the new token
(`worms-provision@pve!worms`) created 13 VMs, 6 bridges and the firewall rules with no permission errors
(`docs/e2e-run-2026-11-03/protocol.md`, Stage 2). The throwaway test is no longer needed.

**What happens next:** I remove the old `terraform-prov@pve` token and its `Administrator` grant, and
disable that user.

**Until then:** the old token and grant stay as a fallback.

### 1b. Run the teardown once more, so the suite can run from start to finish

- [ ] Run this, or add a permission rule for it:

```sh
/usr/local/sbin/worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve -parallelism=1'
```

The automated suite (`tests/live`) starts only from the base state, and the safety check refuses this command when I try it. It removes only the 13 devbox VMs.
The added `-parallelism=1` stops it leaving volumes behind, as last time (`docs/e2e-run-2026-11-04/protocol.md`, G7).

**What happens next:** I run the suite with `--live-wipe-database`, which deletes the test users and teams and takes about an hour and a half. That also puts the G7 fix through its first teardown. Until then the suite's account steps and its Reset button have only been tried by the manual scripts it came from.

### 2. Delete the old credential copies

- [ ] Run this from the repo root:

```sh
find opentofu -name 'credentials.auto.tfvars.disabled' -not -path '*/.terraform/*' -delete
rm .env
```

The safety check blocked me from deleting these, so I renamed the tfvars files instead. The
vault now holds the tokens that are in use. `.env` also holds the password of
`terraform-prov@pve`, which nothing here uses. If you might want that password later, save it
first.

### 3. Test the dashboard from outside by name

- [ ] Run this from your laptop and expect a `200`:

```sh
curl -vk https://worms-dev.mlsec.tu-berlin.de/
```

You confirmed access by IP earlier. The IP now gets `400`, because the name is the only one
accepted. The host cannot test this itself.

## Decide these

### 3b. Publish the devbox SSH gateway

- [ ] Say yes or no.

Teams cannot reach their devboxes from outside: port 2222 is not published (`host_publish_ssh_gateway: false`). The
dashboard now shows `ssh -J team@worms-dev.mlsec.tu-berlin.de:2222 root@...`, but that command only works once the port is open. I
tested SSH from the node, which covers everything except the public port forward. I did not open a public port without
asking.

### 3c. What the apt cache should hold

- [ ] Choose one.

With the internet closed, a team can install only what the cache already holds: about 150 packages that the VMs themselves
needed, plus the seed list (`apt_cache_seed_packages`). `apt install cowsay` fails.

| Option | Trade-off |
|---|---|
| A longer seed list you choose | Small and predictable, but teams get only what you foresaw |
| Mirror a large part of Debian | Teams can install nearly anything, but it takes a lot of disk and download time |
| Keep it minimal | Nothing to do, but tell teams to vendor what they need (`docs/worm-contract.md` already suggests this) |

### 3d. Try a TRIM of the disk

- [ ] Say yes or no, or run it yourself.

```sh
zpool trim rpool        # then watch it with: zpool status -t rpool
```

The disk looks like the main reason builds fail and steps seem to hang. A ZFS write cycle took up to 110 seconds, and Proxmox allows `zfs create` only 10 seconds, so 4 of 7 devbox builds failed on it in the 2026-11-04 run (`docs/e2e-run-2026-11-04/protocol.md`, F5).
`zpool status -t` shows the disk as `(untrimmed)`, so this may help, but I have not proven it. It is safe for ZFS, but some older Samsung SATA drives have a history of TRIM bugs, so take a look at your backups and the drive's firmware first. It also slows the disk while it runs, so not during a build.

**If it does not help:** the options are a faster disk for the VMs, or having the agent retry on this error, which it does not do yet.

### 3e. Record devbox traffic?

- [ ] Say if you want this. The default is to leave it as it is.

Devbox traffic is not recorded, by design (`docs/requirements.md`, Monitor), so `network_flows` has no devbox rows. The setting `host_mirror_extra_bridges` exists but is not tested for
devboxes. Its rule for gateways names only `sandbox-gw`, so devbox gateways would probably produce duplicate records until that is changed too.

### 4. Public certificate for the dashboard

- [ ] Choose one.

The certificate has the right name, but browsers warn because it comes from the internal CA.

| Option | Trade-off |
|---|---|
| Certificate issued through TU Berlin | No browser warning. Probably renewed by hand |
| Automatic public certificate (ACME) | No browser warning and renews itself, but it is awkward with the VMs' internet closed |
| Keep the internal CA | Nothing to do, but every browser warns unless it trusts the CA |

### 5. Filling the private internet caches

- [ ] Choose one.

After `make`, devpi has 0 files, the registry is empty, cve-search has 0 records and Wikipedia is
not running. The documented way to fill them cannot run as written (`docs/pi-failures.md`,
findings 5 and 6).

| Option | Trade-off |
|---|---|
| `make` fills them | Works out of the box, but builds take much longer and download a lot |
| A separate command, with corrected docs | Builds stay fast, but the services are empty until someone runs it |

### 6. Man pages fix

- [ ] Choose one.

Every man page returns `503`, because the systemd hardening blocks Apache's CGI daemon
(`docs/pi-failures.md`, finding 4).

| Option | Trade-off |
|---|---|
| Add `CAP_DAC_OVERRIDE` and `CAP_CHOWN` | Tested and works, but `CAP_DAC_OVERRIDE` is a broad capability on a service sandboxes can reach |
| Serve man pages without CGI | Keeps the tight hardening, but takes more work |

### 7. Smaller fixes

- [ ] Tell me which of these you want. All are documented but not changed.

| Item | Effect now | Reference |
|---|---|---|
| NTP cannot reach its upstream servers | Clients get the VM's own clock, which nothing corrects | `pi-failures.md`, finding 7 |
| Private internet VMs never get upgrades | 60 packages behind | `pi-failures.md`, finding 11 |
| Port isolation is not active | One defense layer is missing; it needs `root@pam` to attach | `pi-failures.md`, finding 9 |
| Report page and cve-search UI have no login | Information exposure on the services bridge | `pi-failures.md`, finding 10 |
| `Update all packages` prints nothing for about 16 minutes | A fresh build still looks stuck there | `progress-setup-infrastructure.md` |
| `no_log` hides migration errors | `make` prints only "censored" when one fails | `progress-setup-infrastructure.md` |
| A failed devbox reset looks like a success | The team loses its score, the VMs stay as they were, and the page keeps saying "being rebuilt" | E2E protocol, F25 |
| Untrusted VMs run without a firewall while being built | Up to 28 minutes measured. The host's own rules still apply | E2E protocol, F4 |
| Nothing checks that infrastructure VMs have a firewall | The watchdog watches only sandbox and devbox VMs. This is how the SSH gateway ran unprotected for 2 hours | E2E protocol, F26 |
| With devboxes up, there is no supported way to deploy | `make` and `worms build provision` refuse, and no command removes only the devboxes. Fixes in this run had to be copied by hand | E2E protocol, F6 |
| The agent throws away Ansible's output | A failed job usually says only `exited 2`. Each failure in this run had to be reproduced by hand. Keeping the output needs care, as it may hold secrets | E2E protocol, F9 |
| `agent-sync` does not restart the agent | New playbooks take effect, but new agent code does not until `systemctl restart worms-round-agent`. Restarting during a job would kill it | E2E protocol, Stage 2 |
| An interrupted build leaves things behind | A VM outside OpenTofu's state, or an `apt` on a VM holding its lock. There is also no command to cancel a job | E2E protocol, F2 |
| The dashboard shows no VM addresses, and Copy covers the end of the SSH command | Users must find B and C from inside A | E2E protocol, F18 |
| The API's documented limits differ | `GET` gets 403 rather than 405, and logging accepts more than 4 KiB | E2E protocol, F23 |
| A user for a full or unknown team still gets an account | It has no team, and there is no delete button | E2E protocol, F1 |
| Live header says "ROUND IN PROGRESS (DEV)" with 0 rounds | `WORMS 2026` has no registration dates, which the code treats as a dev round. Setting a registration window in the staff pages clears it; the logic could also be fixed | `dashboard/middleware.py` |

### 8. Commit the changes

- [ ] Tell me when to commit, and how you want it split.

Nothing is committed. **Your `proxmox_instance = worms-dev` line should stay out of any commit**:
the repo's default should remain `worms-pve`.

## On hold, until you say otherwise

- **Parallelizing Ansible:** you asked me not to yet.
- **A full rebuild** (`make destroy_all`, then `make`): you asked to avoid it. It is still the only
  way to test the first-run paths on brand-new VMs, so consider one before going live.

## Before going live on `worms-pve`

- [ ] Create a DNS record for `worms.mlsec.tu-berlin.de`. It does not resolve yet.
- [ ] Make sure `proxmox_instance` is `worms-pve`. A node not listed in `dashboard_public_names`
      (`group_vars/all.yml`) fails the run.
- [ ] Plan the rename. OpenTofu's state and the VMs are tied to the node name, so `make destroy_all`
      should probably run before the rename and `make` after it. This order is not tested.
- [ ] Confirm the `cve_search` RAM setting. Your uncommitted 1024 MB has not been tested with
      `cve-search-populate` running.
- [ ] Set your own admin password. The end-to-end run set a random one, kept in
      `/tmp/claude-0/-root-app/c3c46757-27f6-4301-a530-4ad1743ed0bd/scratchpad/e2e2/creds.json`, which will not last forever. The CLI needs the packages in `.venv-test`:

```sh
.venv-test/bin/python src/manage.py admin reset-password
```

- [ ] Remove the test's data: the 9 test accounts, the teams `alpha`, `bravo` and `charlie`, their devboxes, and the
      `WORMS 2026 · dev` competition. The commands are in `docs/testing.md`, "The live devbox journey".
