"""`worms-sbom [<component>...] [--out DIR]`: a software bill of materials of the platform, one CSV per
component in sbom/ and all of them in sbom/sbom.csv (docs/03-manage-py.md#worms-sbom).

    host            the root: Proxmox VE (`pveversion -v`), Ansible, OpenTofu and its providers (the
                    lock files), Python (the host's, and the worms CLI's virtualenv)
    dashboard ...   each platform VM: its Debian packages, every virtualenv, every git checkout,
                    read on the VM by sbom_collect.py over its management SSH (as `worms run`)
    apt-cache       also every .deb it serves (the cache sandbox and devbox VMs install from)
    devpi           also every file the PyPI mirror serves
    devbox VM       not a team's VM (assumed rooted by its team): the VM image's packages, read
    sandbox VM      from the image file (libguestfs, read-only), and what is installed on top of it
                    (SetupDevbox.yml's packages; the gameplans' for a sandbox VM), resolved by apt
                    on the apt cache against the image's dpkg status, as a fresh VM gets them

Every row has the same columns (COLUMNS): the minimum elements of an SBOM, and where in WORMS it
is. Nothing is changed anywhere; what a VM sends back is data, never run.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from worms_cli import output, sbom_collect
from worms_cli.build import BuildError

COLUMNS = ["Component Name", "Version", "Supplier", "Component author", "Unique identifier", "Component hash", "License",
           "Source information", "Timestamp", "System component", "Relationship"]  # fmt: skip
PLATFORM_VMS = ("dashboard", "orchestrator", "database", "scoring", "logging")
DEVBOX, SANDBOX = "devbox VM", "sandbox VM"
# What every VM boots from (opentofu/modules/os_images: Debian's cloud image, the guest agent added).
IMAGE = "opentofu/modules/os_images/images/debian-13-generic-amd64-with-guest-agent.qcow2"
IMAGE_SOURCE = "Debian cloud image, trixie (cloud.debian.org), qemu-guest-agent added (opentofu/modules/os_images/Makefile)"
APT_CACHE_DEFAULTS = "ansible/roles/private_internet/apt_cache/defaults/main.yml"
DEVPI_DEFAULTS = "ansible/roles/private_internet/devpi/defaults/main.yml"
# The host's tools besides Proxmox VE: Debian packages, looked up on the controller.
HOST_TOOLS = {"ansible": "Ansible", "ansible-core": "Ansible", "tofu": "OpenTofu",
              **{f"python3{v}{m}": "Python" for v in ("", ".11", ".12", ".13", ".14") for m in ("", "-minimal")}}  # fmt: skip
KNOWN_LICENSES = {"tofu": "MPL-2.0", "bpg/proxmox": "MPL-2.0"}  # where the package itself does not say


def private_internet() -> list[str]:
    from worms_cli import components

    return [n for n, c in components.COMPONENTS.items() if c.kind == "vm" and c.stack == "private_internet"]


def systems() -> list[str]:
    """Every component of the SBOM, the root first."""
    return ["host", *PLATFORM_VMS, DEVBOX, SANDBOX, *private_internet()]


def slug(system: str) -> str:
    return system.lower().replace(" ", "-")


def program(request: dict) -> str:
    """sbom_collect.py's text with its request: what `python3 -` runs on a VM or the host."""
    return Path(sbom_collect.__file__).read_text() + f"\nmain(json.loads({json.dumps(request)!r}))\n"


def h1(directory: Path) -> str:
    """OpenTofu's h1: hash of an unpacked provider (Go's dirhash.Hash1): a SHA256 over the
    sorted "<sha256>  <path>" lines of its files."""
    lines = []
    for path in sorted(directory.rglob("*"), key=lambda p: p.relative_to(directory).as_posix()):
        if path.is_file():
            lines.append(f"{sbom_collect.sha256_file(path)}  {path.relative_to(directory).as_posix()}\n")
    return "h1:" + base64.b64encode(hashlib.sha256("".join(lines).encode()).digest()).decode()


