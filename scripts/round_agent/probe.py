"""The check job's look from inside every sandbox VM, before the worm: is the run's sandbox what
its gameplan says, and does everything a worm needs answer there? It runs once the VM has shown
that it started in this attempt and came up as it should (startup.py, whose script shares this
one's scaffolding, PRELUDE).

One SSH per VM, all VMs at once: PROBE runs as root (python3, which every image keeps), with
what it is to try as its first line. It changes nothing on the VM: apt's index goes to a
directory of its own, which it removes again; connections are opened and closed; nothing else is
written. Every step has a timeout, the whole of it a deadline, after which it reports what it has.

The VMs are trusted then (no team code has reached them yet), but the answer is read as any
other input: one line, JSON, only the fields asked for. What goes into the job's result (the
database, which the dashboard reads) names none of the gameplan's content: no path, user, unit
or rule, only VM names, numbers and the platform's own services. The job log on the host has
the rest.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import time

from gameplan.definition import FLAG_PATH, SERVICE_PORTS

DEADLINE = 150  # seconds, on the VM; the SSH command gets a minute more
APT_TIMEOUT = 90
TIME_WAIT = 45  # how long the VM's clock may take to report itself synchronised
CLOCK_TOLERANCE = 60  # seconds between the host's clock and the VM's (TOTP accepts minutes)
CONNECT_TIMEOUT = 4
MARK = "@worms-probe "
# A seeded package of the private PyPI (roles/private_internet/devpi, devpi_seed_packages).
PYPI_PACKAGE = "requests"
# The platform's names a worm uses (group_vars/dns.yml), and the inventory's address of each.
DNS_NAMES = {
    "scoring.worms.internal": ("services_ips", "scoring"),
    "logging.worms.internal": ("services_ips", "logging"),
    "apt-cache.worms.internal": ("private_internet_ips", "apt_cache", "network"),
    "pypi.worms.internal": ("private_internet_ips", "devpi", "network"),
}
# Where no sandbox VM may get to: (label, the inventory's address, port). Each port is one the
# destination answers on, so only the firewalls stand in the way.
FORBIDDEN = (
    ("dashboard", ("infrastructure_ips", "dashboard"), 443),
    ("database", ("infrastructure_ips", "database"), 5432),
    ("orchestrator", ("infrastructure_ips", "orchestrator"), 443),
    ("host", ("host_ips", "mgmt_sandbox"), 8006),  # the Proxmox API, on the bridge every sandbox VM is on
)
INTERNET = ("internet", "1.1.1.1", 80)  # 80: the LLM endpoint is reached on 443 only
BLOCKED = ("timeout", "unreachable")

# What every script run on a VM here starts with: its parameters (P), the answer it builds (put),
# commands with a timeout, and the deadline, at which it answers with what it has (finish).
PRELUDE = r'''
import base64, errno, http.client, json, os, pwd, shutil, signal, socket, ssl, stat, subprocess, sys, tempfile
import threading, time

P = json.loads(base64.b64decode(P))
out = {"started": time.time()}
lock = threading.Lock()
done = []  # set by finish: what a step writes from then on is dropped
temporary, children = [], []


def put(key, value):
    with lock:
        if not done:
            out[key] = value


def stop(child):
    try:
        os.killpg(child.pid, signal.SIGKILL)  # its own process group: apt's methods go with it
    except OSError:
        pass


def finish(*signal_args):
    # The answer is fixed first: a step whose command is killed below wakes up and would record
    # that as its own failure, so what the deadline cut off would not stay absent.
    signal.alarm(0)
    lock.acquire(timeout=2)
    done.append(True)
    if signal_args:
        out["deadline"] = True
    answer = "@worms-probe " + json.dumps(out) + "\n"
    # At the deadline as at the end: no command left running, nothing left behind. Only a group
    # whose leader has not been reaped: a reaped one's number may be another's by now.
    for child in children:
        if child.poll() is None:
            stop(child)
    for path in temporary:
        shutil.rmtree(path, ignore_errors=True)
    sys.stdout.write(answer)
    sys.stdout.flush()
    os._exit(0)


signal.signal(signal.SIGALRM, finish)
signal.alarm(P["deadline"])


def command(argv, timeout, **options):
    """(exit code, its output), or TimeoutError when it takes longer (it is killed then)."""
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             errors="replace", start_new_session=True, **options)
    children.append(child)
    try:
        output, _ = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop(child)
        child.communicate()
        raise TimeoutError from None
    return child.returncode, output


def guarded(key, step, *args):
    try:
        step(*args)
    except Exception as e:
        put(key, [False, "%s: %s" % (type(e).__name__, e)])
'''

PROBE = PRELUDE + r'''

def connect(address, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(P["connect_timeout"])
    try:
        s.connect((address, port))
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except TimeoutError:
        return "timeout"
    except OSError as e:
        return "unreachable" if e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH) else "error %s" % e.errno
    finally:
        s.close()


def reach(group, key, address, port, want):
    # A connection that should get through is tried three times (ARP, a slow first SYN).
    for attempt in range(1 if want == "blocked" else 3):
        result = connect(address, port)
        if want == "blocked" or result == "open" or (want == "reachable" and result == "refused"):
            break
        time.sleep(1)
    with lock:
        if not done:
            out.setdefault(group, {})[key] = result


def flag():
    try:
        st = os.lstat(P["flag"])
    except FileNotFoundError:
        return put("flag", [False, "missing"])
    if not stat.S_ISREG(st.st_mode):
        return put("flag", [False, "not a regular file"])
    put("flag", [st.st_size > 0, "" if st.st_size > 0 else "empty"])


def dns():
    problems = []
    for name, want in sorted(P["dns"].items()):
        try:
            found = sorted({a[4][0] for a in socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)})
        except OSError as e:
            problems.append("%s: no answer (%s)" % (name, e.strerror or e))
            continue
        if want and want not in found:
            problems.append("%s is %s, not %s" % (name, ", ".join(found), want))
    put("dns", [not problems, "; ".join(problems)])


def https(key, host, port):
    # GET: nginx refuses it (POST only), which shows the way and the certificate; nothing is submitted.
    context = ssl.create_default_context()
    last = None
    for attempt in range(3):
        try:
            c = http.client.HTTPSConnection(host, port, timeout=10, context=context)
            c.request("GET", "/submit", headers={"User-Agent": "worms-check"})
            status = c.getresponse().status
            c.close()
            return put(key, [status < 500, "HTTP %d" % status])
        except ssl.SSLCertVerificationError as e:
            return put(key, [False, "certificate not verified: %s" % e.verify_message])
        except (OSError, http.client.HTTPException) as e:
            last = e
            time.sleep(2)
    put(key, [False, "%s: %s" % (type(last).__name__, last)])


def pypi():
    host, port, path = P["pypi"]
    last = None
    for attempt in range(2):
        try:
            c = http.client.HTTPConnection(host, port, timeout=10)
            c.request("GET", path)
            status = c.getresponse().status
            c.close()
            return put("pypi", [status == 200, "HTTP %d" % status])
        except (OSError, http.client.HTTPException) as e:
            last = e
            time.sleep(2)
    put("pypi", [False, "%s: %s" % (type(last).__name__, last)])


def apt():
    # The index into a directory of its own: the VM's own lists stay as the sandbox's snapshot has them.
    d = tempfile.mkdtemp(prefix="worms-check-", dir=P["tmp"])
    temporary.append(d)
    try:
        os.makedirs(d + "/lists/partial")
        argv = ["apt-get", "update", "--error-on=any", "-o", "Dir::State::Lists=" + d + "/lists",
                "-o", "Dir::Cache::pkgcache=", "-o", "Dir::Cache::srcpkgcache=", "-o", "APT::Sandbox::User=root",
                "-o", "Acquire::http::Timeout=10", "-o", "Acquire::Retries=0"]
        code, output = command(argv, P["apt_timeout"], env=dict(os.environ, LC_ALL="C"))
        lines = output.splitlines()
        why = next((l for l in lines if l.startswith("E:")), None) or next((l for l in lines if l.startswith(("Err", "W:"))), "")
        put("apt", [code == 0, "" if code == 0 else (why or "exit %d" % code)])
    except TimeoutError:
        put("apt", [False, "no index within %d s" % P["apt_timeout"]])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def clock():
    # A gameplan may remove or disable the VM's time service: then the host's clock check alone
    # (records), which every VM gets.
    until = time.time() + P["time_wait"]
    while True:
        code, output = command(["timedatectl", "show", "-p", "NTPSynchronized", "-p", "CanNTP", "-p", "NTP"], 10)
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if "no" in (values.get("CanNTP"), values.get("NTP")):
            return put("time", [True, "no time service: its clock as the host sees it"])
        synced = values.get("NTPSynchronized") == "yes"
        if synced or time.time() > until:
            break
        time.sleep(3)
    put("time", [synced, "" if synced else "not synchronised after %d s (%s)" % (P["time_wait"], " ".join(output.split())[:80])])


def files():
    found = {}
    for key, path, owner, mode in P["files"]:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            found[key] = "missing"
            continue
        except OSError as e:
            found[key] = "unreadable (%s)" % e.strerror
            continue
        problems = [] if stat.S_ISREG(st.st_mode) else ["not a regular file"]
        try:
            name = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            name = str(st.st_uid)
        if name != owner:
            problems.append("owner " + name)
        if stat.S_IMODE(st.st_mode) != int(mode, 8):
            problems.append("mode %04o" % stat.S_IMODE(st.st_mode))
        found[key] = ", ".join(problems)
    put("files", found)


steps = [("flag", flag), ("dns", dns), ("pypi", pypi), ("apt", apt), ("time", clock), ("files", files)]
steps += [(key, https, key, host, port) for key, (host, port) in sorted(P["https"].items())]
steps += [("error-connect-" + key, reach, "connect", key, a, port, want) for key, a, port, want in P["connect"]]
steps += [("error-forbidden-" + key, reach, "forbidden", key, a, port, "blocked") for key, a, port in P["forbidden"]]
threads = [threading.Thread(target=guarded, args=step, daemon=True) for step in steps]
for t in threads:
    t.start()
for t in threads:
    t.join()
finish()
'''


def _lookup(addressing, path):
    value = addressing
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, str) else None


def _nic_addresses(spec):
    """{VM name: {network name: its address there}}."""
    return {vm["name"]: {nic["network"]: nic["address"].split("/")[0] for nic in vm["nics"]} for vm in spec["vms"]}


def _port_in(dport, port):
    if dport is None:
        return True
    low, _, high = str(dport).partition(":")
    return int(low) <= port <= int(high or low)


def allows(vm, direction, port, peer):
    """What the VM's own firewall does with a TCP connection on `port` to `peer` (out) or from it
    (in): the gameplan's rules in their order, the first that matches decides; none: dropped (the
    policy). The platform's groups before them match only the services, the LLM endpoint and the
    host's SSH on the management NIC, never a sandbox network."""
    address = ipaddress.ip_address(peer)
    for rule in vm["firewall"]:
        if rule["direction"] != direction or rule["proto"] != "tcp" or not _port_in(rule["dport"], port):
            continue
        if any(address in ipaddress.ip_network(cidr, strict=False) for cidr in rule["peers"]):
            return rule["action"] == "ACCEPT"
    return False


