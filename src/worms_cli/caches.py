"""The private internet's package lists: checked before a fill, added to and removed from.

    worms check apt|pypi|caches         do the listed packages exist, and install together?
    worms add package apt|pypi <name>... checked first, then appended to the list (in this checkout)
    worms remove package apt|pypi <name>...
    worms refill apt|pypi|caches         the lists checked, then the cache filled in the internet window

A fill fetches the whole list, and one name that does not exist (or conflicts with another) fails
it, after the window has opened and the devboxes have been shut down. The check runs first:

  apt   offline, on the apt-cache VM: `apt-get install -s` of the whole list against the index the
        cache holds (/var/lib/apt-cache-client/lists, which every fill keeps: role apt_cache), with
        nothing installed; every unknown name and every conflict, as apt says them.
  pypi  each name against PyPI's JSON API, from here: only with internet here (on the node of
        production, only while it has egress). Whether they install together is the fill's to find.

The lists are the roles' defaults (apt_cache_seed_packages, devpi_seed_packages): a change is a
change of this checkout, to commit, and reaches the cache with the next fill.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from worms_cli import output
from worms_cli.build import BuildError

LISTS = {
    "apt": ("ansible/roles/private_internet/apt_cache/defaults/main.yml", "apt_cache_seed_packages", "apt-cache"),
    "pypi": ("ansible/roles/private_internet/devpi/defaults/main.yml", "devpi_seed_packages", "devpi"),
}
APT_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]+")
PYPI_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9_,.-]+\])?")

# On the apt-cache VM: the client view (a fill's) if it is there, else the VM's own lists.
APT_SIMULATION = r"""set -u
d=$(mktemp -d /var/tmp/worms-check.XXXXXX); trap 'rm -rf "$d"' EXIT; : > "$d/status"
if [ -f /etc/apt-cache-client/sources.list ] && ls /var/lib/apt-cache-client/lists 2>/dev/null | grep -q InRelease; then
    set -- -o Dir::Etc::SourceList=/etc/apt-cache-client/sources.list -o Dir::Etc::SourceParts=/etc/apt-cache-client/sources.list.d \
           -o Dir::State::Lists=/var/lib/apt-cache-client/lists
    echo "@index the cache's (its last fill: $(stat -c %y /var/lib/apt-cache-client/lists | cut -c1-16))"
else
    set --
    echo "@index this VM's own lists ($(stat -c %y /var/lib/apt/lists | cut -c1-16)): no fill has kept the cache's yet"
fi
apt-get install -s --no-install-recommends -o Dir::State::status="$d/status" -o Dir::Cache::pkgcache= \
    -o Dir::Cache::srcpkgcache= -o Debug::NoLocking=1 "$@" @PACKAGES@ > "$d/out" 2>&1