def lock_providers(repo: Path) -> list[dict]:
    """Each provider version the stacks' lock files pin: its stacks, its hashes there, and the
    h1 of the copy installed here (linux_amd64) when one is."""
    found: dict = {}
    for lock in sorted((repo / "opentofu" / "stacks").glob("**/.terraform.lock.hcl")):
        stack = lock.parent.relative_to(repo / "opentofu" / "stacks").as_posix()
        for block in re.finditer(r'provider "([^"]+)" \{(.*?)\n\}', lock.read_text(), re.S):
            address, body = block.groups()
            version = re.search(r'version\s*=\s*"([^"]+)"', body)
            if not version:
                continue
            entry = found.setdefault((address, version.group(1)), {"address": address, "version": version.group(1),
                                                                    "stacks": [], "hashes": set(), "installed": ""})  # fmt: skip
            entry["stacks"].append(stack)
            entry["hashes"] |= set(re.findall(r'"(h1:[^"]+)"', body))
            unpacked = lock.parent / ".terraform" / "providers" / address / version.group(1) / "linux_amd64"
            if not entry["installed"] and unpacked.is_dir():
                entry["installed"] = h1(unpacked)
    return list(found.values())


def row(name, version="", supplier="", author="", purl="", digest="", license="", source="", system="", relationship=""):
    return dict(zip(COLUMNS, [name, version, supplier, author, purl, digest, license, source, "", system, relationship]))


def deb_namespace(origin: str) -> str:
    """The purl namespace of a Debian package: debian, or the vendor of the repository it is from
    (download.proxmox.com: proxmox)."""
    site = re.search(r"\(([^/ )]+)", origin or "")
    host = site.group(1) if site else ""
    if not host or "debian" in host:
        return "debian"
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else host


def deb_row(p: dict, distro: str, system: str, relationship: str) -> dict:
    ns = deb_namespace(p.get("origin", ""))
    purl = f"pkg:deb/{ns}/{p['name']}@{quote(p['version'], safe='')}?arch={p['arch']}&distro={distro}"
    return row(p["name"], p["version"], p.get("maintainer", ""), p.get("author", ""), purl,
               f"sha256:{p['sha256']}" if p.get("sha256") else "", p.get("license") or KNOWN_LICENSES.get(p["name"], ""),
               p.get("origin") or "dpkg (no entry in this machine's apt index)", system, relationship)  # fmt: skip


def pypi_row(d: dict, system: str, relationship: str, source: str, file_name: str = "") -> dict:
    name = re.sub(r"[-_.]+", "-", (d.get("name") or "")).lower()
    purl = f"pkg:pypi/{name}@{quote(d.get('version') or '', safe='')}" + (f"?file_name={quote(file_name)}" if file_name else "")
    return row(d.get("name") or name, d.get("version") or "", d.get("maintainer") or d.get("author") or "", d.get("author") or "",
               purl, f"sha256:{d['sha256']}" if d.get("sha256") else "", d.get("license") or "", source, system, relationship)  # fmt: skip


def git_row(g: dict, system: str) -> dict:
    url, commit = g.get("url", ""), g.get("commit", "")
    name = Path(g["path"]).name
    github = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    purl = f"pkg:github/{github.group(1)}/{github.group(2)}@{commit}" if github else f"pkg:generic/{name}@{commit}"
    owner = github.group(1) if github else ""
    return row(name, commit[:12], owner, owner, purl, f"sha1:{commit}" if commit else "", g.get("license", ""),
               f"git ({url or 'no origin'})", system, f"installed (git checkout {g['path']})")  # fmt: skip


