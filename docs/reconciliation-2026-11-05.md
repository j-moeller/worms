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