def listening(vm):
    """The ports something answers on, by the gameplan: sshd (on 22 and its own port) and the
    framework's services (SERVICE_PORTS) the VM runs."""
    ports = {22, vm["ssh_port"]}
    for unit in vm["services"]:
        ports |= {low for low, high in SERVICE_PORTS.get(unit, []) if low == high}
    return ports


def connections(spec, name):
    """What `name` should and should not reach on the sandbox networks, by its gameplan's rules:
    [{to, network, address, port, want}]. For every VM it shares a network with: each SSH port of
    that VM, and each single port a rule of either side names, allowed by both firewalls or not.
    want: open (allowed, and something answers there), reachable (allowed, an answer or a refusal
    from the VM), blocked (no answer at all). And one VM it shares no network with, which nothing
    routes to (sandbox-gw does not route between sandbox networks)."""
    by_name = {vm["name"]: vm for vm in spec["vms"]}
    addresses = _nic_addresses(spec)
    src = by_name[name]
    out, neighbours = [], {name}
    for net in spec["networks"]:
        if name not in net["vms"]:
            continue
        for other in net["vms"]:
            if other == name:
                continue
            neighbours.add(other)
            dst = by_name[other]
            src_ip, dst_ip = addresses[name][net["name"]], addresses[other][net["name"]]
            ports = {22, dst["ssh_port"]}
            for vm, direction, peer in ((dst, "in", src_ip), (src, "out", dst_ip)):
                for rule in vm["firewall"]:
                    dport = rule["dport"]
                    if rule["direction"] == direction and rule["proto"] == "tcp" and dport and ":" not in dport:
                        if any(ipaddress.ip_address(peer) in ipaddress.ip_network(c, strict=False) for c in rule["peers"]):
                            ports.add(int(dport))
            for port in sorted(ports):
                allowed = allows(src, "out", port, dst_ip) and allows(dst, "in", port, src_ip)
                want = ("open" if port in listening(dst) else "reachable") if allowed else "blocked"
                out.append({"to": other, "network": net["name"], "address": dst_ip, "port": port, "want": want})
    stranger = next((vm for vm in spec["vms"] if vm["name"] not in neighbours and vm["nics"]), None)
    if stranger is not None:
        out.append({"to": stranger["name"], "network": None, "address": stranger["nics"][0]["address"].split("/")[0],
                    "port": stranger["ssh_port"], "want": "blocked"})  # fmt: skip
    return out


