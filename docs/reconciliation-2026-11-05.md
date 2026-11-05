# Branch reconciliation, 2026-11-05

This integrates `private-internet-hardening` at `0eb8042` and `main` at `c9a8b44`
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

## Uncommitted work on worms-dev

A read-only snapshot was taken at main `c9a8b44`. The private patch and its hash
are kept outside this repository. The node was not changed.

The pending feature patch adds round-independent devbox progression, log records
and ZIP/API downloads, and switches the worm entry point from `main.py` to
`main.sh`. It includes corresponding UI, protocol and test changes. These are
reviewed in a separate preview checkout rather than silently included here.

The node's hostname default and cve-search RAM override are local deployment
settings; they must not replace the repository defaults. Operator notes and the
empty untracked `tile` file are not part of the feature patch.

The next live test should use an agreed deployed revision of the reconciled code
plus whichever pending features Jonas intends to ship. PyPI currently seeds only
`tabulate`; selecting and filling the intended offline package set (including
`tqdm`) remains necessary before testing arbitrary pip installs.

## Review of the pending feature patch

The isolated preview passes 204 focused tests covering profile/progression views,
team-scoped API and ZIP access, uploads, the shell worm contract, agent command
construction and browser rendering. The browser-rendering checks use local
fixture responses; they are not a live Proxmox/sandbox rehearsal.

The patch applies to the reconciliation with one import conflict: removing the
old devbox log helper must retain the membership-locking imports used by SSH-key
registration. This was resolved in the preview only.

Before shipping it, agree the `main.sh` contract change. Previously accepted
archives with only `main.py` will need to be replaced or an explicit compatibility
policy chosen before they are run by the new launcher. Commit the intended
feature changes separately from the hostname/RAM overrides so the tested revision
can be identified and deployed without overwriting other work on the node.


## Latest-main update during validation

Jonas published the reviewed source patch as `abcde6a` and then `211d6ac` while
validation was running. Both official commits are now included. The separate
preview is retained as evidence of the review; no private snapshot files or
node-specific overrides were added to this branch.

A final read-only check of the node at `211d6ac` found only the hostname and
cve-search RAM overrides remaining tracked but uncommitted, plus `docs/IMPORTANT`
and the empty `tile`. Those remain untouched.

The combined browser rehearsal reproduced a lost-click bug: loading network
connections rebuilt the sidebar between pointer-down and pointer-up on a log
entry. The connections count now updates in place, preserving the event row.
The browser driver deliberately delivers that response during the click rather
than masking the failure with a retry or a longer timeout.
