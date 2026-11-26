"""worms-sbom's collector (src/worms_cli/sbom.py, docs/03-manage-py.md#worms-sbom).

Run on a VM or on the host as `python3 -`: this file's text, then a last line `main(<request>)`;
it prints one JSON document on stdout. The standard library only (a VM has python3 and nothing
else that is sure), and it only reads: what is installed (dpkg, every virtualenv, git
checkouts), and on the apt cache and the PyPI mirror what they serve. The one thing it runs that
is not a query, `apt-get update` into a directory of its own, does what the apt cache's own
check does (roles/private_internet/apt_cache/tasks/check.yml): ask the cache, offline, for the
index a fresh VM gets. The controller imports this file too (parse_control, copyright_info,
pypi_name_version, VENV_SCRIPT) for what it reads itself.
"""

from __future__ import annotations

import email.parser
import glob
import gzip
import hashlib
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor

DPKG_FORMAT = "${Package}\t${Version}\t${Architecture}\t${Maintainer}\t${Homepage}\t${source:Package}\t${db:Status-Abbrev}\n"
WALK_ROOTS = ("/opt", "/srv", "/usr/local", "/var/lib", "/home", "/root")
WALK_DEPTH = 4
# Not walked for virtualenvs and checkouts: data, caches, package databases.
SKIP_DIRS = {"+files", ".git", "node_modules", "__pycache__", "site-packages", "dist-packages", "postgresql", "mongodb",
             "docker", "docker-registry", "apt-cacher-ng", "kiwix", "dpkg", "apt", ".cache", "lost+found", ".indices"}  # fmt: skip
# apt-cacher-ng's names of the repositories it caches (its backends_* files).
APT_CACHER_BACKENDS = {"debrep": "deb.debian.org/debian", "secdeb": "security.debian.org/debian-security"}
COPYRIGHT_LIMIT = 256 * 1024

# Run by a virtualenv's own python (-I): what is installed in it, from its metadata.
VENV_SCRIPT = r"""
import importlib.metadata as m, json, sys
paths = json.loads(sys.argv[1]) if len(sys.argv) > 1 else None
out = []
for d in (m.distributions(path=paths) if paths else m.distributions()):
    md = d.metadata
    lic = md.get("License-Expression") or ""
    raw = (md.get("License") or "").strip()
    if not lic and raw and raw.upper() != "UNKNOWN" and "\n" not in raw and len(raw) <= 80:
        lic = raw
    if not lic:
        lic = " AND ".join(dict.fromkeys(c.split(" :: ")[-1] for c in (md.get_all("Classifier") or []) if c.startswith("License :: ")))
    home = md.get("Home-page") or ""
    for url in md.get_all("Project-URL") or []:
        label, _, link = url.partition(",")
        if not home and label.strip().lower() in ("homepage", "home", "source", "source code", "repository", "github"):
            home = link.strip()
    sha256 = ""
    try:
        direct = json.loads(d.read_text("direct_url.json") or "{}")
        info = direct.get("archive_info") or {}
        sha256 = (info.get("hashes") or {}).get("sha256") or (info.get("hash", "").partition("sha256=")[2])
    except ValueError:
        pass
    out.append({"name": md.get("Name"), "version": md.get("Version"), "author": md.get("Author") or md.get("Author-email") or "",
                "maintainer": md.get("Maintainer") or md.get("Maintainer-email") or "", "license": lic, "home": home,
                "sha256": sha256})
print(json.dumps(out))
"""


def run(argv, **kw) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, **kw).stdout


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -- Debian's formats ---------------------------------------------------------------------------


def parse_control(text: str) -> list[dict]:
    """Paragraphs of a Debian control-format text (dpkg's status, an apt Packages index): one
    dict each, field names as written, continuation lines joined."""
    out, record, last = [], {}, None
    for line in text.splitlines():
        if not line.strip():
            if record:
                out.append(record)
            record, last = {}, None
        elif line[0] in " \t" and last:
            record[last] += "\n" + line.strip()
        elif ":" in line:
            last, _, value = line.partition(":")
            record[last] = value.strip()
    if record:
        out.append(record)
    return out


