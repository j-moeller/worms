# Manual run: from an empty database to a reset devbox

Every step of the devbox journey by hand, with the exact command or form input and what you should see. The automated version of the same journey is `tests/live` (`docs/testing.md`).

**Nothing here was executed while writing this document.** The commands come from the code and from the scripted runs on 2026-11-03 and 2026-11-04. Each step ends with **Last seen**, naming the run and stage of `docs/e2e-run-*/protocol.md` that saw that result. **Not tried yet** means the expectation comes from reading the code alone.

It takes about an hour, most of it waiting: teardown 5 minutes, build 17, key push 3, reset 20.

## Read this first

Four things work differently from what you might expect:

| You might expect | What the system does | So this run |
|---|---|---|
| Root creates the users, teams and admin | `worms` creates only the admin and staff accounts. No command creates a team, and `manage.py create_users` ignores the team column | Root creates the admin (step 2). The admin creates teams and users on the dashboard (steps 5 and 6) |
| auditd shows what happens on a devbox | auditd is not installed on devbox VMs, their gateways or the SSH gateway (`audit_hosts` in `ansible/inventory.yml`), and a devbox cannot reach the syslog VM, by design | Nothing typed on a devbox appears in auditd. Step 12c shows what auditd does hold, and step 12d where the devbox actions are recorded instead |
| The dashboard's SSH command works everywhere | It works from outside, but not on the node itself, because the port forward only matches outside traffic | SSH runs from the node, with the gateway's address in place of the public name. Step 10 also gives the command for your laptop |
| The database wipe comes first | A wipe leaves the VMs running. Building over leftover VMs after a wipe has never been tried, and 13 devbox VMs exist from the stopped suite run | The VMs go first (step 0), then the wipe (step 1) |

## Prepare your shell

**Who:** root on the node. Keep this shell open for the whole run.

```sh
cd /root/app

# The management CLI.
worms() { /root/app/.venv-test/bin/python /root/app/src/manage.py "$@"; }

# Run a command on a service VM, to read its logs.
vm() {
  host=$1; shift
  ssh -i /etc/worms/ssh/worms -o BatchMode=yes -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR debian@"$host" "$@"
}

# Read-only SQL, through the CLI's database connection. Run it from /root/app.
sql() {
  .venv-test/bin/python - "$1" <<'PY'
import sys
sys.path.insert(0, "src")
from worms_cli import django_setup
from worms_cli.config import Config
django_setup.setup(Config.load())
from django.db import connection, transaction
with transaction.atomic():
    cursor = connection.cursor()
    cursor.execute("SET TRANSACTION READ ONLY")
    cursor.execute(sys.argv[1])
    print(" | ".join(column.name for column in cursor.description))
    for row in cursor.fetchall():
        print(" | ".join(str(value) for value in row))
PY
}
```

`worms` and `sql` can print `[PRODUCTION] We are running in PRODUCTION mode` first. That line is expected.

### Names and addresses

The dashboard is `https://worms-dev.mlsec.tu-berlin.de/` here, and `https://worms.mlsec.tu-berlin.de/` in production. Browsers warn about its certificate because it comes from the internal CA (`docs/check-with-user.md`, 4).

| Team | Index on Staff → Teams | A, through the gateway | B, from A | C, from B | Address seen by services |
|---|---|---|---|---|---|
| `alpha` | `#0` | `10.7.0.11` | `10.128.0.12` | `10.128.1.13` | `10.2.0.32` |
| `bravo` | `#1` | `10.7.1.11` | `10.128.2.12` | `10.128.3.13` | `10.2.0.33` |
| `charlie` | `#2` | `10.7.2.11` | `10.128.4.12` | `10.128.5.13` | `10.2.0.34` |

If a team has another index `n`: A is `10.7.n.11`, B is `10.128.(2n).12`, C is `10.128.(2n+1).13`, and the services see `10.2.0.(32+n)`.

The SSH gateway for teams is `10.7.0.2`.