def plan(spec, name, addressing, maintenance, devbox_address=None):
    """What PROBE tries on the VM `name`: its parameters, and the host's own notes to read the
    answer by (they never go to the VM: `connect` is its connections, `files` and `forbidden`
    what each number stands for). `maintenance`: {VM name: its management address}."""
    vm = next(v for v in spec["vms"] if v["name"] == name)
    conns = connections(spec, name)
    forbidden = [(label, _lookup(addressing, path), port) for label, path, port in FORBIDDEN]
    forbidden.append(INTERNET)
    if devbox_address:
        forbidden.append(("devbox", devbox_address, 80))  # an allowed port: only the destination is wrong
    other = next((ip for vm_name, ip in sorted(maintenance.items()) if vm_name != name), None)
    if other:
        forbidden.append(("maintenance", other, 22))  # another VM's management NIC: the host's alone
    forbidden = [(label, address, port) for label, address, port in forbidden if address]
    files = [f for f in spec["files"] if f["vm"] == name and f["at_reset"]]
    params = {
        "deadline": DEADLINE, "apt_timeout": APT_TIMEOUT, "time_wait": TIME_WAIT, "connect_timeout": CONNECT_TIMEOUT,
        "flag": FLAG_PATH, "tmp": "/var/tmp",
        "dns": {host: _lookup(addressing, path) or "" for host, path in DNS_NAMES.items()},
        "https": {"scoring": ["scoring.worms.internal", 443], "logging": ["logging.worms.internal", 443]},
        "pypi": ["pypi.worms.internal", 3141, f"/root/pypi/+simple/{PYPI_PACKAGE}/"],
        "connect": [[str(i), c["address"], c["port"], c["want"]] for i, c in enumerate(conns)],
        "forbidden": [[label, address, port] for label, address, port in forbidden],
        "files": [[str(f["id"]), f["path"], f["owner"], f["mode"]] for f in files],
    }  # fmt: skip
    return params, {"connect": conns, "files": {str(f["id"]): f for f in files}}


