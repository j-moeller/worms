"""The check job's first part, for every VM of the run's sandbox and for its gateway, sandbox-gw:
did it start in this attempt, and did it come up as it should? Before the probes (probe.py), so
before the worm: the VMs are still ours, and what they answer can be trusted.

Started is the host's view (handlers.check, _started): the VM is in the pool `sandbox` under its
name, Proxmox says it runs and so does its QEMU (not paused, not halted), and that QEMU started
after this attempt's prepare began (host_state.qemu_started, on the kernel's clock prepare noted).
A VM left running from before the rollback would still hold the last run's state: it fails.

Initialised is the VM's view, one SSH each, all at once: INIT, on probe.PRELUDE, as root. It
changes nothing. It first waits, up to WAIT seconds, for cloud-init to be done and for systemd to
have finished booting; then each NIC has its address (by its MAC, which the host's config gives),
the default route goes where the stack says (sandbox-gw has none), sshd listens on the VM's SSH
port, and the gameplan's users and services are there and running. On sandbox-gw also its nftables
chains, as its cloud-init defines them, and its sysctl settings (IP forwarding, nf_conntrack_max),
both read from the cloud-init it was built with: its snippet on the host, which its Proxmox config
names (`cicustom`), rendered from opentofu/stacks/sandbox/templates/sandbox-gw.tftpl by the apply
that built it. Not the template in the workspace: a deploy during a round replaces that, and the
round's gateway, rolled back to its snapshot, keeps what it was built with.

What a real VM of the image answers (Debian 13, cloud-init 25.1, systemd 257; read on the
platform's dns VM, which boots the same image, 2026-11-22): `cloud-init status --format json`
{"status": "done", "extended_status": "done", "errors": [], "recoverable_errors": {}, ...},
exit 0, 2 with recoverable errors (status still "done"), 1 on an error (status "error"; each
error of a module "('<module>', <exception>)"); `systemctl is-system-running --wait` "running";
`systemctl list-jobs --no-legend --plain` "<job> <unit> <type> <state>" per job, nothing when
there is none; `ip -j addr show` each link's "address" (its MAC, lower case; `-4` leaves it out)
and "addr_info" [{family, local, prefixlen}]; `ip -j -4 route show default` [{dst, gateway,
dev}], "[]" without one; sshd's listening process is `sshd`.

As in probe.py, the job's result (the database) names none of the gameplan's content: no
address, user, unit or port of it, only counts and the platform's own units and chains. The job
log on the host has the rest.
"""

from __future__ import annotations

import ipaddress
import re

from round_agent.probe import PRELUDE, _pair, _text

WAIT = 90  # seconds, on the VM: how long cloud-init and systemd may still take to finish the boot
DEADLINE = 110  # seconds, on the VM; the SSH command gets half a minute more
SSH_TIMEOUT = DEADLINE + 30
# sandbox-gw's cloud-init snippet in its Proxmox config: `cicustom: user=local:snippets/sandbox-gw.yml`.
SNIPPET = re.compile(r"^cicustom:.*?\buser=([^,\s]+)", re.M)
# What of the gateway's sysctl file the check reads back: the rest are defence in depth.
GATEWAY_SYSCTLS = ("net.ipv4.ip_forward", "net.netfilter.nf_conntrack_max")
# Of those, the ones whose other value is reported, not failed (capacity, not the routing itself).
SOFT_SYSCTLS = ("net.netfilter.nf_conntrack_max",)
# The units of the image a VM's start rests on: one of them failed, or still starting after the
# wait, fails the boot. Any other (systemd says `degraded`, or still `starting`) is counted in the
# record and named in the job log only. The time service: systemd-timesyncd on team VMs and
# sandbox-gw (role ntp_client), chrony where a gameplan installs it.
PLATFORM_UNITS = (
    "ssh.service", "sshd.service", "systemd-timesyncd.service", "chrony.service", "systemd-networkd.service",
    "networking.service", "cloud-init-local.service", "cloud-init-main.service", "cloud-init-network.service",
    "cloud-init.service", "cloud-config.service", "cloud-final.service",
)  # fmt: skip
# A module's error in `cloud-init status`: "('scripts_user', RuntimeError('Runparts: ...'))".
CLOUD_INIT_MODULE = re.compile(r"^\('([\w.-]+)',")
# sandbox-gw's own (its cloud-init): its rules, and its route to the LLM endpoint.
GATEWAY_UNITS = ("nftables.service", "worms-llm-route.service")
UNIT_TYPES = {"service", "socket", "target", "timer", "path", "mount", "automount", "swap", "slice", "scope", "device"}
# A NIC in a VM's Proxmox config: `net0: virtio=BC:24:11:79:87:95,bridge=vmbr23,firewall=1`.
NIC = re.compile(r"^net(\d+):.*?\b((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\b")