SPDX = {"expat": "MIT", "mit": "MIT", "apache-2.0": "Apache-2.0", "apache-2": "Apache-2.0", "mpl-2.0": "MPL-2.0",
        "bsd-2-clause": "BSD-2-Clause", "bsd-3-clause": "BSD-3-Clause", "isc": "ISC", "zlib": "Zlib", "psf-2": "PSF-2.0",
        "python": "PSF-2.0", "public-domain": "public-domain", "cc0-1.0": "CC0-1.0", "artistic": "Artistic-1.0"}  # fmt: skip


def spdx(name: str) -> str:
    """A Debian copyright file's license short name, as SPDX names it where that is clear
    (GPL-2+ is GPL-2.0-or-later); else as written."""
    name = name.strip()
    match = re.fullmatch(r"(A?L?GPL|GFDL)-(\d)(?:\.(\d))?(\+)?", name, re.I)
    if match:
        kind, major, minor, plus = match.groups()
        return f"{kind.upper()}-{major}.{minor or 0}-{'or-later' if plus else 'only'}"
    return SPDX.get(name.lower(), name)


LICENSE_WORDS = (  # what names a license in a free-text license or copyright file
    ("Python Software Foundation License", "PSF-2.0"), ("GNU AFFERO GENERAL PUBLIC LICENSE", "AGPL-3.0"),
    ("GNU LESSER GENERAL PUBLIC LICENSE", "LGPL"), ("GNU GENERAL PUBLIC LICENSE", "GPL"), ("Apache License", "Apache-2.0"),
    ("Mozilla Public License", "MPL-2.0"), ("Permission is hereby granted, free of charge", "MIT"), ("MIT License", "MIT"),
    ("Redistribution and use in source and binary forms", "BSD"),
)  # fmt: skip


def guess_license(text: str) -> str:
    """The licenses a free-text file names: its /usr/share/common-licenses/<name> references
    (Debian's way), else each of LICENSE_WORDS in it (GPL and LGPL with their version when it
    follows); "" if none."""
    named = list(dict.fromkeys(spdx(n) for n in re.findall(r"/usr/share/common-licenses/([A-Za-z0-9.+_-]+)", text)))
    if named:
        return " AND ".join(named[:6])
    found, lower = [], text.lower()
    for words, name in LICENSE_WORDS:
        at = lower.find(words.lower())
        if at < 0:
            continue
        if name in ("GPL", "LGPL"):
            version = re.search(r"Version (\d)", text[at:at + 400])
            name = f"{name}-{version.group(1)}.0" if version else name
        if name not in found:
            found.append(name)
    return " AND ".join(found[:4])


def copyright_info(text: str) -> tuple[str, str]:
    """(license, author) from a package's copyright file. Machine-readable (DEP-5): the licenses
    of its Files paragraphs (Files: * first), the author its Upstream-Contact, else the first
    copyright holder of Files: *. Free text: guess_license, no author."""
    if not text:
        return "", ""
    if not text.lstrip().startswith("Format:"):
        return guess_license(text), ""
    paragraphs = parse_control(text)
    header = paragraphs[0] if paragraphs else {}
    files = [p for p in paragraphs if "Files" in p]
    files.sort(key=lambda p: p["Files"].split()[0] != "*")
    licenses = list(dict.fromkeys(spdx(p["License"].splitlines()[0]) for p in files if p.get("License")))
    author = header.get("Upstream-Contact", "").splitlines()[0] if header.get("Upstream-Contact") else ""
    if not author and files and files[0].get("Copyright"):
        author = re.sub(r"^(?:\(c\)\s*)?[\d,\s-]+", "", files[0]["Copyright"].splitlines()[0], flags=re.I).strip()
    shown = licenses[:8] + (["..."] if len(licenses) > 8 else [])
    return " AND ".join(shown) or guess_license(text), author[:200]


def read_text(path, limit=COPYRIGHT_LIMIT) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def origin_of(list_name: str) -> str:
    """"apt (deb.debian.org/debian trixie/main)" from an apt lists file's name."""
    base = os.path.basename(list_name)
    site, _, rest = base.partition("_dists_")
    parts = rest.split("_")
    where = f" {parts[0]}/{parts[1]}" if len(parts) > 2 else ""
    return f"apt ({site.replace('_', '/')}{where})"