| Service VM | Address for `vm` |
|---|---|
| scoring | `10.5.0.14` |
| logging | `10.5.0.15` |
| syslog | `10.5.0.30` |
| apt cache | `10.5.0.20` |
| devpi | `10.5.0.21` |
| SSH gateway | `10.5.0.40` |

---

## Step 0: remove the devbox VMs

**Who:** root on the node.

```sh
# Nothing may be queued or running.
sql "SELECT kind, state FROM host_jobs WHERE state IN ('queued', 'running')"

/usr/local/sbin/worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve -parallelism=1'

# Both must print nothing.
qm list | grep devbox
for id in $(zfs list -H -o name -t volume -r rpool | grep -oE 'vm-[0-9]+-' | cut -d- -f2 | sort -un); do
  qm status "$id" >/dev/null 2>&1 || echo "leftover volumes of VM $id"
done

worms status
```

**Expect:**
- No job rows from the first command.
- `Destroy complete! Resources: 108 destroyed.`, for 3 teams, after about 5 minutes.
- `devbox VMs ○ none` and `internet access ● denied` in `worms status`.

**If a volume is left over,** find its name with `zfs list -t volume -r rpool | grep vm-<id>-`, then remove it: for `rpool/vm-118-cloudinit`, run `pvesm free local-zfs-sandbox:vm-118-cloudinit`. Otherwise a new VM given that ID fails with `dataset already exists`.

**Last seen:** teardown and volume check on 2026-11-04, 16:16.

## Step 1: clear all data from the database

**Who:** root on the node.

```sh
worms reset-database --yes
worms users
worms teams
```

**Expect:**
- `truncated <n> table(s)`, `admin: created admin ... login disabled until worms admin reset-password`, `competition: created 'WORMS <year>'` and `database reset complete`.
- `worms users` lists only `admin`.
- `worms teams` prints `No teams (or no current competition).`

This deletes teams, users, captures, logs, jobs and sessions. It does not touch VMs or tables' structure.

**Last seen:** the suite's first run on 2026-11-04, step 00.

## Step 2: create the admin account

**Who:** root on the node.

```sh
worms admin reset-password
```

**Expect:** the last line printed is a 20-character one-time password. It is shown once and stored only as a hash. If you lose it, run the command again.

**Last seen:** the suite's first run on 2026-11-04, step 01.

## Step 3: the admin signs in

**Who:** admin, in a browser.

| Page | Field | Input |
|---|---|---|
| `https://worms-dev.mlsec.tu-berlin.de/login/` | Username | `admin` |
| | Password | the password from step 2 |
| | | Press **Sign in** |
| `/change-password/` (you are sent here) | both password fields | a new password of your choice |
| | | Press **Save password** |

**Expect:** the page `/profile/` and the message `Password saved.` Every other page sends you back to `/change-password/` until you have saved it.

The new password needs at least 8 characters, not all digits, not a common one, and not similar to your account's name.

**Note:** the login page allows 10 attempts a minute per address. After that, wait a minute.

**Last seen:** 2026-11-04, Stage 1.

## Step 4: create the development competition

**Who:** admin, in a browser.

| Page | Input |
|---|---|
| `/staff/`, panel **Development competition** | Press **Create it** |

The page calls this optional. Every earlier run did it, so this one does too.

**Last seen:** 2026-11-04, Stage 1.

## Step 5: create the teams

**Who:** admin, in a browser.

On `/staff/teams/`, panel **New team**, three times:

| Name | Members at most | |
|---|---|---|
| `alpha` | `3` | Press **Create team** |
| `bravo` | `3` | Press **Create team** |
| `charlie` | `3` | Press **Create team** |

**Expect:** `Team alpha created. Build its devbox from the console: worms devbox provision.`, and the same for the others. A warning that the teams have no devbox yet is correct at this point.

**Last seen:** 2026-11-04, Stage 1.

## Step 6: create the users

**Who:** admin, in a browser.

On `/staff/users/`, panel **Import from a CSV**, paste this into **…or paste the CSV** and press **Import**:

```csv
Vorname,Nachname,E-Mail,Team
Anna,Alpha,anna.alpha@example.org,alpha
Arne,Alpha,arne.alpha@example.org,alpha
Aylin,Alpha,aylin.alpha@example.org,alpha
Ben,Bravo,ben.bravo@example.org,bravo
Bea,Bravo,bea.bravo@example.org,bravo
Bo,Bravo,bo.bravo@example.org,bravo
Carl,Charlie,carl.charlie@example.org,charlie
Cem,Charlie,cem.charlie@example.org,charlie
Cleo,Charlie,cleo.charlie@example.org,charlie
```

**Expect:** the page **Accounts imported**, with 9 accounts and their one-time passwords, in a table and again under **As CSV**. **Copy them now: they are not shown again.** If you lose one, open that user on `/staff/users/` and use **New one-time password**.

To add one person instead, use the panel **New account**: **Email (the username)**, **First name**, **Surname**, **Team**, then **Create account**.

**Check, as root on the node:**

```sh
worms teams     # each team at 3/3
worms users     # 9 users marked "one-time pw", and admin
```

**Last seen:** 2026-11-04, Stage 1, with 1 account by the form and 8 by CSV.

## Step 7: build the devbox VMs

**Who:** root on the node.

```sh
worms devbox provision
```

**Expect:** `enqueued devbox_provision host job <uuid>`. The round agent then builds the VMs. Watch it with any of these:

```sh
tail -F /var/log/worms/jobs/latest.log   # the job's full output; Ctrl-C to leave
sql "SELECT left(uuid::text, 8) AS job, kind, state, started_at, finished_at, left(error, 100) AS error FROM host_jobs ORDER BY requested_at"
qm list | grep -c devbox
```

**Finished when:** the job's state is `done`, after about 17 minutes, and `qm list | grep -c devbox` prints `13`: the SSH gateway, 3 team gateways and 9 VMs. `/staff/teams/` then shows `#0`, `#1` and `#2` in the **Devbox** column.

**If the job fails with `got timeout`,** run `worms devbox provision` again. That is the disk timeout F5: it hit 4 of 7 builds before the TRIM on 2026-11-04, and none since, though only one build has run since.

**Last seen:** the suite's first run on 2026-11-04: done on the first attempt, in 17 minutes.

## Step 8: a user signs in

**Who:** Anna, in a private browser window, so the admin stays signed in elsewhere.

| Page | Field | Input |
|---|---|---|
| `/login/` | Username | `anna.alpha@example.org` |
| | Password | Anna's one-time password from step 6 |
| | | Press **Sign in** |
| `/change-password/` (you are sent here) | both password fields | a new password of your choice |
| | | Press **Save password** |

**Expect:** `/profile/` and `Password saved.` The one-time password is refused from now on.

**Last seen:** 2026-11-04, Stage 3.

## Step 9: add an SSH key

**Part 1. Who:** root on the node. The key lives here because SSH runs from here (see "Read this first").

```sh
ssh-keygen -t ed25519 -N '' -C anna-laptop -f ~/.ssh/worms-anna
cat ~/.ssh/worms-anna.pub
```

A key's comment may not contain spaces, or the dashboard refuses the key.

**Part 2. Who:** Anna, in the browser.

| Page | Field | Input |
|---|---|---|
| Top bar | | Switch the toggle to **Dev**, then open `/` |
| `/`, panel **SSH keys** | Label | `anna-laptop` |
| | Public key | the one line printed by `cat` above |
| | | Press **Add key** |

**Expect:**
- `SSH key added. Your devbox and the SSH gateway are being updated now.`
- `1 registered` on the panel.
- The panel **Connect** shows `ssh -J team@worms-dev.mlsec.tu-berlin.de:2222 root@10.7.0.11`. Use this from outside. On the node, use the gateway's address, as in step 10.

**Part 3. Who:** root on the node. Wait for the key to arrive, about 3 minutes:

```sh
sql "SELECT left(uuid::text, 8) AS job, kind, state, finished_at FROM host_jobs WHERE kind = 'devbox_reconfigure' ORDER BY requested_at"
```

**Finished when:** the last row is `done`.

