"""`worms check [<team>...]`: can each team work on its devbox? Every check does what the
team or its worm does, end to end, and changes nothing there:

    login     the SSH gateway takes the team's registered keys (as user `team`) and forwards the team
              to its VM A; A, B and C take the keys as root
    flag      /etc/worms/flag is there and is FLAG{<32 hex>} (never printed)
    scoring   POST https://<service>.worms.internal/submit with `{}`, as a worm posts: a 400 means the
    logging   service was reached over TLS verified with the internal CA, and it knows the team by
              its gateway's address (403: it does not). An empty object is no submission: nothing
              is stored, nothing scores
    apt       a fresh package index through the apt cache, as a VM just reset reads it, into a
              directory of its own (the VM's lists stay as they are), then one package (APT_PACKAGE)
    PyPI      one package (PYPI_PACKAGE) from the devpi mirror, through the VM's own pip.conf

A login is asked as an SSH client asks before it signs, with the public key alone (RFC 4252, 7:
sshd says whether it would take the key). No private key is needed, nobody logs in, and the round
agent records no team login (it counts "Accepted publickey" only). The gateway is asked from the
node, on its management address; A, B and C from the gateway, where a team's connection comes from.
One connection to each sshd, every key in it.

The SSH gateway's sshd sees every team as one address: the node masquerades them (host_network,
worms.nft: "present teams as the host"; inventory host_ips.devbox_ssh). With OpenSSH's per-source
penalties (on by default since 9.8), a few failed logins by anyone shut every team out, for up to
10 minutes. So this reads whether they are off there or that address is exempt, and how often the
gateway refused a connection for a penalty in the last 24 h: a risk while it refused none, a
problem once it has.

The VMs are assumed rooted by their team: of what they return only the known answers are read, and
shown escaped; a team can only misreport its own devbox.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from worms_cli import output
from worms_cli.build import BuildError
from worms_cli.keys import GATEWAY, GATEWAY_FILE, KeyCheck, canonical
from worms_cli.remote import Remote, Target, escape
from worms_cli.teams import Team, find, vm_name

ROLES = ("a", "b", "c")
CHECKS = ("login", "flag", "scoring", "logging", "apt", "PyPI")
VM_CHECKS = CHECKS[1:]
APT_PACKAGE = "jq"  # small, and in the apt cache's list since its first fill (apt_cache_seed_packages)
PYPI_PACKAGE = "requests"  # in devpi's since its first (devpi_seed_packages); --no-deps: one wheel
GATEWAY_USER = "team"  # role ssh_gateway, ssh_gateway_user
MAX_BYTES = 256 * 1024

# sh, on stdin: one SSH connection per `probe`, every key offered, public halves only. sshd answers
# each "accepts" or not (ssh -v: "Server accepts key: <file>"); the client has no private key to
# sign with and gives up. No agent, no ssh_config, nobody's known_hosts.
PROBE = r"""set -u
d=$(mktemp -d) || exit 1
trap 'rm -rf "$d"' EXIT
@KEYS@
probe() {  # <tag> <user@address> <key file>...
    tag=$1 dest=$2
    shift 2
    set -- $(for k in "$@"; do printf ' -o IdentityFile=%s/%s' "$d" "$k"; done)
    timeout 30 ssh -F /dev/null -v -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none \
        -o PreferredAuthentications=publickey -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o GlobalKnownHostsFile=/dev/null -o ConnectTimeout=8 -o ConnectionAttempts=1 -o ControlPath=none \
        "$@" "$dest" true </dev/null >/dev/null 2>"$d/$tag.log"
    {
        if grep -q 'Remote protocol version' "$d/$tag.log"; then echo "@reached $tag"
        else echo "@error $tag $(grep -v '^debug' "$d/$tag.log" | tail -n 1 | tr -cd '[:print:]' | cut -c1-160)"; fi
        sed -n "s|^debug1: Server accepts key: $d/\(k[0-9]*\) .*|@accepts $tag \1|p" "$d/$tag.log"
    } >"$d/$tag.out"
}
@PROBES@
wait
cat "$d"/*.out 2>/dev/null
"""

# sh, on the SSH gateway as root, after its probes: OpenSSH's per-source penalties, and the
# connections it refused for one in the last 24 h (sshd logs "drop connection ... penalty: ...").
PENALTIES = r"""settings=$(sshd -T 2>/dev/null)
echo "@penalties $(printf '%s\n' "$settings" | sed -n 's/^persourcepenalties //p')"
echo "@exempt $(printf '%s\n' "$settings" | sed -n 's/^persourcepenaltyexemptlist //p')"
drops=$(journalctl -u ssh --since -24h --no-pager -o short-iso 2>/dev/null | grep 'drop connection.*penalty')
echo "@drops $(printf '%s' "$drops" | grep -c .) $(printf '%s\n' "$drops" | tail -n 1 | cut -d' ' -f1)"
"""

# sh, on a devbox VM as root: one `@check <name> ok|warn|fail <why>` line per check.
CHECK_SCRIPT = r"""set -u
say() { printf '@check %s %s %s\n' "$1" "$2" "$(printf '%s' "$3" | tr -c '[:print:]' ' ' | cut -c1-220)"; }
why() { grep -m 1 -E '^E:' "$1" || grep -m 1 -E '^(Err|W):' "$1" || tail -n 1 "$1"; }
d=$(mktemp -d /var/tmp/worms-validate.XXXXXX) || { say apt fail "no temporary directory in /var/tmp"; exit 0; }
trap 'rm -rf "$d"' EXIT

if [ ! -e /etc/worms/flag ]; then say flag fail "no /etc/worms/flag"
elif grep -qxE 'FLAG\{[0-9a-f]{32}\}' /etc/worms/flag; then say flag ok ""
else say flag fail "/etc/worms/flag is not FLAG{<32 hex>}"; fi

for api in scoring logging; do
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 -X POST -H 'Content-Type: application/json' \
           -d '{}' "https://$api.worms.internal/submit" 2>"$d/curl")
    case "$?:$code" in
        0:400) say "$api" ok "" ;;
        0:429) say "$api" warn "429: reached, but the team is over its rate limit right now" ;;
        0:403) say "$api" fail "403: the service does not know this team's gateway address (its devbox map)" ;;
        0:*) say "$api" fail "HTTP $code" ;;
        *) say "$api" fail "$(tail -n 1 "$d/curl")" ;;
    esac
done

mkdir "$d/lists" "$d/lists/partial" "$d/deb" "$d/pip"
set -- -o Dir::State::Lists="$d/lists" -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache= -o APT::Sandbox::User=root \
       -o Acquire::http::Timeout=10 -o Acquire::https::Timeout=10 -o Acquire::Retries=0
if ! timeout 150 apt-get update --error-on=any "$@" >"$d/apt.log" 2>&1; then
    say apt fail "$(why "$d/apt.log")"
elif ! (cd "$d/deb" && timeout 60 apt-get download "$@" @APT@) >"$d/apt.log" 2>&1; then
    say apt fail "the index, but not the package @APT@: $(why "$d/apt.log")"
else say apt ok ""; fi

if ! python3 -m pip --version >/dev/null 2>&1; then
    say PyPI fail "no pip (python3-pip is not installed: the devbox is not fully built)"
elif timeout 90 python3 -m pip download --no-deps --no-cache-dir --disable-pip-version-check --timeout 10 --retries 0 \
        -d "$d/pip" @PYPI@ >"$d/pip.log" 2>&1; then
    say PyPI ok ""
else
    index=$(python3 -m pip config get global.index-url 2>/dev/null || echo "none set (no /etc/pip.conf)")
    say PyPI fail "index $index: $(grep -m 1 '^ERROR' "$d/pip.log" || tail -n 1 "$d/pip.log")"
fi
"""

CHECK_LINE = re.compile(r"@check (flag|scoring|logging|apt|PyPI) (ok|warn|fail) ?(.*)")
STATE = {"ok": "ok", "warn": "warn", "fail": "error"}


@dataclass
class Cell:
    state: str  # an output.STATUS_STYLE key: ok, warn, error, stopped, unknown, n/a
    text: str
    why: str = ""  # said under the table

    def render(self) -> str:
        return output.status_cell(self.state, self.text)


@dataclass
class Probes:
    """What `PROBE` printed: the sshds that answered, the keys each takes, why one did not answer."""

    reached: set
    accepts: dict
    errors: dict
    penalties: str | None = None
    exempt: str = ""
    drops: tuple[int, str] = (0, "")


def probe_script(keys: list[str], probes: list[tuple[str, str, list[int]]], extra: str = "") -> str:
    """`PROBE` for these keys (canonical, `type blob`: safe words, quoted all the same) and probes
    (tag, user@address, the keys' numbers), run at once; `extra`: sh after them."""
    files = "\n".join(f"printf '%s\\n' {shlex.quote(key)} > \"$d/k{i}.pub\"" for i, key in enumerate(keys, 1))
    lines = "\n".join(f"probe {shlex.quote(tag)} {shlex.quote(dest)} {' '.join(f'k{i}' for i in numbers)} &"
                      for tag, dest, numbers in probes)  # fmt: skip
    return PROBE.replace("@KEYS@", files).replace("@PROBES@", lines + ("\n" + extra if extra else ""))


def parse_probes(text: str) -> Probes:
    found = Probes(set(), {}, {})
    for line in text.splitlines():
        word, _, rest = line.partition(" ")
        if word == "@reached":
            found.reached.add(rest.strip())
        elif word == "@accepts":
            tag, _, key = rest.partition(" ")
            if key.startswith("k") and key[1:].isdigit():
                found.accepts.setdefault(tag, set()).add(int(key[1:]))
        elif word == "@error":
            tag, _, why = rest.partition(" ")
            found.errors[tag] = escape(why.strip())[:160] or "no answer"
        elif word == "@penalties":
            found.penalties = rest.strip()
        elif word == "@exempt":
            found.exempt = rest.strip()
        elif word == "@drops":
            count, _, last = rest.partition(" ")
            found.drops = (int(count) if count.isdigit() else 0, escape(last.strip())[:40])
    return found


def parse_checks(text: str) -> dict[str, Cell]:
    cells = {}
    for line in text.splitlines():
        match = CHECK_LINE.fullmatch(line.strip())
        if match and match.group(1) not in cells:
            name, state, why = match.groups()
            label = {"ok": "✓", "warn": "warn", "fail": "fail"}[state]  # the dot is ✗ already
            cells[name] = Cell(STATE[state], label, escape(why)[:220] if state != "ok" else "")
    return cells


def exempted(address: str | None, exempt: str) -> bool:
    """Whether sshd's PerSourcePenaltyExemptList (addresses and networks, by commas) holds `address`."""
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    for entry in exempt.split(","):
        try:
            if ip in ipaddress.ip_network(entry.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def permitted(gateway_text: str) -> dict[str, set[str]]:
    """{team slug: the addresses its lines on the SSH gateway may forward to (permitopen)}."""
    found: dict[str, set[str]] = {}
    for line in gateway_text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith("restrict") and parts[-1].startswith("team="):
            found.setdefault(parts[-1][len("team="):], set()).update(re.findall(r'permitopen="([^"]+)"', parts[0]))
    return found


class Validator:
    def __init__(self, config, catalogue=None):
        from worms_cli.vms import Catalogue

        self.config = config
        self.catalogue = catalogue or Catalogue(config)
        self.remote = Remote(config, self.catalogue)

    def run(self, target: Target, script: str, root: bool, timeout: int) -> tuple[int, str]:
        """(exit code, stdout) of `script`, given to sh on its stdin, on `target`; (255, why) when it could not run."""
        try:
            argv, env = self.remote.argv(target, "sh -s", root)
            proc = subprocess.run(argv, env=env, input=script.encode(), capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, f"no answer in {timeout} s"
        except Exception as exc:  # noqa: BLE001  (no address in the inventories, the node out of reach)
            return 255, str(exc)
        out = proc.stdout[:MAX_BYTES].decode(errors="replace")
        if proc.returncode == 255 and not out.strip():
            lines = proc.stderr[-4096:].decode(errors="replace").strip().splitlines()
            return 255, lines[-1] if lines else "ssh failed"
        return proc.returncode, out

    def teams(self, names) -> list[tuple[Team, list[str], int]]:
        """[(team, its keys (canonical), keys that are no SSH public key)] of the current competition
        with a devbox: those `names` name, or all."""
        from worms_cli.keys import _teams

        rows = _teams()
        known = {slug: Team(slug, name, has) for name, slug, _, has in rows}
        texts = {slug: keys for _, slug, keys, _ in rows}
        if names:
            chosen = []
            for text in names:
                team = find(known, text)
                if team is None:
                    raise BuildError(f"no team {text!r} in the current competition (`worms show devboxes`)")
                if not team.devbox:
                    raise BuildError(f"{team.label} has no devbox")
                chosen.append(team)
        else:
            chosen = [t for t in known.values() if t.devbox]
        out = []
        for team in dict.fromkeys(chosen):
            keys = [k for k in map(canonical, texts[team.slug]) if k]
            out.append((team, list(dict.fromkeys(keys)), len(texts[team.slug]) - len(keys)))
        return out

    def validate(self, names) -> int:
        chosen = self.teams(names)
        if not chosen:
            print("No team with a devbox (or no current competition).")
            return 0
        vms = {vm["name"]: vm for vm in self.catalogue.vms()}
        hosts = self.catalogue.hosts()

        def running(name: str) -> bool:
            return vms.get(name, {}).get("status") == "running"

        # Every key once, numbered: k<i> in the probes.
        keys: list[str] = []
        numbers: dict[str, list[int]] = {}
        for team, team_keys, _ in chosen:
            for key in team_keys:
                if key not in keys:
                    keys.append(key)
                numbers.setdefault(team.slug, []).append(keys.index(key) + 1)
        fabric = {}
        for team, _, _ in chosen:
            for role in ROLES:
                try:
                    fabric[(team.slug, role)] = str(ipaddress.ip_address(hosts.get(vm_name(team.slug, role), {}).get("devbox_ssh_ip")))
                except ValueError:
                    pass
        gateway_vm = vms.get(GATEWAY)
        gateway_address = hosts.get(GATEWAY, {}).get("ansible_host")
        at_vms = [(t.slug, role) for t, k, _ in chosen if k for role in ROLES if running(vm_name(t.slug, role)) and (t.slug, role) in fabric]

        jobs = {}
        if running(GATEWAY):
            if keys and gateway_address:
                jobs["node"] = (Target("node", None), probe_script(keys, [("gateway", f"{GATEWAY_USER}@{gateway_address}", list(range(1, len(keys) + 1)))]), False, 60)
            probes = [(f"{slug}-{role}", f"root@{fabric[(slug, role)]}", numbers[slug]) for slug, role in at_vms]
            jobs["gateway"] = (Target(GATEWAY, gateway_vm), probe_script(keys, probes, PENALTIES), True, 90)
        for team, _, _ in chosen:
            for role in ROLES:
                name = vm_name(team.slug, role)
                if running(name):
                    jobs[name] = (Target(name, vms[name], True), CHECK_SCRIPT.replace("@APT@", APT_PACKAGE).replace("@PYPI@", PYPI_PACKAGE), True, 300)

        print(f"validating the devboxes of {len(chosen)} team(s): {', '.join(CHECKS)} on A, B and C; the SSH gateway "
              "(a minute or two) ...", flush=True)  # fmt: skip
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = {name: pool.submit(self.run, *job) for name, job in jobs.items()}
            gateway_file = pool.submit(KeyCheck(self.config, self.catalogue).read, gateway_vm, GATEWAY_FILE)
            results = {name: future.result() for name, future in futures.items()}
            gateway_text, gateway_problem = gateway_file.result()

        node = parse_probes(results["node"][1]) if "node" in results else None
        if node is not None and "gateway" not in node.reached:
            node.errors.setdefault("gateway", escape(results["node"][1].strip())[:160] or "no answer")
        gateway = parse_probes(results["gateway"][1]) if "gateway" in results else None
        failed = self._gateway(gateway_vm, results.get("gateway"), gateway, node)
        targets = permitted(gateway_text) if gateway_text is not None else None
        for team, team_keys, invalid in chosen:
            cells = self._team(team, team_keys, invalid, vms, fabric, numbers.get(team.slug, []), results, node, gateway, targets, gateway_problem)
            failed += self._print(team, team_keys, cells, vms)
        stopped = [n for t, _, _ in chosen for n in (vm_name(t.slug, r) for r in ROLES) if n in vms and not running(n)]
        if stopped:
            print(output.color(f"stopped, not checked: {', '.join(stopped)}", "dim"))
        if failed:
            print(output.status_cell("error", f"{failed} problem(s), each said above"))
            return 1
        print(output.status_cell("ok", "every checked devbox works as its team needs it"))
        return 0

    def _gateway(self, vm, result, gateway: Probes | None, node: Probes | None) -> int:
        """The SSH gateway's own lines; the number of problems."""
        print(output.heading(f"SSH gateway ({GATEWAY})"))
        if vm is None or vm.get("status") != "running":
            state = "no such VM" if vm is None else vm.get("status")
            print(output.status_cell("error", f"{state}: no team can log in (`worms start ssh-gateway`)"))
            return 1
        problems = 0
        if node is not None and "gateway" not in node.reached:
            print(output.status_cell("error", f"sshd does not answer the node: {node.errors.get('gateway', 'no answer')}"))
            problems += 1
        if gateway is None or not gateway.penalties:
            why = escape(result[1].strip())[:160] if result and result[0] else "sshd -T said nothing"
            print(output.status_cell("unknown", f"could not read its sshd's settings: {why}"))
            return problems + 1
        count, last = gateway.drops
        refused = f"; it refused {count} connection(s) for a penalty in the last 24 h, the last at {last}" if count else ""
        address = self.teams_address()
        if gateway.penalties == "no":
            print(output.status_cell("ok", "OpenSSH's per-source penalties are off" + refused))
        elif exempted(address, gateway.exempt):
            print(output.status_cell("ok", f"OpenSSH's per-source penalties spare the teams' address {address}" + refused))
        else:
            # A risk until it has refused someone; then the teams felt it.
            print(output.status_cell("error" if count else "warn",
                                     f"OpenSSH's per-source penalties are on, and every team comes from one address, the "
                                     f"node's ({address or 'host_ips.devbox_ssh'}): a few failed logins by anyone shut all "
                                     f"teams out, for up to 10 minutes{refused}. Off: `PerSourcePenalties no`, or "
                                     f"`PerSourcePenaltyExemptList {address or '<it>'}`, in its sshd"))  # fmt: skip
            problems += bool(count)
        return problems

    def teams_address(self) -> str | None:
        """The address the SSH gateway sees every team come from: the node's on the SSH fabric
        (inventory: host_ips.devbox_ssh), from the inventory the VM catalogue reads first."""
        import yaml

        local = self.config.repo_root / "ansible" / "inventory.generated.yml"
        if local.exists():
            text = local.read_text()
        else:
            code, text, _ = self.catalogue.runner.session.node_run(
                ["sh", "-c", 'cat "$(sed -n "s/^REPO_ROOT=//p" /etc/worms/round-agent.env)/ansible/inventory.generated.yml"'])
            if code != 0:
                return None
        try:
            value = (((yaml.safe_load(text) or {}).get("all") or {}).get("vars") or {}).get("host_ips", {}).get("devbox_ssh")
            return str(ipaddress.ip_address(value))
        except (yaml.YAMLError, AttributeError, ValueError):
            return None

    def _team(self, team, team_keys, invalid, vms, fabric, mine, results, node, gateway, targets, gateway_problem) -> dict:
        """{(check, role): Cell} of one team."""
        cells: dict[tuple[str, str], Cell] = {}
        n = len(team_keys)
        for role in ROLES:
            name = vm_name(team.slug, role)
            vm = vms.get(name)
            if vm is None:
                for check in CHECKS:
                    cells[(check, role)] = Cell("n/a", "—")
                continue
            if vm.get("status") != "running":
                for check in CHECKS:
                    cells[(check, role)] = Cell("stopped", "off")
                continue
            code, out = results.get(name, (255, "not run"))
            found = parse_checks(out)
            # A VM the checks could not run on: said once, under its first check.
            why = (f"could not run the checks: {escape(out.strip())[:160]}" if not found and code else
                   f"no answer (the checks stopped, exit {code})")  # fmt: skip
            for check in VM_CHECKS:
                cells[(check, role)] = found.get(check) or Cell("unknown", "unknown", why)
                if not found:
                    why = ""
            cells[("login", role)] = self._login(team, role, n, mine, fabric, node, gateway, targets, gateway_problem, vms)
        if invalid:
            cell = cells.get(("login", "a"))
            if cell is not None:
                cell.why = "; ".join(x for x in (cell.why, f"{invalid} of the team's keys are no SSH public key: left out") if x)
        return cells

    def _login(self, team, role, n, mine, fabric, node, gateway, targets, gateway_problem, vms) -> Cell:
        if not n:
            return Cell("error", "no key", "the team has no SSH key registered (its profile on the dashboard)") if role == "a" else Cell("n/a", "—")
        tag = f"{team.slug}-{role}"
        if (team.slug, role) not in fabric:
            return Cell("unknown", "unknown", f"no devbox_ssh_ip for {vm_name(team.slug, role)} in the inventories (`worms step inventory`)")
        if gateway is None:
            return Cell("unknown", "unknown", "the SSH gateway is not running: not asked")
        if tag not in gateway.reached:
            return Cell("error", "no answer", f"sshd does not answer the SSH gateway: {gateway.errors.get(tag, 'no answer')}")
        taken = gateway.accepts.get(tag, set()) & set(mine)
        whys, sync = [], False  # sync: what `worms sync keys` puts right
        if len(taken) < n:
            whys.append(f"takes {len(taken)} of the team's {n} key(s) as root")
            sync = True
        if role == "a":
            # The team's way in: the gateway takes the key and forwards to A, and A takes it too.
            if node is None or "gateway" not in node.reached:
                whys.append("the SSH gateway was not asked (no answer to the node)")
                taken = set()
            else:
                at_gateway = node.accepts.get("gateway", set()) & set(mine)
                if len(at_gateway) < n:
                    whys.append(f"the SSH gateway takes {len(at_gateway)} of them")
                    sync = True
                taken &= at_gateway
            want = f"{fabric[(team.slug, 'a')]}:22"
            if targets is None:
                whys.append(f"the SSH gateway's key file could not be read ({gateway_problem})")
                taken = set()
            elif targets.get(team.slug, set()) != {want}:
                there = ", ".join(sorted(targets.get(team.slug, set()))) or "nowhere"
                whys.append(f"the SSH gateway forwards the team to {there}, not to A ({want})")
                sync, taken = True, set()
        text = f"{len(taken)}/{n}"
        if len(taken) == n and not whys:
            return Cell("ok", f"✓ {text}")
        return Cell("error", text, "; ".join(whys) + (" (`worms sync keys`)" if sync else ""))

    def _print(self, team, team_keys, cells, vms) -> int:
        """One team's table and what is wrong; the number of problems."""
        print()
        rows = [[check] + [cells[(check, role)].render() for role in ROLES] for check in CHECKS]
        print(output.table(rows, headers=[f"{team.label}, {len(team_keys)} key(s)", "A", "B", "C"]))
        problems = 0  # the lines that say one: a cell without a line is explained by another
        for check in CHECKS:
            for role in ROLES:
                cell = cells[(check, role)]
                if cell.why:
                    problems += cell.state in ("error", "unknown")
                    style = "red" if cell.state == "error" else "yellow"
                    print(output.color(f"  {role.upper()} {check}: {cell.why}", style))
        nat = vms.get(vm_name(team.slug, "gw"))
        if nat is not None and nat.get("status") != "running":
            print(output.color(f"  its NAT gateway {nat['name']} is {nat.get('status')}: its VMs reach no service", "red"))
            problems += 1
        return problems


def devbox(config, names) -> int:
    """`worms check [<team>...]`."""
    return Validator(config).validate(names)
