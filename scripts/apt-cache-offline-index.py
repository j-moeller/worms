#!/usr/bin/env python3
"""Rebuild apt-cacher-ng's repository index from the apt-cache VM's own package lists, offline.

    worms rsync scripts/apt-cache-offline-index.py apt-cache:
    worms login apt-cache
      sudo python3 apt-cache-offline-index.py

For a cache whose index is gone or incomplete (offline it answers "503 Unable to download in
offline mode" for InRelease or a Packages list, and a fresh VM finds no package at all) when no
internet is at hand for `worms apply apt-cache --packages`.

Every fill (`worms apply apt-cache --packages`, role apt_cache, tasks/seed.yml) fetches the index
through the cache, as a client VM asks for it, and keeps it in /var/lib/apt-cache-client/lists:
each suite's InRelease and every Packages list it names, uncompressed, signed. (The VM's own apt
fetches from the Debian mirrors. Seeded before that change, it went through the cache, and its
own /var/lib/apt/lists hold that index: used when the other is not there.)
This puts them where the cache serves them from: InRelease under its own name, each Packages list
under its own name and under by-hash/SHA256/<its hash>, each with the header file the cache keeps
next to it. A fresh VM then gets InRelease, asks for the compressed lists (not in the cache: 503),
falls back to the uncompressed ones, which InRelease names with their hash, and checks them
against it as always. The .deb files are untouched: the cache serves what it was filled with.

Every list is checked against its suite's InRelease first; the previous index trees are kept as
dists.before-offline-index. At the end the cache is restarted and asked, as a fresh VM would, for
the whole index (an empty lists directory, `apt-get update --error-on=any`).
"""

import email.utils
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

CACHE = "/var/cache/apt-cacher-ng"
# The index the last fill fetched through the cache (role apt_cache: apt_cache_client_lists), or,
# seeded before it was kept there, the VM's own lists, which then went through the cache.
CLIENT_LISTS, OWN_LISTS = "/var/lib/apt-cache-client/lists", "/var/lib/apt/lists"
LISTS = CLIENT_LISTS if os.path.isdir(CLIENT_LISTS) and os.listdir(CLIENT_LISTS) else OWN_LISTS
# How a client VM asks the cache (role apt_cache: apt_cache_client_options), for the check at the end.
CLIENT_OPTIONS = ["-o", "Dir::Etc::SourceList=/etc/apt-cache-client/sources.list",
                  "-o", "Dir::Etc::SourceParts=/etc/apt-cache-client/sources.list.d",
                  "-o", "Dir::Cache::pkgcache=", "-o", "Dir::Cache::srcpkgcache=",
                  "-o", "Acquire::Check-Valid-Until=false", "-o", "Acquire::Languages=none",
                  "-o", "APT::Sandbox::User=root"]
BACKENDS = {"debian": "debrep", "debian-security": "secdeb"}  # acng's backends for the two archives
LIST_NAME = re.compile(r"^[^_]+_deb\.debian\.org_(debian|debian-security)_dists_([A-Za-z0-9._+-]+(?:_[A-Za-z0-9._+-]+)*)$")
NEW, OLD = "dists.offline-index-new", "dists.before-offline-index"


def header(path, source):
    stat = os.stat(path)
    return (f"HTTP/1.1 200 OK\r\nContent-Length: {stat.st_size}\r\n"
            f"Last-Modified: {email.utils.formatdate(stat.st_mtime, usegmt=True)}\r\n"
            f"X-Original-Source: {source}\r\n\r\n").encode()