**Last seen:** 2026-11-04, Stage 3.

## Step 10: log into the devbox through the gateway

**Who:** root on the node, acting as Anna.

```sh
# A config file, not -i: only the file also reaches the ssh that -J starts.
cat > ~/.ssh/worms-anna.cfg <<'EOF'
Host *
  IdentityFile ~/.ssh/worms-anna
  IdentitiesOnly yes
EOF

# The key in an agent, forwarded so you can go on from A to B and C.
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/worms-anna

# Forget host keys from earlier VMs at these addresses.
ssh-keygen -R 10.7.0.2
ssh-keygen -R 10.7.0.11

ssh -F ~/.ssh/worms-anna.cfg -A -J team@10.7.0.2 root@10.7.0.11
```

Answer `yes` to the two host key questions, one for the gateway and one for A.

**Expect:** a root shell on `devbox-t1-a`.

**To reach B and C,** from that shell:

```sh
ssh -A root@10.128.0.12      # now on devbox-t1-b
ssh root@10.128.1.13         # now on devbox-t1-c
```

**From your laptop instead,** generate the key there and add it in step 9, because a key belongs to one team and the node's copy stays on the node. Then run the command the dashboard shows:

```sh
ssh-add ~/.ssh/worms-anna
ssh -A -J team@worms-dev.mlsec.tu-berlin.de:2222 root@10.7.0.11
```

**Not tried yet:** port 2222 was opened after the earlier runs, and the node cannot test it from inside. If the connection times out rather than being refused, the network in front of the node may be blocking the port.

**Optional: what the gateway refuses.** Run these from the node:

```sh
ssh -F ~/.ssh/worms-anna.cfg -J team@10.7.0.2 root@10.7.1.11 hostname     # bravo's A: stdio forwarding failed
ssh -F ~/.ssh/worms-anna.cfg -J team@10.7.0.2 root@10.128.0.12 hostname   # her own B, directly: stdio forwarding failed
ssh -F ~/.ssh/worms-anna.cfg team@10.7.0.2 id                             # This account is currently not available.
```

**Last seen:** 2026-11-04, Stages 4 and 5, with the same routes. **Not tried yet** in this exact form: those runs skipped the host key questions and used a separate known-hosts setting.

## Step 11: commands on the devbox

**Who:** Anna, on the devbox, as root. Run everything on A. Run 11a and 11f on B and C too, so that all three tokens count.

### 11a. Note the starting state

```sh
hostname; id -un; uptime -s
sha256sum /etc/ssh/ssh_host_ed25519_key.pub /etc/worms/flag
```

**Save this output**: step 14 compares against it. The hash keeps the token itself out of your notes.

### 11b. Create a file

```sh
echo manual-run > /root/manual-run.txt
cat /root/manual-run.txt
```

**Expect:** `manual-run`.

### 11c. apt, through the private internet

```sh
grep -h '^URIs' /etc/apt/sources.list.d/*.sources | sort -u
command -v jq || echo absent
apt-get update
apt-get install -y --no-install-recommends jq
jq --version
```

**Expect:** source `http://10.2.0.20:3142/deb.debian.org/debian`, `absent`, then a clean install and `jq-1.7`.

### 11d. pip, through the private internet

```sh
pip3 config get global.index-url
python3 -c 'import tabulate' 2>/dev/null && echo present || echo absent
pip3 install --no-cache-dir tabulate
python3 -c 'import tabulate; print(tabulate.__version__)'
```

**Expect:** an address on `pypi.worms.internal`, `absent`, a clean install with no special flags, and `0.10.0`.

**Last seen, 11a to 11d:** 2026-11-04, Stage 5.

### 11e. Other private internet services