INIT = PRELUDE + r'''

SYSTEM_STATES = ("initializing", "starting", "running", "degraded", "maintenance", "stopping", "offline", "unknown")


def first_json(text):
    """The first JSON value in a command's output (what it says on stderr comes with it)."""
    decoder = json.JSONDecoder()
    for i, c in enumerate(text):
        if c in "[{":
            try:
                return decoder.raw_decode(text, i)[0]
            except ValueError:
                pass
    raise ValueError("no JSON in its output: " + " ".join(text.split())[:120])


def cloud_init():
    # A look first: `cloud-init status` takes two seconds even when it is done, which is not waiting.
    # One too slow to answer the look is waited for like one still running, within the same wait.
    begun, late, waited, status = time.time(), False, 0.0, "running"
    try:
        code, output = command(["cloud-init", "status", "--format", "json"], 30)
        status = first_json(output).get("status")
    except TimeoutError:
        pass
    if status in ("running", "not started"):
        try:
            code, output = command(["cloud-init", "status", "--wait", "--format", "json"], max(1, P["wait"] - (time.time() - begun)))
        except TimeoutError:
            late = True
            code, output = command(["cloud-init", "status", "--format", "json"], 30)
        waited = time.time() - begun
    data = first_json(output)
    errors = data.get("errors") if isinstance(data.get("errors"), list) else []
    recoverable = data.get("recoverable_errors") if isinstance(data.get("recoverable_errors"), dict) else {}
    put("cloud-init", {
        "code": code, "status": str(data.get("status")), "extended": str(data.get("extended_status")),
        "errors": [str(e)[:160] for e in errors][:3],
        "recoverable": {str(k): len(v) if isinstance(v, list) else 1 for k, v in recoverable.items()},
        "stage": data.get("stage") if isinstance(data.get("stage"), str) else None,
        "waited": round(waited, 1), "late": late,
    })


def boot():
    begun, late, state = time.time(), False, None
    try:
        words = command(["systemctl", "is-system-running", "--wait"], P["wait"])[1].split()
        state = words[-1] if words and words[-1] in SYSTEM_STATES else None
    except TimeoutError:
        late = True
    shown = command(["systemctl", "show", "-p", "SystemState", "-p", "NJobs"], 15)[1]
    props = dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)
    listed = command(["systemctl", "list-units", "--state=failed", "--all", "--plain", "--no-legend", "-o", "json"], 15)[1]
    try:
        failed = [str(unit.get("unit")) for unit in first_json(listed) if isinstance(unit, dict)]
    except (ValueError, TypeError):
        failed = [line.split()[0] for line in listed.splitlines() if line.split()]
    state = state or props.get("SystemState") or "unknown"
    pending = None
    if state in ("initializing", "starting"):  # still booting after the wait: the jobs it has left, by unit
        code, jobs = command(["systemctl", "list-jobs", "--no-legend", "--plain"], 15)
        if code == 0:
            pending = sorted({line.split()[1] for line in jobs.splitlines() if len(line.split()) > 1 and line.split()[0].isdigit()})
    put("boot", {
        "state": state, "failed": sorted(failed)[:200], "pending": pending[:200] if pending is not None else None,
        "jobs": int(props["NJobs"]) if props.get("NJobs", "").isdigit() else None,
        "waited": round(time.time() - begun, 1), "late": late,
    })


def has(link, want):
    """Whether a link (`ip -j addr show`) has this IPv4 address/prefix (or this address, any prefix)."""
    have = ["%s/%s" % (a.get("local"), a.get("prefixlen")) for a in link.get("addr_info") or [] if a.get("family") == "inet"]
    return (want in have if "/" in want else want in [h.split("/")[0] for h in have]), have


def addresses():
    # Not `ip -4`: it leaves out each link's MAC address.
    links = first_json(command(["ip", "-j", "addr", "show"], 15)[1])
    by_mac = {}  # a bridge, a bond or a VLAN on a NIC may carry its MAC too
    for link in links:
        if isinstance(link, dict):
            by_mac.setdefault(str(link.get("address", "")).lower(), []).append(link)
    found = {}
    for key, mac, want in P["nics"]:
        candidates = by_mac.get(mac.lower(), []) if mac else []
        if not candidates:
            found[key] = "no interface has its MAC address (%s)" % (mac or "none in its Proxmox config")
            continue
        if any(has(link, want)[0] and "UP" in (link.get("flags") or []) for link in candidates):
            found[key] = ""
            continue
        # What is wrong, of the one with the address, else of the NIC itself (no master, no lower link).
        link = next((c for c in candidates if has(c, want)[0]), None) or next(
            (c for c in candidates if not c.get("master") and not c.get("link")), candidates[0])
        ok, have = has(link, want)
        problems = [] if ok else ["%s has %s, not %s" % (link.get("ifname"), ", ".join(have) or "no IPv4 address", want)]
        if "UP" not in (link.get("flags") or []):
            problems.append("%s is down" % link.get("ifname"))
        found[key] = "; ".join(problems)
    put("addresses", found)


def route():
    routes = [r for r in first_json(command(["ip", "-j", "-4", "route", "show", "default"], 15)[1]) if isinstance(r, dict)]
    seen = ["via %s dev %s" % (r.get("gateway", "no gateway"), r.get("dev")) for r in routes]
    ok = {r.get("gateway") for r in routes} == {P["gateway"]} if P["gateway"] else not routes
    put("route", [ok, "; ".join(seen) or "no default route"])


def listening(port):
    """The sockets that listen on this TCP port (any address) and the names of the processes that hold them."""
    sockets = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as fh:
                rows = fh.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            f = row.split()
            if len(f) > 9 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                sockets.add("socket:[%s]" % f[9])
    owners = set()
    for pid in [p for p in os.listdir("/proc") if p.isdigit()] if sockets else []:
        try:
            fds = os.listdir("/proc/%s/fd" % pid)
        except OSError:
            continue
        for fd in fds:
            try:
                held = os.readlink("/proc/%s/fd/%s" % (pid, fd)) in sockets
            except OSError:
                continue
            if held:
                try:
                    with open("/proc/%s/comm" % pid) as fh:
                        owners.add(fh.read().strip())
                except OSError:
                    owners.add("?")
                break
    return sockets, owners


def sshd():
    sockets, owners = listening(P["ssh_port"])
    # systemd: an sshd per connection (ssh.socket)
    put("sshd", [bool(owners & {"sshd", "systemd"}), ", ".join(sorted(owners)) or ("held by no process" if sockets else "nothing listens")])


def users():
    found = {}
    for user in P["users"]:
        try:
            pwd.getpwnam(user)
            found[user] = True
        except KeyError:
            found[user] = False
    put("users", found)


def services():
    found = {}
    for unit in P["services"]:
        found[unit] = command(["systemctl", "is-active", unit], 15)[1].strip() or "unknown"
    put("services", found)


def nftables():
    code, output = command(["nft", "-j", "list", "chains"], 15)
    if code != 0:
        return put("nftables", [False, " ".join(output.split())[:160] or "exit %d" % code])
    chains = {}
    for item in first_json(output).get("nftables") or []:
        chain = item.get("chain") if isinstance(item, dict) else None
        if isinstance(chain, dict):
            chains["%s %s %s" % (chain.get("family"), chain.get("table"), chain.get("name"))] = chain
    found = {}
    for family, table, name, hook, policy in P["chains"]:
        key = "%s %s %s" % (family, table, name)
        chain = chains.get(key)
        if chain is None:
            found[key] = "missing"
        elif chain.get("hook") != hook:
            found[key] = "hook %s, not %s" % (chain.get("hook"), hook)
        elif policy and chain.get("policy") != policy:
            found[key] = "policy %s, not %s" % (chain.get("policy"), policy)
        else:
            found[key] = ""
    put("nftables", found)


def sysctl():
    found = {}
    for key in P["sysctl"]:
        try:
            with open("/proc/sys/" + key.replace(".", "/")) as fh:
                found[key] = fh.read().strip()
        except OSError:
            found[key] = None
    put("sysctl", found)


# The boot first, as long as it takes (up to the wait): the rest looks at a VM that is up.
waits = [threading.Thread(target=guarded, args=step, daemon=True) for step in (("cloud-init", cloud_init), ("boot", boot))]
for t in waits:
    t.start()
for t in waits:
    t.join()
steps = [("addresses", addresses), ("route", route), ("sshd", sshd), ("users", users), ("services", services)]
steps += [("nftables", nftables)] if P["chains"] else []
steps += [("sysctl", sysctl)] if P["sysctl"] else []
for step in steps:
    guarded(*step)
finish()
'''


