"""worms-sbom (src/worms_cli/sbom.py) and its collector (src/worms_cli/sbom_collect.py): the formats
it reads, the identifiers and hashes it writes, the CSV files. The VMs themselves: docs/03 (live)."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "src"))

from worms_cli import sbom, sbom_collect  # noqa: E402

DEP5 = """Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: jq
Upstream-Contact: Stephen Dolan <mu@netsoc.tcd.ie>

Files: debian/*
Copyright: 2013 Debian
License: GPL-2+

Files: *
Copyright: 2012 Stephen Dolan
License: Expat
 Permission is hereby granted, free of charge, ...
"""


def test_a_machine_readable_copyright_file_gives_its_licenses_files_star_first_and_its_upstream_contact():
    assert sbom_collect.copyright_info(DEP5) == ("MIT AND GPL-2.0-or-later", "Stephen Dolan <mu@netsoc.tcd.ie>")
    without_contact = DEP5.replace("Upstream-Contact: Stephen Dolan <mu@netsoc.tcd.ie>\n", "")
    assert sbom_collect.copyright_info(without_contact)[1] == "Stephen Dolan"  # the first holder of Files: *


def test_a_free_text_copyright_file_gives_what_it_names():
    assert sbom_collect.copyright_info("see /usr/share/common-licenses/GPL-2 and /usr/share/common-licenses/LGPL-2.1")[0] \
        == "GPL-2.0-only AND LGPL-2.1-only"  # fmt: skip
    text = "Python Software Foundation License ... GNU GENERAL PUBLIC LICENSE\n Version 2, June 1991"
    assert sbom_collect.guess_license(text) == "PSF-2.0 AND GPL-2.0"
    assert sbom_collect.copyright_info("") == ("", "")
    assert sbom_collect.spdx("LGPL-2.1+") == "LGPL-2.1-or-later" and sbom_collect.spdx("GPL-3") == "GPL-3.0-only"


def test_an_apt_index_gives_each_package_its_deb_hash_and_repository(tmp_path):
    (tmp_path / "deb.debian.org_debian_dists_trixie_main_binary-amd64_Packages").write_text(
        "Package: jq\nVersion: 1.7.1-6\nArchitecture: amd64\nMaintainer: Debian <d@d>\nSHA256: abc\n"
        "Filename: pool/main/j/jq/jq_1.7.1-6_amd64.deb\nDescription: one\n more\n\nPackage: x\nVersion: 1\n"
    )
    index = sbom_collect.lists_index(str(tmp_path))
    assert index[("jq", "1.7.1-6", "amd64")]["sha256"] == "abc"
    assert index[("jq", "1.7.1-6", "amd64")]["origin"] == "apt (deb.debian.org/debian trixie/main)"
    assert sbom_collect.parse_control("A: 1\nB: x\n y\n")[0] == {"A": "1", "B": "x\ny"}


def test_a_served_python_distribution_gives_its_name_version_and_metadata(tmp_path):
    wheel = tmp_path / "Flask_Foo-2.0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("flask_foo-2.0.1.dist-info/METADATA", "Metadata-Version: 2.1\nName: Flask-Foo\nVersion: 2.0.1\n"
                         "Author: Ann\nClassifier: License :: OSI Approved :: MIT License\n")  # fmt: skip
    assert sbom_collect.pypi_name_version(wheel.name) == ("Flask_Foo", "2.0.1")
    assert sbom_collect.pypi_name_version("requests-2.32.3.tar.gz") == ("requests", "2.32.3")
    md = sbom_collect.dist_metadata(str(wheel))
    assert md["name"] == "Flask-Foo" and md["license"] == "MIT License" and md["author"] == "Ann"
    served = sbom_collect.devpi_served(str(tmp_path / "devpi"))
    assert served == []  # no +files: nothing served
    (tmp_path / "devpi" / "+files" / "root" / "pypi" / "+f" / "abc").mkdir(parents=True)
    shutil.copy(wheel, tmp_path / "devpi" / "+files" / "root" / "pypi" / "+f" / "abc" / wheel.name)
    [one] = sbom_collect.devpi_served(str(tmp_path / "devpi"))
    assert one["index"] == "root/pypi" and one["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    row = sbom.pypi_row(one, "devpi", "served", "pypi.org via devpi root/pypi (a mirror)", one["file"])
    assert row["Unique identifier"] == "pkg:pypi/flask-foo@2.0.1?file_name=Flask_Foo-2.0.1-py3-none-any.whl"
    assert row["Component hash"] == f"sha256:{one['sha256']}" and row["Supplier"] == "Ann"


def test_a_debian_package_is_identified_by_its_purl_with_its_vendor():
    p = {"name": "libc6", "version": "1:2.41-12+deb13u1", "arch": "amd64", "maintainer": "GNU Libc Maintainers",
         "sha256": "ff", "origin": "apt (deb.debian.org/debian trixie/main)"}  # fmt: skip
    row = sbom.deb_row(p, "debian-13", "dashboard", "installed")
    assert row["Unique identifier"] == "pkg:deb/debian/libc6@1%3A2.41-12%2Bdeb13u1?arch=amd64&distro=debian-13"
    assert row["Component hash"] == "sha256:ff" and row["Supplier"] == "GNU Libc Maintainers"
    proxmox = sbom.deb_row({**p, "origin": "apt (download.proxmox.com/debian/pve trixie/pve-no-subscription)"}, "debian-13", "host", "x")
    assert proxmox["Unique identifier"].startswith("pkg:deb/proxmox/")
    assert sbom.deb_row({**p, "name": "tofu", "origin": ""}, "debian-13", "host", "x")["License"] == "MPL-2.0"


def test_a_providers_h1_is_go_dirhash(tmp_path):
    (tmp_path / "b").write_bytes(b"two")
    (tmp_path / "a").write_bytes(b"one")
    lines = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in (("a", b"one"), ("b", b"two")))
    assert sbom.h1(tmp_path) == "h1:" + base64.b64encode(hashlib.sha256(lines.encode()).digest()).decode()
    for provider in sbom.lock_providers(REPO):  # where they are installed: in the lock files, as OpenTofu checks
        assert not provider["installed"] or provider["installed"] in provider["hashes"], provider


@pytest.mark.skipif(not shutil.which("dpkg-query"), reason="no dpkg here")
def test_the_collector_runs_as_python3_stdin_and_answers_json():
    done = subprocess.run([sys.executable, "-"], input=sbom.program({"dpkg": ["dpkg"]}), capture_output=True, text=True, check=True)
    data = json.loads(done.stdout)
    assert data["notes"] == [] and data["os"]["id"]
    assert [p["name"] for p in data["dpkg"]] == ["dpkg"] and data["dpkg"][0]["license"]


def test_each_component_gets_a_csv_and_sbom_csv_holds_them_all(tmp_path):
    run = sbom.Sbom(SimpleNamespace(repo_root=tmp_path), tmp_path / "sbom")
    results = {"host": [sbom.row("WORMS", "abc", relationship="root"), sbom.row("tofu", "1.13.2", relationship="installed")],
               "devbox VM": [sbom.row("devbox VM", "abc"), sbom.row("jq", "1.7")]}  # fmt: skip
    run.write(results, ["host", "dashboard", "devbox VM"])
    assert sorted(p.name for p in (tmp_path / "sbom").iterdir()) == ["devbox-vm.csv", "host.csv", "sbom.csv"]
    with open(tmp_path / "sbom" / "sbom.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == sbom.COLUMNS and [r["Component Name"] for r in rows] == ["WORMS", "tofu", "devbox VM", "jq"]
    assert {r["Timestamp"] for r in rows} == {run.timestamp} and rows[0]["System component"] == ""  # as given here
    assert sbom.systems()[:8] == ["host", "dashboard", "orchestrator", "database", "scoring", "logging", "devbox VM", "sandbox VM"]
    assert "apt-cache" in sbom.systems() and "devpi" in sbom.systems()
