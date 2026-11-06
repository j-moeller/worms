# Branch reconciliation, 2026-11-05

This integrates `private-internet-hardening` at `0eb8042` and `main` through `211d6ac`
into the previously published `end-to-end-tests` at `291e8e8`. Integration and
verification run in an isolated local checkout; no deployment, data reset or VM
operation is part of this work.

## Resolution decisions

- Keep main's current infrastructure and per-service firewall groups. Main
  independently contains the effective hardening changes; preserve its later
  fixes, including devpi's memory policy, offline cache seeding and CGI support.
- Keep the retired per-VM cloud-init templates deleted. The APT cache ownership
  fix runs in Ansible before installing/starting the package.
- Keep main's standard-library dashboard HTTPS client and current profile/SSH-key
  UI. Retain explicit CA overrides, managed service-name resolution and app/daemon
  restarts. The Django app role owns Proxmox CA distribution, avoiding a duplicate
  separate play. The dashboard no longer needs Requests installed.
- Preserve membership/team locking, case-insensitive account/team uniqueness,
  atomic upload repair, serialized round planning and daemon reconnection.
- Keep main's database-unique SSH fingerprints, canonicalize their base64 identity,
  and preserve the membership recheck, 64-key limit and retry/re-push behavior.
- Keep main's immutable SSH job snapshots and agent supersession. Shared producer
  locking and a full-stack-job barrier prevent a later rebuild/provision from
  undoing a current key update. No `UPDATE(params)` grant is needed.

## Database upgrade

Do not restore the branch's obsolete migration history. Main's `0001_initial`
stays unchanged. New migrations `0002_case_insensitive_accounts_and_teams` and
`0003_canonical_ssh_key_fingerprints` upgrade that deployed schema.

Case collisions or duplicate canonical SSH identities stop the respective
migration without deleting/merging records. Resolve those explicitly before
re-running migrations. Regression tests cover fresh installs, preservation of
populated accounts/uploads/logs, and collision failures without data loss.

## Review of Jonas's work

A read-only snapshot was taken at main `c9a8b44`. The private patch and its hash
are kept outside this repository. The node was not changed.

The feature patch adds round-independent devbox progression, log records and
ZIP/API downloads, and switches the worm entry point from `main.py` to `main.sh`.
An isolated preview passed 204 focused tests covering profile/progression views,
team-scoped API and ZIP access, uploads, the shell worm contract, agent command
construction and browser rendering. Those rendering tests use local fixture
responses; they are not a live Proxmox/sandbox rehearsal.

Jonas then published the feature patch as `abcde6a` and `211d6ac` while validation
was running. Both official commits are included here. The separate preview is
retained as review evidence; no private snapshot files were added to this branch.
The merged views retain the membership-locking imports used by SSH registration.

A subsequent read-only check of the node at `211d6ac` found only its hostname and
cve-search RAM overrides remaining tracked but uncommitted, plus `docs/IMPORTANT`
and the empty `tile`. These local settings and private files remain untouched.

Previously accepted archives with only `main.py` will need to be replaced or an
explicit compatibility policy chosen before the new launcher runs them.

## Browser regression

The combined browser rehearsal reproduced a lost-click bug: loading network
connections rebuilt the sidebar between pointer-down and pointer-up on a log
entry. The connections count now updates in place, preserving the event row.
The browser driver deliberately delivers that response during the click rather
than masking the failure with a retry or a longer timeout.

## Final validation

The full suite on code revision `db9be86` passed in the isolated Colima Linux
container: **807 passed, 15 skipped in 1233.07 seconds**. The skips are exactly
the opt-in `tests/live/test_devbox_journey.py` cases requiring `--live` and a
deployed platform. No failed tests remain in this run.

Coverage includes unit and integration checks, PostgreSQL grants/concurrency and
populated upgrades, Ansible deployment/connectivity regressions, a real local
nginx/Gunicorn/TLS stack with worker/database recovery, and Chromium, Firefox and
WebKit log/replay/ZIP journeys. The forced background-response/click regression
passes in all three engines. The earlier separate feature preview passed 204
focused tests; that is a separate run, not additional unique test coverage.

The final remote refresh still had main at `211d6ac`. Both that main revision and
hardening `0eb8042` are ancestors of this reconciliation. The shared platform was
not deployed or reset as part of validation.

## Remaining live work

No shared deployment, VM state or application data was changed. The opt-in live
suite requires a deployed revision and is separate from the local rehearsal.
Next, deploy the agreed tested revision without overwriting node-local settings,
then exercise real devbox/sandbox provisioning, private-internet access and a
complete round with logs, scores and downloads.

PyPI currently seeds only `tabulate`; the internal index serves that package but
has no cached `tqdm`. Select and fill the intended offline package set before
checking arbitrary pip installs. Respect the builder's internet guard while
student devboxes exist; do not tear down those devboxes merely to populate PyPI.