# -- what is asked of each VM ----------------------------------------------------------------


def nic_macs(config):
    """{NIC number: MAC, lower case} from a VM's Proxmox config (`qm config`): the host's, which
    no guest can change."""
    out = {}
    for line in config.splitlines():
        match = NIC.match(line.strip())
        if match:
            out[int(match.group(1))] = match.group(2).lower()
    return out


def snippet(config):
    """The volume of the cloud-init snippet (user data) a VM's Proxmox config (`qm config`) names,
    such as `local:snippets/sandbox-gw.yml`, or None."""
    match = SNIPPET.search(config)
    return match.group(1) if match else None


def unit(name):
    """A unit's full name: systemctl takes `cron` for `cron.service`, its listings do not."""
    return name if "." in name and name.rsplit(".", 1)[1] in UNIT_TYPES else f"{name}.service"


def gateway_template(text):
    """What sandbox-gw's cloud-init sets that the check reads back: its nftables chains [(family,
    table, chain, hook, policy or None)] (each base chain of /etc/nftables.conf) and its sysctl
    settings {key: value} (GATEWAY_SYSCTLS of /etc/sysctl.d/90-sandbox-gw.conf). `text`: the
    snippet the sandbox stack rendered from sandbox-gw.tftpl, or the template itself (the same)."""
    chains, table, chain = [], None, None
    for line in text.splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        if words[0] == "table" and len(words) >= 3:
            table, chain = (words[1], words[2]), None
        elif words[0] == "chain" and len(words) >= 2 and table:
            chain = words[1]
        elif "hook" in words[:-1] and table and chain:
            policy = re.search(r"\bpolicy (\w+);", line)
            chains.append((*table, chain, words[words.index("hook") + 1], policy.group(1) if policy else None))
    sysctl = {key: value for key, value in re.findall(r"^\s*(net\.[\w.]+)\s*=\s*(\S+)\s*$", text, re.M) if key in GATEWAY_SYSCTLS}
    return chains, sysctl