INDEX_FIELDS = {"Package", "Version", "Architecture", "SHA256", "Filename", "Maintainer", "Homepage", "Source"}


def lists_index(lists_dir: str, wanted=None) -> dict:
    """{(package, version, arch): {sha256, filename, maintainer, homepage, source, origin}} from the
    Packages indexes of an apt lists directory (plain, .gz or .xz), of the `wanted` keys only
    when given. Read line by line: Debian's index has some 70,000 packages, and a service VM
    has 300 MB of memory (the whole of it parsed at once: the collector was killed)."""
    index = {}
    for path in sorted(glob.glob(os.path.join(lists_dir, "*_Packages*"))):
        opener = gzip.open if path.endswith(".gz") else lzma.open if path.endswith(".xz") else open if path.endswith("_Packages") else None
        if opener is None:
            continue
        origin = origin_of(path)

        def keep(r):
            key = (r.get("Package", ""), r.get("Version", ""), r.get("Architecture", ""))
            if key[0] and key[1] and (wanted is None or key in wanted) and key not in index:
                index[key] = {"sha256": r.get("SHA256", ""), "filename": r.get("Filename", ""), "maintainer": r.get("Maintainer", ""),
                              "homepage": r.get("Homepage", ""), "source": r.get("Source", "").split(" ")[0], "origin": origin}  # fmt: skip

        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                record = {}
                for line in fh:
                    if not line.strip():
                        keep(record)
                        record = {}
                    elif line[0] not in " \t":
                        key, _, value = line.partition(":")
                        if key in INDEX_FIELDS:
                            record[key] = value.strip()
                keep(record)
        except (OSError, EOFError, lzma.LZMAError):
            continue
    return index


# -- what is installed --------------------------------------------------------------------------


def os_info() -> dict:
    values = {}
    for line in read_text("/etc/os-release").splitlines():
        key, _, value = line.partition("=")
        values[key] = value.strip().strip('"')
    return {"id": values.get("ID", ""), "version_id": values.get("VERSION_ID", ""), "pretty": values.get("PRETTY_NAME", ""),
            "hostname": os.uname().nodename}  # fmt: skip


def dpkg_installed(names=None) -> list[dict]:
    """The installed packages (all, or those named), with their license and author from
    /usr/share/doc/<package>/copyright and the .deb's SHA256 from this machine's apt lists."""
    wanted = set(names) if names else None
    installed = []
    for line in run(["dpkg-query", "-W", "-f", DPKG_FORMAT]).splitlines():
        fields = line.split("\t")
        if len(fields) >= 7 and fields[6].startswith(("ii", "hi")) and (wanted is None or fields[0] in wanted):
            installed.append(fields[:6])
    index = lists_index("/var/lib/apt/lists", {(f[0], f[1], f[2]) for f in installed})
    out = []
    for name, version, arch, maintainer, homepage, source in installed:
        lic, author = copyright_info(read_text(f"/usr/share/doc/{name}/copyright"))
        found = index.get((name, version, arch), {})
        out.append({"name": name, "version": version, "arch": arch, "maintainer": maintainer, "homepage": homepage,
                    "source": source or name, "license": lic, "author": author, "sha256": found.get("sha256", ""),
                    "origin": found.get("origin", "")})  # fmt: skip
    return out


def pveversion() -> list[str]:
    """The packages `pveversion -v` names (Proxmox VE's own), on a Proxmox host."""
    if not shutil.which("pveversion"):
        return []
    names = []
    for line in run(["pveversion", "-v"]).splitlines():
        name = line.split(":", 1)[0].strip()
        if re.fullmatch(r"[a-z0-9][a-z0-9.+-]+", name):
            names.append(name)
    return names


def walk(roots=WALK_ROOTS, depth=WALK_DEPTH):
    """Every directory under `roots`, `depth` levels at most, SKIP_DIRS left out; symlinks not followed."""
    for root in roots:
        base = root.rstrip("/").count("/")
        for path, dirs, _files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(path, d))]
            if path.count("/") - base >= depth:
                dirs[:] = []
            yield path, dirs