def script(params, body=None):
    """PROBE (or another script on PRELUDE: startup.INIT) with its parameters as the first line
    (base64: no quoting to get wrong)."""
    return f'P = "{base64.b64encode(json.dumps(params).encode()).decode()}"\n{PROBE if body is None else body}'


def params_of(text):
    """The parameters `script` put into a probe (for a fake VM in the tests)."""
    match = re.match(r'P = "([A-Za-z0-9+/=]*)"\n', text)
    return json.loads(base64.b64decode(match.group(1))) if match else None


def answer(output):
    """The probe's answer in a VM's output, or None."""
    line = next((line for line in reversed(output.splitlines()) if line.startswith(MARK)), None)
    if line is None:
        return None
    try:
        value = json.loads(line[len(MARK):])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _text(value, size=160):
    """A VM's words, printable and short."""
    return "".join(c if c.isprintable() else " " for c in str(value))[:size]


def _pair(answer, key):
    value = answer.get(key)
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], bool):
        return value[0], _text(value[1])
    return False, "no answer"


def records(name, params, notes, reply, sent, note, why=None):
    """The check's records for one VM from its probe's answer ([(check, ok, detail)]); what they
    leave out (paths, owners, units, rules, the VM's own error) goes to `note`, the job log."""
    if reply is None:
        note(f"check {name}: the probe did not answer: {why}")
        return [(f"vm:{name}:probe", False, "no answer (the job log on the host says why)")]
    prefix = f"vm:{name}"
    out = []
    late = reply.get("deadline") is True
    out.append((f"{prefix}:probe", not late, f"it ran out of time ({DEADLINE} s): what it had not done failed" if late else ""))
    for key in ("flag", "dns", "scoring", "logging", "apt", "pypi"):
        ok, detail = _pair(reply, key)
        out.append((f"{prefix}:{key}", ok, detail))

    synced, detail = _pair(reply, "time")
    started = reply.get("started")
    offset = started - sent if isinstance(started, (int, float)) and not isinstance(started, bool) else None
    if offset is None:
        out.append((f"{prefix}:time", False, "no clock reading"))
    elif abs(offset) > CLOCK_TOLERANCE:
        out.append((f"{prefix}:time", False, f"its clock is {offset:+.0f} s from the host's"))
    else:
        out.append((f"{prefix}:time", synced, detail or "synchronised"))

    forbidden = reply.get("forbidden") if isinstance(reply.get("forbidden"), dict) else {}
    labels = [label for label, _, _ in params["forbidden"]]
    reached = [label for label in labels if forbidden.get(label) not in BLOCKED]
    for label, address, port in params["forbidden"]:
        if label in reached:
            note(f"check {name}: {label} ({address}:{port}): {_text(forbidden.get(label, 'no answer'), 40)}, expected no answer")
    out.append((f"{prefix}:isolated", not reached,
                f"got to: {', '.join(reached)}" if reached else f"none of {', '.join(labels)} answered"))  # fmt: skip

    results = reply.get("connect") if isinstance(reply.get("connect"), dict) else {}
    bad = 0
    for key, conn in enumerate(notes["connect"]):
        result = _text(results.get(str(key), "no answer"), 40)
        ok = {"open": result == "open", "reachable": result in ("open", "refused"), "blocked": result in BLOCKED}[conn["want"]]
        if not ok:
            bad += 1
            where = f"on {conn['network']}" if conn["network"] else "sharing no network"
            note(f"check {name}: {conn['to']} port {conn['port']} ({conn['address']}, {where}): {conn['want']} expected, got {result}")
    total = len(notes["connect"])
    out.append((f"{prefix}:firewall", not bad, f"{bad} of {total} connections not as the gameplan's firewall says "
                "(the job log on the host lists them)" if bad else f"{total} connections as its rules say"))  # fmt: skip

    if notes["files"]:
        found = reply.get("files") if isinstance(reply.get("files"), dict) else {}
        wrong = []
        for key, entry in notes["files"].items():
            problem = _text(found.get(key, "no answer"), 80)
            if problem:
                wrong.append(key)
                note(f"check {name}: file {key} ({entry['path']}, {entry['owner']} {entry['mode']}): {problem}")
        out.append((f"{prefix}:files", not wrong, f"file{'s' if len(wrong) > 1 else ''} {', '.join(wrong)} not as declared "
                    "(the job log on the host says how)" if wrong else f"{len(notes['files'])} as declared"))  # fmt: skip
    return out


def run(ex, ip, params, body=None, timeout=DEADLINE + 60):
    """PROBE (or `body`: startup.INIT) on the VM at `ip` (python3 -I: nothing of the VM's own on
    its path; -B: no .pyc written): (its answer or None, when it was sent by the host's clock, why
    there is none)."""
    sent = time.time()
    try:
        output = ex.ssh(ip, ["sudo", "python3", "-I", "-B", "-"], input=script(params, body), timeout=timeout)
    except Exception as error:  # noqa: BLE001  (SSH, a timeout: the VM did not answer)
        return None, sent, f"{type(error).__name__}: {_text(error, 300)}"
    reply = answer(output or "")
    return reply, sent, None if reply is not None else "no @worms-probe line in its output"