```sh
code() { printf '%-10s ' "$1"; curl -sS -o /dev/null --max-time 12 -w 'http=%{http_code}\n' "$2"; }
code devpi     http://pypi.worms.internal:3141/root/pypi/+simple/
code registry  http://docker-registry.worms.internal:5000/v2/
code manpages  http://manpages.worms.internal/
code cve       http://cve-search.worms.internal:5000/
code wikipedia http://wikipedia.worms.internal:8888/

python3 - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(5)
s.sendto(b'\x1b' + 47 * b'\0', ('ntp.worms.internal', 123))
print('ntp stratum', s.recvfrom(48)[0][1])
PY

# Not held by the private internet, so these should fail cleanly.
apt-get install -y cowsay; echo "apt exit $?"
pip3 install six; echo "pip exit $?"
```

**Expect:**
- `http=200` from devpi, the registry, man pages and cve-search.
- No answer from Wikipedia, which holds no content yet (`docs/pi-failures.md`).
- `ntp stratum 10`.
- `apt exit 100`, and `pip exit 1` with `No matching distribution found`.

**Last seen:** the service checks on 2026-11-03 only, and `cowsay` and `six` on 2026-11-04, Stage 5.

### 11f. Scoring and logging

```sh
curl -sS -o /dev/null -w 'http=%{http_code} tls_verify=%{ssl_verify_result}\n' \
  -X POST https://scoring.worms.internal/submit -H 'Content-Type: application/json' \
  -d "{\"vm_flag\":\"$(tr -d '\n' </etc/worms/flag)\"}"

curl -sS -o /dev/null -w 'http=%{http_code} tls_verify=%{ssl_verify_result}\n' \
  -X POST https://logging.worms.internal/submit -H 'Content-Type: application/json' \
  -d "{\"log_data\":{\"marker\":\"manual-run\",\"vm\":\"$(hostname)\"}}"
```

**Expect:** `http=201 tls_verify=0` from both.

### 11g. The real internet stays out of reach

```sh
for url in https://deb.debian.org/ https://pypi.org/simple/ http://1.1.1.1/; do
  curl -sS -o /dev/null --max-time 6 "$url" 2>/dev/null && echo "OPEN $url" || echo "closed $url"
done
```

**Expect:** `closed` three times. An `OPEN` here is a serious fault: `docs/IMPORTANT`.

**Last seen, 11f and 11g:** 2026-11-04, Stage 5.

## Step 12: observe the traces

This is where each action should leave a trace:

| Action | Dashboard | Database | auditd | Recorded elsewhere |
|---|---|---|---|---|
| SSH login | No | No | No | SSH gateway's journal, and the devbox's own |
| Creating a file | No | No | No | Only on the VM |
| `apt` | No | No | No | apt cache's log, and the devbox's `/var/log/apt/history.log` |
| `pip` | No | No | No | devpi's journal |
| `curl` to scoring | Token `found` | `flag_capture_attempts`, `devbox_captures` | No | |
| `curl` to logging | Log entry | `logging_attempts`, `devbox_logs` | No | |
| Other network traffic | No | No: devbox bridges are not mirrored, by design | No | |
| Adding a key, build, reset | Key list, version | `devbox_ssh_keys`, `host_jobs`, `teams.devbox_version` | Yes, on scoring and logging: see 12c | Round agent's journal |

### 12a. The dashboard

**Who:** Anna, in the browser. Open `/` in the **Dev** view and reload.

**Expect:**
- **Tokens found:** `3/3` at `version 1`, or `1/3` if you ran 11f only on A.
- **VMs:** `found` on each VM that submitted.
- **Logs:** one entry per VM. Open an entry to see `marker: manual-run` and the VM's name.
- Only scoring and logging appear here. SSH, the file, apt and pip do not, as intended.

Records reach the page through a background processor. The suite allows up to 5 minutes.

**Last seen:** 2026-11-04, Stage 6.

### 12b. The database

**Who:** root on the node, in `/root/app`.