def _cidr(address, network):
    """address/prefix when `network` holds the address, else the address alone (any prefix will do)."""
    if not address:
        raise ValueError("the sandbox stack gives no address")
    try:
        net = ipaddress.ip_network(network)
        if ipaddress.ip_address(address) in net:
            return f"{address}/{net.prefixlen}"
    except (TypeError, ValueError):
        pass
    return str(address)


def _params(cidrs, macs, gateway, ssh_port, users=(), services=(), chains=(), sysctl=()):
    return {
        "kind": "init", "deadline": DEADLINE, "wait": WAIT,
        "nics": [[f"net{i}", macs.get(i, ""), cidr] for i, cidr in enumerate(cidrs)],
        "gateway": gateway, "ssh_port": int(ssh_port), "users": list(users), "services": list(services),
        "chains": [list(c) for c in chains], "sysctl": list(sysctl),
    }  # fmt: skip


def plan_vm(vm, macs):
    """INIT's parameters for a VM of the gameplan (its spec), and the host's notes to read the
    answer by. Its NICs in the order the sandbox stack makes them (build.hcl, main.tf): one per
    network of the gameplan, then the management NIC. The default route: its first network's
    gateway. `macs`: nic_macs of its Proxmox config."""
    cidrs = [nic["address"] for nic in vm["nics"]] + [vm["maintenance"]]
    gateway = next((nic["gateway"] for nic in vm["nics"] if nic["gateway"]), None)
    params = _params(cidrs, macs, gateway, vm["ssh_port"], vm["users"], vm["services"])
    theirs = {unit(name) for name in vm["services"]}
    return params, {"router": False, "critical": set(PLATFORM_UNITS) | theirs, "theirs": theirs, "sysctl": {}}