class Sbom:
    def __init__(self, config, out: Path):
        self.config = config
        self.repo = Path(config.repo_root)
        self.out = out
        self.timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.problems: list[str] = []
        self.image_data: dict | None = None

    # -- collecting --------------------------------------------------------------------------

    def collect(self, argv, env, request, name, timeout=3600) -> dict:
        done = subprocess.run(argv, env=env, input=program(request), capture_output=True, text=True, timeout=timeout)
        if done.returncode != 0:
            raise BuildError(f"the collector on {name} exited {done.returncode}: {(done.stderr or done.stdout).strip()[-300:]}")
        try:
            data = json.loads(done.stdout)
        except ValueError:
            raise BuildError(f"the collector on {name} sent no JSON: {done.stdout.strip()[:200]}") from None
        self.problems += [f"{name}: {note}" for note in data.get("notes", [])]
        return data

    def local(self, request, name) -> dict:
        python = shutil.which("python3") or sys.executable
        return self.collect([python, "-"], None, request, name)

    def image(self) -> dict:
        """The VM image's packages (dpkg's status), their copyright files and os-release, read from
        the image file with libguestfs (read-only: the image is not changed or booted as a VM)."""
        if self.image_data is not None:
            return self.image_data
        path = self.repo / IMAGE
        if not path.is_file() or not shutil.which("virt-cat"):
            raise BuildError(f"the VM image needs {IMAGE} and libguestfs (virt-cat, virt-tar-out) here")
        status = subprocess.run(["virt-cat", "-a", str(path), "/var/lib/dpkg/status"], capture_output=True, text=True,
                                timeout=1800, check=True).stdout  # fmt: skip
        release = subprocess.run(["virt-cat", "-a", str(path), "/etc/os-release"], capture_output=True, text=True,
                                 timeout=1800).stdout  # fmt: skip
        docs = subprocess.run(["virt-tar-out", "-a", str(path), "/usr/share/doc", "-"], capture_output=True, timeout=1800).stdout
        texts, links = {}, {}
        with tarfile.open(fileobj=io.BytesIO(docs), mode="r:") as archive:
            for member in archive:
                parts = member.name.removeprefix("./").split("/")
                if len(parts) == 1 and member.issym():
                    links[parts[0]] = member.linkname.rstrip("/").split("/")[-1]
                elif len(parts) == 2 and parts[1] == "copyright" and member.isfile():
                    texts[parts[0]] = archive.extractfile(member).read(sbom_collect.COPYRIGHT_LIMIT).decode("utf-8", "replace")
        values = dict(line.split("=", 1) for line in release.splitlines() if "=" in line)
        packages = []
        for p in sbom_collect.parse_control(status):
            if not p.get("Status", "").endswith(" installed"):
                continue
            name = p["Package"]
            lic, author = sbom_collect.copyright_info(texts.get(name) or texts.get(links.get(name, ""), ""))
            packages.append({"name": name, "version": p.get("Version", ""), "arch": p.get("Architecture", ""),
                             "maintainer": p.get("Maintainer", ""), "homepage": p.get("Homepage", ""),
                             "source": p.get("Source", name).split(" ")[0], "license": lic, "author": author})  # fmt: skip
        self.image_data = {"status": status, "packages": packages, "sha256": sbom_collect.sha256_file(path),
                           "version": values.get("VERSION", "").strip('"'), "distro": f"debian-{values.get('VERSION_ID', '').strip(chr(34))}"}  # fmt: skip
        return self.image_data

    def gameplan_packages(self) -> dict[str, list[str]]:
        """{package: the gameplans that install it on a sandbox VM} (each VM's `install`)."""
        from gameplan import loader
        from gameplan.runtime import instantiate

        folder = self.repo / "gameplans"
        out: dict[str, list[str]] = {}
        for name in loader.available(folder):
            try:
                spec = instantiate(loader.load(folder, name).cls).spec()
            except Exception as exc:  # noqa: BLE001  (one gameplan that does not load: said, the others go on)
                self.problems.append(f"{SANDBOX}: gameplan {name} not read ({exc})")
                continue
            for vm in spec.get("vms", []):
                for package in vm.get("install") or []:
                    out.setdefault(package, [])
                    if Path(name).stem not in out[package]:
                        out[package].append(Path(name).stem)
        return out

    def devbox_packages(self) -> list[str]:
        from worms_cli import changes

        tree = changes.Tree(self.repo)
        return sorted(changes._packages(tree, changes.reach(tree, "devbox", ["SetupDevbox.yml"])))

    def defaults(self, path) -> dict:
        import yaml

        return yaml.safe_load((self.repo / path).read_text()) or {}

    # -- the components ------------------------------------------------------------------------

    def host(self, session) -> list[dict]:
        from worms_cli.build import Runner

        commit = Runner(self.config).commit()
        if session.laptop:
            pve = self.collect(session.ssh_argv("python3 -"), session.env(), {"pveversion": True, "dpkg": []}, "the host")
            tools = self.local({"dpkg": list(HOST_TOOLS)}, "this controller")
        else:
            pve = tools = self.local({"pveversion": True, "dpkg": list(HOST_TOOLS)}, "the host")
        distro = f"{pve['os']['id']}-{pve['os']['version_id']}"
        rows = [row("WORMS", commit, "WORMS", "", f"pkg:generic/worms@{commit}", "", "", f"this checkout ({self.repo})",
                    "host", f"root: the platform on {pve['os']['hostname']} ({pve['os']['pretty']})")]  # fmt: skip
        names = set(pve.get("pveversion", []))
        rows += [deb_row(p, distro, "host", "installed (Proxmox VE)") for p in pve.get("dpkg", []) if p["name"] in names]
        rows += [deb_row(p, f"{tools['os']['id']}-{tools['os']['version_id']}", "host", f"installed ({HOST_TOOLS[p['name']]})")
                 for p in tools.get("dpkg", []) if p["name"] in HOST_TOOLS]  # fmt: skip
        for provider in lock_providers(self.repo):
            host, _, kind = provider["address"].partition("/")
            installed = provider["installed"]
            note = "" if not installed or installed in provider["hashes"] else "; the installed copy's h1 is NOT in the lock files"
            rows.append(row(kind, provider["version"], kind.split("/")[0], kind.split("/")[0],
                            f"pkg:generic/{host}/{kind}@{provider['version']}", installed, KNOWN_LICENSES.get(kind, ""),
                            f"OpenTofu registry ({host}){note}", "host",
                            f"OpenTofu provider (stacks: {', '.join(provider['stacks'])})"))  # fmt: skip
        dists = json.loads(subprocess.run([sys.executable, "-I", "-c", sbom_collect.VENV_SCRIPT], capture_output=True,
                                          text=True, check=True).stdout)  # fmt: skip
        rows += [pypi_row(d, "host", f"installed (Python: the worms CLI's virtualenv {sys.prefix})", "pypi (pypi.org)")
                 for d in dists]  # fmt: skip
        return rows

    def vm_rows(self, data: dict, system: str) -> list[dict]:
        distro = f"{data['os']['id']}-{data['os']['version_id']}"
        rows = [deb_row(p, distro, system, "installed") for p in data.get("dpkg", [])]
        for venv in data.get("venvs", []):
            if venv.get("error"):
                self.problems.append(f"{system}: virtualenv {venv['path']}: {venv['error']}")
            rows += [pypi_row(d, system, f"installed (virtualenv {venv['path']})", f"pypi ({venv.get('index', 'pypi.org')})")
                     for d in venv.get("dists", [])]  # fmt: skip
        rows += [git_row(g, system) for g in data.get("git", [])]
        for p in data.get("apt_served", []):
            rows.append(deb_row(p, distro, system, "served (apt-cacher-ng: what sandbox and devbox VMs install from)"))
        for d in data.get("devpi_served", []):
            source = f"pypi.org via devpi {d['index']} (a mirror)" if d["index"] == "root/pypi" else f"devpi {d['index']}"
            rows.append(pypi_row(d, system, f"served (devpi {d['index']}: what devbox VMs pip install from)", source, d["file"]))
        return rows

    def vm(self, catalogue, deployed, name, request) -> tuple[list[dict], dict]:
        vm = catalogue.resolve(name)
        if vm.get("status") != "running":
            raise BuildError(f"{name} is {vm.get('status')}: not read (start it first)")
        argv, env = catalogue.ssh_argv(vm, "sudo -n python3 -", interactive=False)
        data = self.collect(argv, env, request, name)
        commit = deployed.get(name, ("", ""))[0]
        head = row(name, commit, "WORMS", "", f"pkg:generic/worms/{slug(name)}@{commit}", "", "",
                   f"Proxmox VM {vm['vmid']} ({vm.get('pool', '?')}), {data['os']['pretty']}", name,
                   "part of WORMS (configured by Ansible from this repository)")  # fmt: skip
        return [head, *self.vm_rows(data, name)], data

    def built(self, system, resolved: list[dict], wanted: dict[str, list[str]], by: str) -> list[dict]:
        """A devbox or sandbox VM: the image, its packages, what is installed on top (`resolved`)."""
        from worms_cli.build import Runner

        image = self.image()
        commit = Runner(self.config).commit()
        rows = [row(system, commit, "WORMS", "", f"pkg:generic/worms/{slug(system)}@{commit}", "", "",
                    f"built from the VM image by {by} (the round agent), per team or round", system,
                    "part of WORMS (one per team or sandbox; not read from a team's VM)"),
                row(Path(IMAGE).name, image["version"], "Debian", "Debian Cloud Team", f"pkg:generic/debian/{Path(IMAGE).name}",
                    f"sha256:{image['sha256']}", "", IMAGE_SOURCE, system, "the VM image every VM boots from")]  # fmt: skip
        lookup = image.get("lookup", {})
        for p in image["packages"]:
            found = lookup.get(f"{p['name']} {p['version']} {p['arch']}", {})
            rows.append(deb_row({**p, **found, "origin": found.get("origin") or f"apt, in the VM image ({Path(IMAGE).name})"},
                                image["distro"], system, "in the VM image"))  # fmt: skip
        for p in resolved:
            if p.get("error"):
                self.problems.append(f"{system}: apt on the apt cache: {p['error']}")
                continue
            names = wanted.get(p["name"])
            relationship = f"installed by {by}" + (f" for {', '.join(names)}" if names and names != [by] else "") \
                if names else f"installed by {by}: a dependency"  # fmt: skip
            rows.append(deb_row(p, image["distro"], system, relationship))
        return rows

    # -- the run -------------------------------------------------------------------------------

    def run(self, wanted: list[str]) -> int:
        from worms_cli import changes
        from worms_cli.vms import Catalogue

        chosen = [s for s in systems() if not wanted or s in wanted or slug(s) in wanted]
        unknown = [w for w in wanted if w not in systems() and w not in map(slug, systems())]
        if unknown:
            output.error(f"not a component of the SBOM: {', '.join(unknown)} (one of: {', '.join(map(slug, systems()))})")
            return 2
        catalogue = Catalogue(self.config)
        session = catalogue.runner.session
        deployed = changes.deployed(session)
        results: dict[str, list[dict]] = {}
        built = [s for s in (DEVBOX, SANDBOX) if s in chosen]
        sets, wanted_by = {}, {}
        apt = self.defaults(APT_CACHE_DEFAULTS)
        if built:
            try:
                self.image()
                print(output.status_cell("ok", f"the VM image: {len(self.image_data['packages'])} packages ({Path(IMAGE).name})"), flush=True)
            except (BuildError, subprocess.SubprocessError, OSError, tarfile.TarError) as exc:
                self.problems.append(f"the VM image: {exc}")
                built = []
        if DEVBOX in built:
            packages = self.devbox_packages()
            sets[DEVBOX], wanted_by[DEVBOX] = packages, {p: ["SetupDevbox.yml"] for p in packages}
        if SANDBOX in built:
            by_gameplan = self.gameplan_packages()
            sets[SANDBOX], wanted_by[SANDBOX] = sorted(by_gameplan), {p: [f"gameplan {g}" for g in gs] for p, gs in by_gameplan.items()}
        requests = {name: {"dpkg": True, "venvs": True, "git": True} for name in [*PLATFORM_VMS, *private_internet()] if name in chosen}
        if "apt-cache" in requests or built:
            client_dir = apt.get("apt_cache_client_dir", "/etc/apt-cache-client")
            options = shlex.split(apt.get("apt_cache_client_options", "").replace("{{ apt_cache_client_dir }}", client_dir))
            request = requests.setdefault("apt-cache", {"dpkg": False})
            if "apt-cache" in chosen:
                request["apt_served"] = {"cache_dir": apt.get("apt_cache_cache_dir", "/var/cache/apt-cacher-ng")}
            if built:
                request["resolve"] = {"options": options, "client_lists": apt.get("apt_cache_client_lists", ""),
                                      "status": self.image_data["status"], "sets": sets,
                                      "lookup": [[p["name"], p["version"], p["arch"]] for p in self.image_data["packages"]]}  # fmt: skip
        if "devpi" in requests:
            requests["devpi"]["devpi_served"] = {"server_dir": self.defaults(DEVPI_DEFAULTS).get("devpi_server_dir", "/var/lib/devpi")}
        print(output.color("reading the VMs (the apt cache and the PyPI mirror hash every file they serve: minutes)", "dim"), flush=True)

        def one(name):
            try:
                if name == "host":
                    return name, self.host(session), None
                rows, data = self.vm(catalogue, deployed, name, requests[name])
                return name, (rows if name in chosen else None), data
            except (BuildError, subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
                self.problems.append(f"{name}: {exc}")
                return name, None, None

        names = [n for n in ["host", *requests] if n == "host" and "host" in chosen or n in requests]
        with ThreadPoolExecutor(max_workers=6) as pool:
            for name, rows, data in pool.map(one, names):
                if rows is not None:
                    results[name] = rows
                    print(output.status_cell("ok", f"{name}: {len(rows)} rows"), flush=True)
                if name == "apt-cache" and data and built:
                    resolved = data.get("resolved", {})
                    self.image_data["lookup"] = resolved.get("lookup", {})
                    for system in built:
                        results[system] = self.built(system, resolved.get("sets", {}).get(system, []), wanted_by[system],
                                                     "SetupDevbox.yml" if system == DEVBOX else "the gameplans")  # fmt: skip
                        print(output.status_cell("ok", f"{system}: {len(results[system])} rows"), flush=True)
        for system in built:
            if system not in results:
                self.problems.append(f"{system}: not resolved (the apt cache was not read)")
        self.write(results, chosen)
        for problem in self.problems:
            print(output.status_cell("warn", problem), file=sys.stderr)
        missing = [s for s in chosen if s not in results]
        if missing:
            output.error(f"not in the SBOM: {', '.join(missing)} (above)")
        return 1 if missing else 0

    def write(self, results: dict[str, list[dict]], chosen: list[str]) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        everything = []
        for system in chosen:
            rows = results.get(system)
            if rows is None:
                continue
            for r in rows:
                r["Timestamp"] = self.timestamp
            body = [rows[0], *sorted(rows[1:], key=lambda r: (r["Relationship"], r["Component Name"].lower(), r["Version"]))]
            self._csv(self.out / f"{slug(system)}.csv", body)
            everything += body
        self._csv(self.out / "sbom.csv", everything)
        print(output.status_cell("ok", f"{len(everything)} rows: {self.out}/sbom.csv and one CSV per component ({self.timestamp})"))

    @staticmethod
    def _csv(path: Path, rows: list[dict]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="worms-sbom", description="A software bill of materials of the platform: one CSV per "
                                "component, and all of them in sbom.csv.", epilog="components: " + ", ".join(map(slug, systems())))  # fmt: skip
    p.add_argument("components", nargs="*", metavar="component", help="only these (default: every one)")
    p.add_argument("--out", metavar="DIR", help="where the CSV files go (default: sbom/ in this checkout)")
    return p


def main(argv=None) -> int:
    from worms_cli.config import Config

    args = parser().parse_args(argv)
    config = Config.load()
    out = Path(args.out) if args.out else Path(config.repo_root) / "sbom"
    try:
        return Sbom(config, out).run(args.components)
    except BuildError as exc:
        output.error(str(exc))
        return 1
    except KeyboardInterrupt:
        output.error("interrupted: nothing written")
        return 130