echo "@exit $?"
echo "@packages $(grep -c '^Inst ' "$d/out")"
grep -E '^E:|^ [a-z0-9].* : |^ +(Depends|Conflicts|Breaks|PreDepends):' "$d/out" | head -n 60
"""


def listed(repo_root, kind: str) -> list[str]:
    path, key, _component = LISTS[kind]
    return list(yaml.safe_load((Path(repo_root) / path).read_text())[key] or [])


def _apt_vm(config):
    from worms_cli.remote import Remote, Target

    remote = Remote(config)
    vm = next((vm for vm in remote.catalogue.vms() if vm["name"] == "apt-cache"), None)
    if vm is None or vm.get("status") != "running":
        raise BuildError("the apt-cache VM is not running: the apt list is checked against its index (`worms start apt-cache`)")
    return remote, Target("apt-cache", vm)


def check_apt(config, names: list[str]) -> tuple[list[str], str]:
    """(problems, a line about what was checked): `apt-get install -s` of `names` on the apt-cache VM."""
    bad = [name for name in names if not APT_NAME.fullmatch(name)]
    if bad:
        return [f"not a Debian package name: {', '.join(bad)}"], ""
    remote, target = _apt_vm(config)
    script = APT_SIMULATION.replace("@PACKAGES@", " ".join(shlex.quote(n) for n in names))
    argv, env = remote.argv(target, "sh -s", True)
    proc = subprocess.run(argv, env=env, input=script.encode(), capture_output=True, timeout=300)
    out = proc.stdout.decode(errors="replace")
    facts = dict(line[1:].split(" ", 1) for line in out.splitlines() if line.startswith("@") and " " in line)
    if "exit" not in facts:
        raise BuildError(f"the check did not run on apt-cache: {proc.stderr.decode(errors='replace').strip()[-200:]}")
    problems = [line.strip() for line in out.splitlines() if line and not line.startswith("@")]
    problems = [p for p in problems if not p.startswith("E: The following information from --solver")]
    if facts["exit"] != "0" and not problems:
        problems = [f"apt-get exited {facts['exit']}"]
    return problems, f"{len(names)} name(s) against {facts.get('index', '?')}: {facts.get('packages', '?')} package(s) with their dependencies"


def online() -> bool:
    try:
        urllib.request.urlopen("https://pypi.org/simple/", timeout=5).close()
        return True
    except (urllib.error.URLError, OSError):
        return False


def check_pypi(names: list[str]) -> tuple[list[str], str]:
    """(problems, a line): each name on PyPI (its JSON API), when there is internet here."""
    bad = [name for name in names if not PYPI_NAME.fullmatch(name)]
    if bad:
        return [f"not a package name: {', '.join(bad)}"], ""
    if not online():
        return [], (f"{len(names)} name(s) not checked: no internet here (pypi.org); the fill finds them, inside the "
                    "window")  # fmt: skip

    def exists(name: str) -> str | None:
        project = name.split("[")[0]
        try:
            urllib.request.urlopen(f"https://pypi.org/pypi/{project}/json", timeout=20).close()
            return None
        except urllib.error.HTTPError as exc:
            return f"{name}: not on PyPI" if exc.code == 404 else f"{name}: PyPI said {exc.code}"
        except OSError as exc:
            return f"{name}: {exc}"

    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        problems = [p for p in pool.map(exists, names) if p]
    return problems, f"{len(names)} name(s) on PyPI"


def check(config, kind: str, names: list[str] | None = None) -> int:
    """`worms check apt|pypi`: the list (or `names`), said; 1 if a problem."""
    names = names if names is not None else listed(config.repo_root, kind)
    problems, line = check_apt(config, names) if kind == "apt" else check_pypi(names)
    if problems:
        print(output.status_cell("error", f"{kind}: {len(problems)} problem(s)" + (f" ({line})" if line else "")))
        for problem in problems:
            print(output.color(f"  {problem}", "red"))
        return 1
    print(output.status_cell("ok", f"{kind}: {line}"))
    return 0


def _list_span(lines: list[str], key: str) -> tuple[int, int]:
    """(the index of `key:`'s line, the index after its last `  - ` entry)."""
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{key}:"))
    end = start + 1
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("  - "):
            end = i + 1
        elif lines[i].strip() and not lines[i].startswith((" ", "#")):
            break
    return start, end


def _entry(line: str) -> str:
    return line.strip()[2:].split("#", 1)[0].strip() if line.strip().startswith("- ") else ""


def add(config, kind: str, names: list[str]) -> int:
    path, key, component = LISTS[kind]
    have = set(listed(config.repo_root, kind))
    new = [name for name in dict.fromkeys(names) if name not in have]
    for name in sorted(set(names) - set(new)):
        print(output.color(f"{name}: listed already", "dim"))
    if not new:
        return 0
    if check(config, kind, new):
        output.error("nothing added: the list has to stay fillable")
        return 1
    file = Path(config.repo_root) / path
    lines = file.read_text().splitlines()
    _start, end = _list_span(lines, key)
    lines[end:end] = [f"  - {name}" for name in new]
    file.write_text("\n".join(lines) + "\n")
    print(output.status_cell("ok", f"added to {path}: {', '.join(new)}"))
    print(output.color(f"a change of this checkout: commit it; the cache gets them with `worms refill {kind}`", "dim"))
    return 0


def remove(config, kind: str, names: list[str]) -> int:
    path, key, _component = LISTS[kind]
    file = Path(config.repo_root) / path
    lines = file.read_text().splitlines()
    start, end = _list_span(lines, key)
    gone = [i for i in range(start + 1, end) if _entry(lines[i]) in names]
    missing = sorted(set(names) - {_entry(lines[i]) for i in gone})
    if missing:
        output.error(f"not listed: {', '.join(missing)}")
        return 1
    for i in reversed(gone):
        del lines[i]
    file.write_text("\n".join(lines) + "\n")
    print(output.status_cell("ok", f"removed from {path}: {', '.join(names)}"))
    print(output.color("a change of this checkout: commit it. The cache keeps what it holds until it is rebuilt", "dim"))
    return 0


def components(kind: str) -> list[str]:
    return [LISTS[k][2] for k in (("apt", "pypi") if kind == "caches" else (kind,))]