def pip_index(venv: str) -> str:
    for conf in (os.path.join(venv, "pip.conf"), "/etc/pip.conf", "/root/.config/pip/pip.conf"):
        match = re.search(r"^\s*index-url\s*=\s*(\S+)", read_text(conf), re.M)
        if match:
            return match.group(1)
    return "pypi.org"


def venvs() -> list[dict]:
    """Every virtualenv (a pyvenv.cfg) under WALK_ROOTS, and pip's packages outside apt
    (/usr/local), each with what is installed in it (VENV_SCRIPT, run by its own python)."""
    out = []
    for path, _dirs in walk():
        if os.path.isfile(os.path.join(path, "pyvenv.cfg")) and os.path.exists(os.path.join(path, "bin", "python")):
            try:
                dists = json.loads(run([os.path.join(path, "bin", "python"), "-I", "-c", VENV_SCRIPT], timeout=120))
            except (subprocess.SubprocessError, OSError, ValueError) as exc:
                out.append({"path": path, "error": str(exc)[:200], "dists": []})
                continue
            out.append({"path": path, "index": pip_index(path), "dists": dists})
    local = sorted(glob.glob("/usr/local/lib/python3*/dist-packages"))
    if local and any(os.scandir(local[-1])) and shutil.which("python3"):
        try:
            dists = json.loads(run(["python3", "-I", "-c", VENV_SCRIPT, json.dumps(local)], timeout=120))
            out.append({"path": local[-1], "index": pip_index("/"), "dists": dists})
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
    return out


def git_head(gitdir: str) -> str:
    head = read_text(os.path.join(gitdir, "HEAD")).strip()
    if not head.startswith("ref: "):
        return head
    ref = head[5:]
    value = read_text(os.path.join(gitdir, ref)).strip()
    if value:
        return value
    for line in read_text(os.path.join(gitdir, "packed-refs")).splitlines():
        if line.endswith(" " + ref):
            return line.split()[0]
    return ""


def git_checkouts() -> list[dict]:
    """Every git checkout under WALK_ROOTS (3 levels): its commit, its origin, its license file."""
    out = []
    for path, _dirs in walk(depth=3):
        gitdir = os.path.join(path, ".git")
        if not os.path.isdir(gitdir):
            continue
        config = read_text(os.path.join(gitdir, "config"))
        url = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', config, re.S)
        license_text = "".join(read_text(f, 8000) for f in sorted(glob.glob(os.path.join(path, "LICEN[CS]E*")) +
                                                                   glob.glob(os.path.join(path, "COPYING*")))[:1])  # fmt: skip
        out.append({"path": path, "commit": git_head(gitdir), "url": url.group(1) if url else "",
                    "license": guess_license(license_text)})  # fmt: skip
    return out


# -- what the apt cache serves, and what a fresh VM gets from it ---------------------------------