```sh
# Teams: version, index, keys, VMs. Expect alpha | 1 | 0 | 1 | 3
sql "SELECT t.name, t.devbox_version, d.index, (SELECT count(*) FROM devbox_ssh_keys k WHERE k.team_id = t.id) AS keys, (SELECT count(*) FROM devbox_vms v WHERE v.devbox_id = d.id) AS vms FROM teams t LEFT JOIN devboxes d ON d.team_id = t.id ORDER BY t.name"

# Scoring: one row per VM that submitted, at version 1.
sql "SELECT t.name, v.vm_identifier, c.devbox_version, c.created_at FROM devbox_captures c JOIN teams t ON t.id = c.team_id JOIN devbox_vms v ON v.id = c.devbox_vm_id ORDER BY c.created_at"

# Logging: one row per entry, with what was sent.
sql "SELECT t.name, l.devbox_version, l.created_at, a.log_data FROM devbox_logs l JOIN teams t ON t.id = l.team_id JOIN logging_attempts a ON a.id = l.logging_attempt_id ORDER BY l.created_at"

# The raw attempts, all marked devbox and processed. Expect true | true | <count>
sql "SELECT devbox, processed, count(*) FROM flag_capture_attempts GROUP BY 1, 2"
sql "SELECT devbox, processed, count(*) FROM logging_attempts GROUP BY 1, 2"

# The build and the key pushes.
sql "SELECT left(uuid::text, 8) AS job, kind, state, started_at, finished_at FROM host_jobs ORDER BY requested_at"

# Network traffic on devbox addresses. Expect 0, by design (docs/requirements.md, Monitor).
sql "SELECT count(*) FROM network_flows WHERE src_ip::inet <<= ANY (ARRAY['10.128.0.0/19','10.7.0.0/16','10.6.0.0/16','10.2.0.32/27']::inet[]) OR dst_ip::inet <<= ANY (ARRAY['10.128.0.0/19','10.7.0.0/16','10.6.0.0/16','10.2.0.32/27']::inet[])"

# Zeek is alive, so that 0 means something. Expect a time under 15 minutes old.
sql "SELECT max(ts), now() FROM network_events WHERE log = 'stats'"

# Each VM's token, stored only as a hash. Save this for step 14.
sql "SELECT vm_identifier, proxmox_vm_id, left(ctf_flag_hash, 12) AS token_hash FROM devbox_vms ORDER BY 1"
```

**Last seen:** the same tables and results on 2026-11-04, Stage 6. **Not tried yet** in this form: the joined queries and the `sql` helper are written for this document.

### 12c. auditd

**Who:** root on the node.

**What it records:** auditd runs on the infrastructure and private internet VMs. Its rules (`ansible/roles/audit/auditd/templates/worms.rules.j2`) record every program started, under the key `exec`, and changes to accounts, SSH and service configuration. Each VM keeps `/var/log/audit/audit.log` and ships it to the syslog VM, in `/var/log/remote/<sender's address>/audit.log`.

**What to expect from this run:**
- **Nothing from the devbox itself**, because it has no auditd and cannot reach the syslog VM.
- **No record of the `curl` calls.** scoring and logging start no program per request, and their nginx access log is off. The database row is the record.
- **Probably no record of `apt` and `pip`** on the apt cache and devpi. Their own logs are the place to look (12d).
- **The agent's writes on scoring and logging.** Each build, key push and reset runs `sudo install ... /etc/sandbox-api/devbox-teams` there, so those are the events to look for.

```sh
# 1. Which VMs report. Expect no address from 10.6.0.0/16, 10.7.0.0/16 or 10.128.0.0/19, and not 10.5.0.40.
vm 10.5.0.30 'sudo ls -l /var/log/remote'

# 2. The agent's writes, on the VMs themselves. Expect one group of events per build and key push.
vm 10.5.0.14 'sudo ausearch -k exec -i -ts today 2>/dev/null | grep -a devbox-teams | tail -6'
vm 10.5.0.15 'sudo ausearch -k exec -i -ts today 2>/dev/null | grep -a devbox-teams | tail -6'

# 3. The same in the central copy. Take the scoring VM's directory from the listing in 1.
#    It should be 10.12.0.14. Search one file only: searching them all has hung before.
vm 10.5.0.30 'sudo timeout 60 grep -a devbox-teams /var/log/remote/10.12.0.14/audit.log | tail -6'

# 4. Which programs ran on scoring today, as a summary.
vm 10.5.0.14 'sudo aureport -x --summary -ts today 2>/dev/null | head -20'

# 5. The last 10 minutes on scoring, right after running 11f.
vm 10.5.0.14 'sudo ausearch -k exec -i -ts recent 2>/dev/null | tail -20'
```