def place(src, dest, source):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(src, dest)
    shutil.copystat(src, dest)
    with open(dest + ".head", "wb") as f:
        f.write(header(dest, source))


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    if os.geteuid() != 0:
        sys.exit("run it as root: sudo python3 apt-cache-offline-index.py")
    found = {}  # (archive, suite) -> {"InRelease": path, "lists": [(relpath, path)]}
    for name in sorted(os.listdir(LISTS)):
        match = LIST_NAME.match(name)
        if not match:
            continue
        archive, rest = match.groups()
        rel = rest.replace("_", "/")  # apt's list names are the URL path with / as _
        suite = rel.split("/")[0]
        entry = found.setdefault((archive, suite), {"InRelease": None, "lists": []})
        if rel == f"{suite}/InRelease":
            entry["InRelease"] = os.path.join(LISTS, name)
        elif rel.endswith("/Packages"):
            entry["lists"].append((rel, os.path.join(LISTS, name)))
    if not found:
        sys.exit(f"no Debian lists in {LISTS}: is this the apt-cache VM, filled at least once?")

    for backend in set(BACKENDS.values()):
        shutil.rmtree(os.path.join(CACHE, backend, NEW), ignore_errors=True)
    placed, problems = 0, []
    for (archive, suite), entry in sorted(found.items()):
        backend = BACKENDS[archive]
        if not entry["InRelease"]:
            problems.append(f"{archive} {suite}: no InRelease in {LISTS}")
            continue
        release = open(entry["InRelease"], encoding="utf-8", errors="replace").read()
        base = os.path.join(CACHE, backend, NEW)
        url = f"http://deb.debian.org/{archive}/dists"
        place(entry["InRelease"], os.path.join(base, suite, "InRelease"), f"{url}/{suite}/InRelease")
        for rel, path in entry["lists"]:
            digest = sha256(path)
            target = rel[len(suite) + 1:]  # e.g. main/binary-amd64/Packages
            if not re.search(rf"^ {digest}\s+\d+\s+{re.escape(target)}$", release, re.M):
                problems.append(f"{archive} {rel}: not the list its InRelease names (sha256 {digest[:12]}): left out")
                continue
            directory = os.path.dirname(rel)
            place(path, os.path.join(base, rel), f"{url}/{rel}")
            place(path, os.path.join(base, directory, "by-hash", "SHA256", digest), f"{url}/{directory}/by-hash/SHA256/{digest}")
            placed += 1
        print(f"{archive} {suite}: InRelease and {len(entry['lists'])} Packages list(s)")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for backend in sorted(set(BACKENDS.values())):
        root = os.path.join(CACHE, backend)
        new, current, kept = os.path.join(root, NEW), os.path.join(root, "dists"), os.path.join(root, OLD)
        if not os.path.isdir(new):
            continue
        if os.path.isdir(current):
            if os.path.exists(kept):
                os.rename(kept, f"{kept}.{stamp}")
            os.rename(current, kept)
        os.rename(new, current)
        subprocess.run(["chown", "-R", "apt-cacher-ng:apt-cacher-ng", current], check=True)
        print(f"{current}: in place (the previous one: {kept})")
    subprocess.run(["systemctl", "restart", "apt-cacher-ng"], check=True)
    time.sleep(2)

    for problem in problems:
        print(f"warning: {problem}")
    print(f"{placed} Packages list(s) placed; asking the cache for a fresh VM's whole index ...")
    lists = tempfile.mkdtemp(prefix="worms-check-lists.", dir="/var/lib/apt")
    try:
        os.mkdir(os.path.join(lists, "partial"))
        options = CLIENT_OPTIONS if os.path.exists("/etc/apt-cache-client/sources.list") else ["-o", "APT::Sandbox::User=root"]
        check = subprocess.run(["apt-get", "update", "--error-on=any", "-o", f"Dir::State::Lists={lists}", *options],
                               capture_output=True, text=True)
    finally:
        shutil.rmtree(lists, ignore_errors=True)
    out = check.stdout + check.stderr
    print("\n".join(line for line in out.splitlines() if line.startswith(("Get:", "Hit:", "Err:", "E:", "W:"))))
    if check.returncode != 0:
        print(f"\nFAILED: the cache still does not give a fresh VM its index (exit {check.returncode}).")
        print(f"To put the previous index back: for b in debrep secdeb; do rm -rf {CACHE}/$b/dists && "
              f"mv {CACHE}/$b/{OLD} {CACHE}/$b/dists; done; systemctl restart apt-cacher-ng")
        return 1
    print("\nOK: the cache gives a fresh VM its whole index, offline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