def plan_gateway(gateway, macs, addressing, template):
    """INIT's parameters for sandbox-gw: `gateway` is the sandbox stack's output of it (sandbox.
    gateway: its sandbox legs, one per network, then its services and management addresses),
    `addressing` the inventory's (the prefixes of those two networks), `template` the cloud-init it
    was built with (gateway_template). No default route: it routes the sandbox to the services and
    nowhere else."""
    networks = addressing.get("networks") or {}
    cidrs = [leg["cidr"] for leg in gateway.get("sandbox_legs") or []]
    cidrs += [_cidr(gateway.get("services_ip"), networks.get("services")),
              _cidr(gateway.get("maintenance_ip"), networks.get("mgmt_sandbox"))]
    chains, sysctl = gateway_template(template)
    params = _params(cidrs, macs, None, 22, chains=chains, sysctl=sorted(sysctl))
    return params, {"router": True, "critical": set(PLATFORM_UNITS) | set(GATEWAY_UNITS), "theirs": set(), "sysctl": sysctl}


# -- what the answer says ----------------------------------------------------------------------


def _part(reply, key):
    """(the step's answer, or None; why there is none)."""
    value = reply.get(key)
    if isinstance(value, dict):
        return value, ""
    if isinstance(value, list) and len(value) == 2 and value[0] is False:  # the step failed on the VM
        return None, _text(value[1])
    return None, "no answer"


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _cloud_init(name, params, reply, note):
    value, why = _part(reply, "cloud-init")
    if value is None:
        return False, why
    status, waited = str(value.get("status")), value.get("waited") or 0
    after = f", after {waited:.0f} s" if isinstance(waited, (int, float)) and waited >= 2 else ""
    if value.get("late") and status in ("running", "not started"):
        return False, f"{status} after {params['wait']} s" + (f" (stage {_text(value['stage'], 40)})" if value.get("stage") else "")
    if status == "error" or value.get("code") == 1:
        # Which of cloud-init's modules failed; what they said (a script's or a file's name of the
        # gameplan, say) goes to the job log only.
        errors = [e for e in value.get("errors") or [] if isinstance(e, str)]
        for error in errors:
            note(f"check {name}: cloud-init: {_text(error)}")
        if not errors:
            return False, "error: " + _text(value.get("extended"), 60)
        modules = sorted({match.group(1) for match in map(CLOUD_INIT_MODULE.match, errors) if match})
        others = sum(1 for e in errors if not CLOUD_INIT_MODULE.match(e))
        what = [f"{', '.join(modules)} failed"] if modules else []
        what += [_plural(others, "other error" if modules else "error")] if others else []
        return False, "error: " + "; ".join(what) + " (the job log on the host says what)"
    if status == "done":
        recoverable = value.get("recoverable") if isinstance(value.get("recoverable"), dict) else {}
        counts = ", ".join(f"{n} {_text(level, 20)}" for level, n in sorted(recoverable.items()) if isinstance(n, int))
        return True, "done" + (f", with recoverable errors ({counts})" if counts else "") + after
    if status == "disabled":
        return True, "disabled: it did not run in this boot"
    return False, f"{_text(status, 40)} ({_text(value.get('extended'), 60)})"