**Reading the output:**
- Times are UTC on the VMs, so one hour behind the node's CET.
- Your own `vm` calls are recorded too: expect `sshd`, `sudo`, `ausearch` and `aureport` in the results, especially in 5.
- In 5, expect only those commands and whatever timers ran, and nothing tied to your `curl`.

**Not tried yet:** no run has looked for these events. On 2026-11-04 the only audit check was that `/var/log/remote` holds infrastructure VMs only. Please note what you actually see here: it decides whether this section is right.

### 12d. Where the devbox actions are recorded instead

**Who:** root on the node, except the last block.

```sh
# Each jump through the gateway, with the key's fingerprint. Compare with: ssh-keygen -lf ~/.ssh/worms-anna.pub
vm 10.5.0.40 'sudo journalctl -u ssh --since "-60 min" --no-pager | grep -a Accepted | tail -5'

# apt: each package request, by team address (10.2.0.32 is alpha).
vm 10.5.0.20 'sudo grep -a jq /var/log/apt-cacher-ng/apt-cacher.log | tail -5'

# pip: each request's path, naming no client.
vm 10.5.0.21 'sudo journalctl -u devpi --since "-60 min" --no-pager | grep -a tabulate | tail -5'
```

On the devbox itself. A reset removes these:

```sh
journalctl -u ssh --since "-60 min" --no-pager | grep -a Accepted | tail -5
tail -n 8 /var/log/apt/history.log
```

**Note:** the gateway's journal lives only on that VM, so a teardown loses it. A created file leaves no trace outside the VM.

**Last seen:** the same sources on 2026-11-04, "Traces outside the database". **Not tried yet** with these exact filters.

## Step 13: reset the devbox

**Who:** Anna, in the browser. Any member of the team can do this.

| Page | Input |
|---|---|
| `/`, in the **Dev** view, panel **Tokens found** | Press **Reset devbox** |
| The confirmation that opens below it | Press **Yes, rebuild my devbox** |

**Expect at once:**
- `Reset requested: your devbox VMs are being rebuilt and the score starts over.`
- `version 2` and `0/3`.
- `No devbox logs at this version.`

**Who:** root on the node. Wait for the rebuild, about 20 minutes:

```sh
tail -F /var/log/worms/jobs/latest.log   # the job's full output; Ctrl-C to leave
sql "SELECT left(uuid::text, 8) AS job, kind, state, started_at, finished_at, left(error, 100) AS error FROM host_jobs WHERE kind = 'devbox_rebuild'"
qm list | grep devbox-t1
```

**Finished when:** the job is `done`. SSH to the team's VMs fails until then. Only `alpha`'s three VMs are replaced, and they keep their VM IDs.

**Last seen:** 2026-11-04, Stage 7: done in 19.5 minutes, on the first try.

## Step 14: confirm that the devbox was reset

### 14a. Log in again, with the same key

**Who:** root on the node, in the same shell as step 10, so the agent still holds the key.

```sh
ssh-keygen -R 10.7.0.11
ssh -F ~/.ssh/worms-anna.cfg -A -J team@10.7.0.2 root@10.7.0.11
```

Answer `yes` to the host key question. Without `ssh-keygen -R`, SSH stops with `REMOTE HOST IDENTIFICATION HAS CHANGED`: A really is a new VM, with a new key. The gateway has not changed, so it asks nothing.

**Expect:** a root shell, with no need to add the key again.

### 14b. Everything you changed is gone

**Who:** Anna, on the devbox. Run on A, then on B and C.

```sh
hostname; uptime -s
sha256sum /etc/ssh/ssh_host_ed25519_key.pub /etc/worms/flag
ls -l /root/manual-run.txt
command -v jq || echo absent
python3 -c 'import tabulate' 2>/dev/null && echo present || echo absent
```

