# Private-internet live checks, 2026-11-05

## Scope and method

Tests ran on worms-dev with the internet window closed. The node checkout was
`a16f55c`; its hostname/RAM overrides and private files were left in place.
No dashboard/account/round workflow was exercised and no sandbox was created.
There were no sandbox VMs in the running inventory, so **sandbox coverage remains
outstanding**.

Client checks ran inside the existing `devbox-t1-a`, `devbox-t2-a` and
`devbox-t3-a`, one per team. Administrative SSH entered through compute2 and the
node's maintenance network; HTTP, DNS, NTP and package requests originated inside
the devboxes through their normal service-facing routes. Guest SSH identities
were checked against their existing on-disk public host keys. No private keys,
credentials or operator notes are included here.

## Results

| Service/check | Observed result |
|---|---|
| Internal DNS | All seven private-internet names resolve to the expected service addresses from all three teams. |
| APT metadata | An empty temporary lists directory fetched and verified all configured Debian trixie, updates and security indexes through apt-cacher-ng on all three teams (17.2 MB each). Later runs suffered resource failures; see below. |
| APT package installation | On team 3, a real installation fetched 2775 kB and installed the previously absent `python3-pip-whl` and `python3-setuptools-whl`. This comparison temporarily raised the RAM floor and disabled translation downloads; ordinary operations at the default memory floor are not a clean pass. |
| PyPI usable cached package | `pip install --no-cache-dir --target <temporary-dir> tabulate` downloads version 0.10.0 from the internal index on all three teams. Importing it and printing a table succeeds. Temporary installations are removed afterwards. |
| PyPI missing package | `pip install ... tqdm` fails with no matching distribution on all three teams; its simple-index endpoint returns 502. devpi has only one stored package file and runs in offline mode. This is a missing seed/catalog problem, not a client routing failure. |
| Docker registry | `/v2/` responds 200 on all three teams. The catalog is empty. `httpd:2.4.49` (the helper manifest's image) returns 404 for its tag/manifest from team 3. No Docker daemon installation or successful image pull is claimed. |
| Manpages | The homepage responds 200 on all three teams, but its own browse link and search form target return 403. The packaged `man2html.conf` has `Require local`. Apache also logs a CGI socket permission failure under the deployed capability set. |
| CVE-Search | Homepage responds 200 on all three teams. The correct `/api/dbinfo` endpoint responds 200 but reports zero records and null update dates for every listed source. A known CVE lookup returns 404. The data is not populated. |
| Wikipedia | The service name resolves, but port 8888 refuses connections on all three teams. `kiwix-serve` is inactive; no ZIM dataset exists and its startup condition skips the service. |
| NTP | Valid UDP/123 server responses from all three teams (mode 4, leap 0, stratum 10). The server uses its local reference and has no external sources; this verifies internal service, not externally calibrated time. No client clock was changed. |
| Port isolation | Tested TCP 22, 80, 443, 3141, 3142, 5000 and 8888 against each of the seven service addresses from each team. Only the five active expected TCP services accept connections; Wikipedia's expected port refuses and the other combinations time out. UDP NTP was checked separately. |
| Representative external connections | TCP to 1.1.1.1:443 and 130.149.7.7:53 times out from all three teams. Together with the closed internet flag this is supporting isolation evidence, not an exhaustive egress audit. |

## Resource failures and test side effects

Package probes initially used conservative time limits. Some APT planning/venv
commands hit those limits, so they were not counted as successes. Longer probes
also saw APT processes exit with SIGKILL while reading package lists. QMP showed
256 MiB ballooned allocations for team 2 and team 3 despite 512 MiB configured
maximums; a team-1 guest reported only about 249 MiB total RAM and no swap.

For an isolated comparison, team-3 VM 118's balloon floor was temporarily changed
from 256 to 512 MiB, within its existing maximum. The comparison also used `Acquire::Languages=none`, so it is not a one-variable proof of a RAM-only fix. A fresh index update (10.4 MB) and the real two-package install both exited 0. Guest kernel logs confirmed that earlier APT processes had been killed by the OOM killer. The balloon floor was restored to 256 MiB and checked from the node. The two newly added wheel packages were then removed with `dpkg --remove`; no pre-existing packages were removed.

A separate diagnostic mistake affected CVE-Search: launching `mongosh` on the
memory-constrained VM triggered the guest OOM killer, which killed `mongod`.
The shell client exited; MongoDB was started again, and both its active state
and a successful `/api/dbinfo` response were verified. It still reports empty
data, as it did before the incident. Avoid heavy shell/population operations
until this VM has an adequate guaranteed memory budget. A reported guest total
of about 455 MiB is substantially below its configured 1024 MiB maximum.

No internet access was enabled, service configuration was not redeployed, no
VM was reset/rebuilt, and no existing application data was deleted. Final checks on all three clients showed an empty `dpkg --audit`, no remaining
test temporary directories and no running `apt-get`. The CVE-Search API again
returned 200 from all three teams. The internet flag remained closed. New
uncommitted dashboard source work appeared on the node during testing; it was
left untouched and was not part of these private-internet checks.

## Follow-up

1. Give the intended client/package operations an adequate guaranteed RAM budget;
   recheck ordinary APT/venv installation at the final production settings.
2. Agree and populate the offline PyPI catalog; `tabulate` proves the path works
   but is not a useful general package catalog. `tqdm` is explicitly missing.
3. Fix the manpages client access rule and CGI capability/runtime problem; verify
   the real search form and rendered `ls(1)` page, not only the homepage.
4. Populate the intended registry image, CVE data and Wikipedia dataset if those
   services are required. Do this in the supported preparation window; do not
   open internet access around existing student devboxes or remove them merely
   to bypass the builder guard.
5. Repeat these client checks from an actual sandbox when one is available.

Raw command output and probe scripts are retained locally under
`worms-platform-e2e-state/private-internet-20261105/`; they are separate from the
previous isolated 807-test dashboard/platform run.