def deb_copyright(path: str, name: str) -> str:
    """The copyright file inside a .deb (its data, streamed: nothing is unpacked to disk)."""
    try:
        data = subprocess.Popen(["dpkg-deb", "--fsys-tarfile", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        text = subprocess.run(["tar", "-xO", "-f", "-", f"./usr/share/doc/{name}/copyright"], stdin=data.stdout,
                              capture_output=True, timeout=300).stdout  # fmt: skip
        data.stdout.close()
        data.wait(timeout=60)
        return text[:COPYRIGHT_LIMIT].decode("utf-8", "replace")
    except (subprocess.SubprocessError, OSError):
        return ""


def apt_served(cache_dir: str) -> list[dict]:
    """Every .deb the apt cache holds (what sandbox and devbox VMs install from): its control
    fields (dpkg-deb -f), its SHA256, the license and author of its copyright file."""
    debs = sorted(glob.glob(os.path.join(cache_dir, "**", "*.deb"), recursive=True))

    def one(path):
        fields = {}
        try:
            for line in run(["dpkg-deb", "-f", path, "Package", "Version", "Architecture", "Maintainer", "Homepage",
                             "Source"], timeout=120).splitlines():  # fmt: skip
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        except (subprocess.SubprocessError, OSError):
            pass
        name = fields.get("Package") or os.path.basename(path).split("_")[0]
        lic, author = copyright_info(deb_copyright(path, name))
        backend = os.path.relpath(path, cache_dir).split(os.sep)[0]
        return {"name": name, "version": fields.get("Version", ""), "arch": fields.get("Architecture", ""),
                "maintainer": fields.get("Maintainer", ""), "homepage": fields.get("Homepage", ""),
                "source": fields.get("Source", name).split(" ")[0], "license": lic, "author": author,
                "sha256": sha256_file(path), "file": os.path.basename(path),
                "origin": f"apt-cacher-ng ({APT_CACHER_BACKENDS.get(backend, backend)})"}  # fmt: skip

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(one, debs))


def resolve(request: dict, served: list[dict]) -> dict:
    """For each set of packages: what apt installs on a fresh VM (dpkg's status `status`, the VM
    image's) to get them from the cache, with --no-install-recommends as the roles do; and the
    SHA256 of the packages in `lookup`. The index is the clients' (apt_cache_client_lists), or
    asked from the cache, offline, into a directory of its own (the cache's check does the same)."""
    options = request["options"]
    lists = request["client_lists"]
    work = tempfile.mkdtemp(prefix="worms-sbom.")
    try:
        if not glob.glob(os.path.join(lists, "*_Packages*")):
            lists = os.path.join(work, "lists")
            os.makedirs(os.path.join(lists, "partial"))
            subprocess.run(["apt-get", "update", "-qq", *options, "-o", f"Dir::State::Lists={lists}"], capture_output=True,
                           text=True, timeout=600)  # fmt: skip
        status = os.path.join(work, "status")
        with open(status, "w") as fh:
            fh.write(request["status"])
        chosen, errors = {}, {}
        for name, packages in request["sets"].items():
            chosen[name] = []
            if not packages:
                continue
            done = subprocess.run(["apt-get", "-s", "install", "--no-install-recommends", *options,
                                   "-o", f"Dir::State::Lists={lists}", "-o", f"Dir::State::status={status}",
                                   "-o", "Debug::NoLocking=true", *packages], capture_output=True, text=True, timeout=600)  # fmt: skip
            chosen[name] = re.findall(r"^Inst (\S+) (?:\[\S+\] )?\((\S+) .*?\[(\S+)\]\)", done.stdout, re.M)
            if done.returncode != 0:
                errors[name] = (done.stderr or done.stdout).strip()[-400:]
        lookups = [tuple(key) for key in request.get("lookup", [])]
        index = lists_index(lists, {key for keys in chosen.values() for key in keys} | set(lookups))
        cached = {p["file"]: p for p in served}
        sets = {}
        for name, keys in chosen.items():
            got = []
            for pkg, version, arch in keys:
                found = index.get((pkg, version, arch), {})
                deb = cached.get(os.path.basename(found.get("filename", "")), {})
                got.append({"name": pkg, "version": version, "arch": arch, "maintainer": found.get("maintainer", ""),
                            "homepage": found.get("homepage", ""), "source": found.get("source") or pkg,
                            "license": deb.get("license", ""), "author": deb.get("author", ""),
                            "sha256": found.get("sha256", ""), "origin": found.get("origin", "")})  # fmt: skip
            if name in errors:
                got.append({"error": errors[name]})
            sets[name] = got
        lookup = {}
        for pkg, version, arch in lookups:
            found = index.get((pkg, version, arch))
            if found:
                lookup[f"{pkg} {version} {arch}"] = {"sha256": found["sha256"], "origin": found["origin"]}
        return {"sets": sets, "lookup": lookup}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -- what the PyPI mirror serves ---------------------------------------------------------------


def pypi_name_version(filename: str) -> tuple[str, str]:
    """(project, version) from a distribution's file name: a wheel (name-version-...whl) or an
    sdist (name-version.tar.gz, .zip, .tar.bz2)."""
    if filename.endswith(".whl"):
        parts = filename[:-4].split("-")
        return parts[0], parts[1] if len(parts) > 1 else ""
    stem = re.sub(r"\.(tar\.gz|tar\.bz2|tar\.xz|tgz|zip)$", "", filename)
    name, _, version = stem.rpartition("-")
    return (name, version) if name else (stem, "")


def dist_metadata(path: str) -> dict:
    """The core metadata inside a wheel (*.dist-info/METADATA) or an sdist (PKG-INFO)."""
    text = ""
    try:
        if path.endswith((".whl", ".zip")):
            with zipfile.ZipFile(path) as archive:
                names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA") or n.count("/") == 1 and n.endswith("/PKG-INFO")]
                if names:
                    text = archive.read(sorted(names, key=len)[0])[:COPYRIGHT_LIMIT].decode("utf-8", "replace")
        elif ".tar" in path or path.endswith(".tgz"):
            with tarfile.open(path) as archive:
                for member in archive:
                    if member.isfile() and member.name.count("/") == 1 and member.name.endswith("/PKG-INFO"):
                        text = archive.extractfile(member).read(COPYRIGHT_LIMIT).decode("utf-8", "replace")
                        break
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError, lzma.LZMAError):
        return {}
    if not text:
        return {}
    md = email.parser.Parser().parsestr(text, headersonly=True)
    lic = md.get("License-Expression") or ""
    raw = (md.get("License") or "").strip()
    if not lic and raw and raw.upper() != "UNKNOWN" and "\n" not in raw and len(raw) <= 80:
        lic = raw
    if not lic:
        lic = " AND ".join(dict.fromkeys(c.split(" :: ")[-1] for c in (md.get_all("Classifier") or []) if c.startswith("License :: ")))
    return {"name": md.get("Name", ""), "version": md.get("Version", ""), "license": lic,
            "author": md.get("Author") or md.get("Author-email") or "", "maintainer": md.get("Maintainer") or md.get("Maintainer-email") or "",
            "home": md.get("Home-page") or ""}  # fmt: skip