| Check | Expected, compared with 11a |
|---|---|
| `hostname` | The same |
| `uptime -s` | Later |
| Hash of the host key | Different |
| Hash of the token | Different |
| `/root/manual-run.txt` | `No such file or directory` |
| `jq` | `absent` |
| `tabulate` | `absent` |

### 14c. The dashboard

**Who:** Anna, in the browser. Reload `/` in the **Dev** view.

**Expect:** `version 2`, `0/3`, every VM `not found`, `No devbox logs at this version.`, the key still listed, and the same SSH command.

### 14d. The database

**Who:** root on the node.

```sh
# alpha at version 2, bravo and charlie still at 1.
sql "SELECT name, devbox_version FROM teams ORDER BY name"

# The earlier records are kept at version 1, with none at version 2.
sql "SELECT t.name, c.devbox_version, count(*) FROM devbox_captures c JOIN teams t ON t.id = c.team_id GROUP BY 1, 2 ORDER BY 1, 2"
sql "SELECT t.name, l.devbox_version, count(*) FROM devbox_logs l JOIN teams t ON t.id = l.team_id GROUP BY 1, 2 ORDER BY 1, 2"

# New token hashes for alpha, unlike those saved in 12b, and the same VM IDs. Each matches
# the first 12 characters of sha256sum /etc/worms/flag on that VM, from 14b.
sql "SELECT vm_identifier, proxmox_vm_id, left(ctf_flag_hash, 12) AS token_hash FROM devbox_vms ORDER BY 1"

sql "SELECT left(uuid::text, 8) AS job, kind, state, started_at, finished_at FROM host_jobs WHERE kind = 'devbox_rebuild'"
```

**Not tried yet:** comparing the hash with the file by hand. The rest was seen on 2026-11-04, Stage 8.

### 14e. auditd

**Who:** root on the node.

```sh
vm 10.5.0.14 'sudo ausearch -k exec -i -ts today 2>/dev/null | grep -a devbox-teams | tail -3'
```

**Expect:** a new write at the end of the rebuild. Remember the times are UTC. **Not tried yet.**

### 14f. Optional: the new VM works, and other teams are untouched

**The new VM works.** Repeat 11c, 11d and 11f on A, B and C.

**Expect:** apt, pip and both `curl` calls work again. The page shows `3/3` at `version 2`, and the database holds records at both versions.

**Other teams are untouched.** This needs `bravo` to have done steps 8 to 11 before the reset, as Ben with his own key. A key can belong to only one team, so Ben needs a new one, added to bravo's page. Use `10.7.1.11` as his entry VM.

**Expect:** Ben's file, `jq` and `tabulate` still there, the same boot time and hashes, and `3/3` at `version 1`.

**Last seen:** 2026-11-04, Stage 8.

## Afterwards

**Who:** root on the node.

```sh
ssh-agent -k
rm -f ~/.ssh/worms-anna ~/.ssh/worms-anna.pub ~/.ssh/worms-anna.cfg
```

To run it all again, start at step 0. Until you do, the VMs, the test accounts and the teams stay in place.

## If something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| Build job fails with `got timeout` | Disk timeout, F5 | `worms devbox provision` again |
| Build job fails with `dataset already exists` | A volume left by an earlier VM | Step 0, the volume check |
| A job failed, and its `error` does not say why | Ansible reports failed tasks on stdout | Read the job's log: the `error` names the file, and `/var/log/worms/jobs/latest.log` is the most recent |
| `Permission denied (publickey)` | The key has not arrived, or SSH offered another key | Check the `devbox_reconfigure` job, and that you used `-F ~/.ssh/worms-anna.cfg` |
| `stdio forwarding failed` | The gateway allows only your team's A | Check the address against the table above |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | The VM was rebuilt | `ssh-keygen -R <address>` |
| Dashboard answers `400` | It was opened by IP address | Use the name: only the public name is accepted |
| The login page refuses every attempt | Rate limit, 10 sign-ins a minute per address | Wait a minute |
| Score or logs do not change | The background processor has not reached them yet | Wait up to 5 minutes, then check `processed` in 12b |