def _boot(name, params, notes, reply, note):
    value, why = _part(reply, "boot")
    if value is None:
        return False, why
    state = _text(value.get("state"), 20)
    failed = [_text(u, 80) for u in value.get("failed") or [] if isinstance(u, str)]
    pending = value.get("pending")
    pending = [_text(u, 80) for u in pending if isinstance(u, str)] if isinstance(pending, list) else None
    for failed_unit in failed:
        note(f"check {name}: {failed_unit} failed")
    for job in pending or []:
        note(f"check {name}: {job} still starting after {params['wait']} s")
    waited = value.get("waited") or 0
    after = f", after {waited:.0f} s" if isinstance(waited, (int, float)) and waited >= 2 else ""
    if state == "running":
        return True, "running" + after
    if state != "degraded" and (state != "starting" or pending is None):
        if state in ("initializing", "starting"):
            jobs = value.get("jobs")
            return False, f"still {state} after {params['wait']} s" + (f" ({jobs} jobs to go)" if isinstance(jobs, int) else "")
        return False, f"systemd says {state}"

    # Degraded (a unit failed), or still starting after the wait (a job is left, a slow unit's): either
    # fails only by a unit the VM's start rests on. The platform's are named, the gameplan's counted.
    def split(units):
        bad = [u for u in units if u in notes["critical"]]
        named = [u for u in bad if u not in notes["theirs"]]
        return named, len(bad) - len(named), len(units) - len(bad)

    f_named, f_theirs, f_others = split(failed)
    p_named, p_theirs, p_others = split(pending or [])
    head = "degraded" if state == "degraded" else f"still starting after {params['wait']} s"
    if not (f_named or f_theirs or p_named or p_theirs):
        what = [f"{_plural(f_others, 'unit')} failed, none that its start rests on"] if state == "degraded" or failed else []
        what += [f"{_plural(p_others, 'job')} to go, none for a unit its start rests on"] if state == "starting" else []
        return True, f"{head}: " + "; ".join(what) + " (the job log on the host names them)" + after
    what = [f"{', '.join(f_named)} failed"] if f_named else []
    what += [f"{f_theirs} of the gameplan's services failed"] if f_theirs else []
    what += [f"{', '.join(p_named)} not started yet"] if p_named else []
    what += [f"{p_theirs} of the gameplan's services not started yet"] if p_theirs else []
    if state == "degraded":
        what += [f"{_plural(f_others, 'other unit')}"] if f_others else []
    else:
        what += [f"{_plural(f_others, 'other unit')} failed"] if f_others else []
        what += [f"{_plural(p_others, 'other job')} to go"] if p_others else []
    return False, f"{head}: " + "; ".join(what) + " (the job log on the host names them)"