def devpi_served(server_dir: str) -> list[dict]:
    """Every file the PyPI mirror (devpi) holds: +files/<user>/<index>/+f/.../<file>, with its
    SHA256 and the metadata inside it."""
    root = os.path.join(server_dir, "+files")
    files = sorted(os.path.join(path, f) for path, _dirs, names in os.walk(root) for f in names)

    def one(path):
        rel = os.path.relpath(path, root).split(os.sep)
        filename = rel[-1]
        name, version = pypi_name_version(filename)
        md = dist_metadata(path)
        return {"name": md.get("name") or name, "version": md.get("version") or version, "file": filename,
                "index": "/".join(rel[:2]), "license": md.get("license", ""), "author": md.get("author", ""),
                "maintainer": md.get("maintainer", ""), "home": md.get("home", ""), "sha256": sha256_file(path)}  # fmt: skip

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(one, files))


# -- the request ------------------------------------------------------------------------------


def main(request: dict) -> None:
    """`request`: what to collect ({"dpkg": true or [names], "pveversion": true, "venvs": true,
    "git": true, "apt_served": {cache_dir}, "resolve": {...}, "devpi_served": {server_dir}}).
    A part that fails is a note; the others are still collected."""
    out = {"os": os_info(), "notes": []}

    def part(key, fn, *args):
        try:
            out[key] = fn(*args)
        except Exception as exc:  # noqa: BLE001  (said, and the rest goes on)
            out["notes"].append(f"{key}: {type(exc).__name__}: {str(exc)[:300]}")

    names = None
    if request.get("pveversion"):
        part("pveversion", pveversion)
        names = out.get("pveversion", [])
    if request.get("dpkg"):
        wanted = request["dpkg"] if isinstance(request["dpkg"], list) else None
        part("dpkg", dpkg_installed, None if wanted is None and names is None else [*(names or []), *(wanted or [])])
    if request.get("venvs"):
        part("venvs", venvs)
    if request.get("git"):
        part("git", git_checkouts)
    if request.get("apt_served"):
        part("apt_served", apt_served, request["apt_served"]["cache_dir"])
    if request.get("resolve"):
        part("resolved", resolve, request["resolve"], out.get("apt_served", []))
    if request.get("devpi_served"):
        part("devpi_served", devpi_served, request["devpi_served"]["server_dir"])
    json.dump(out, sys.stdout)