def records(name, params, notes, reply, note, why=None):
    """The check's records of a VM's start and initialisation from INIT's answer ([(check, ok,
    detail)]): vm:<name>:init (it answered within its deadline), then vm:<name>:init:<what>.
    What they leave out (addresses, users, units, what the VM said of them) goes to `note`, the
    job log."""
    prefix = f"vm:{name}:init"
    if reply is None:
        note(f"check {name}: its start and initialisation check did not answer: {why}")
        return [(prefix, False, "no answer (the job log on the host says why)")]
    late = reply.get("deadline") is True
    out = [(prefix, not late, f"it ran out of time ({params['deadline']} s): what it had not done failed" if late else "")]
    out.append((f"{prefix}:cloud-init", *_cloud_init(name, params, reply, note)))
    out.append((f"{prefix}:boot", *_boot(name, params, notes, reply, note)))

    found, why = _part(reply, "addresses")
    found = found or {}
    wrong = []
    for key, mac, cidr in params["nics"]:
        problem = _text(found.get(key, why or "no answer"), 160)
        if problem:
            wrong.append(key)
            note(f"check {name}: {key} ({mac or 'no MAC'}): {cidr} expected: {problem}")
    out.append((f"{prefix}:addresses", not wrong, f"{', '.join(wrong)} not with the address its stack gives (the job log on the host "
                "says what it has)" if wrong else f"{len(params['nics'])} NICs, each with its address"))  # fmt: skip

    ok, seen = _pair(reply, "route")
    if not ok:
        note(f"check {name}: default route: {seen}; expected {'via ' + params['gateway'] if params['gateway'] else 'none'}")
    if notes["router"]:
        detail = "none, as its stack has it" if ok else "not as its stack has it: none (the job log on the host says what it has)"
    else:
        detail = "via its network's gateway" if ok else "not via its network's gateway (the job log on the host says what it has)"
    out.append((f"{prefix}:route", ok, detail))

    ok, who = _pair(reply, "sshd")
    port = "22" if notes["router"] else "its SSH port"
    if not ok:
        note(f"check {name}: port {params['ssh_port']}: {who}, not sshd")
    out.append((f"{prefix}:sshd", ok, f"sshd listens on {port}" if ok else f"sshd does not listen on {port} (the job log on the host "
                "says what does)"))  # fmt: skip

    if params["users"]:
        found = _part(reply, "users")[0] or {}
        missing = [user for user in params["users"] if found.get(user) is not True]
        for user in missing:
            note(f"check {name}: user {user}: {'missing' if found.get(user) is False else 'no answer'}")
        out.append((f"{prefix}:users", not missing, f"{len(missing)} of {len(params['users'])} missing (the job log on the host names "
                    "them)" if missing else f"{len(params['users'])} there"))  # fmt: skip
    if params["services"]:
        found = _part(reply, "services")[0] or {}
        down = [u for u in params["services"] if found.get(u) != "active"]
        for u in down:
            note(f"check {name}: service {u}: {_text(found.get(u, 'no answer'), 40)}")
        out.append((f"{prefix}:services", not down, f"{len(down)} of {len(params['services'])} not running (the job log on the host "
                    "names them)" if down else f"{len(params['services'])} running"))  # fmt: skip
    if params["chains"]:
        found, why = _part(reply, "nftables")
        keys = [" ".join(chain[:3]) for chain in params["chains"]]
        wrong = [f"{key}: {_text(found.get(key, 'no answer'), 40)}" for key in keys if found.get(key) != ""] if found is not None else []
        forward = next((c for c in params["chains"] if c[3] == "forward"), None)
        if found is None:
            detail = f"its chains not read: {why}"
        else:
            detail = "; ".join(wrong) if wrong else f"{len(keys)} chains as its cloud-init has them" + (
                f" (forward: policy {forward[4]})" if forward and forward[4] else "")
        out.append((f"{prefix}:nftables", found is not None and not wrong, detail))
    if params["sysctl"]:
        found, why = _part(reply, "sysctl")
        wrong = [f"{key} is {_text(found.get(key) or 'not set', 40)}, not {want}"
                 for key, want in sorted(notes["sysctl"].items()) if found.get(key) != want] if found is not None else []  # fmt: skip
        expected = ", ".join(f"{key} {want}" for key, want in sorted(notes["sysctl"].items()))
        detail = f"not read: {why}" if found is None else "; ".join(wrong) or expected
        # IP forwarding is the gateway's job: without it nothing reaches a service. The conntrack
        # limit is capacity, and no real sandbox-gw has shown it yet (2026-11-22): a different value
        # is said, not a failure, until a round has (SOFT_SYSCTLS).
        hard = [line for line in wrong if not line.startswith(SOFT_SYSCTLS)]
        if wrong and not hard:
            detail += " (a warning: the gateway still routes; under a busy worm its connections may run out)"
        out.append((f"{prefix}:sysctl", found is not None and not hard, detail))
    return out
