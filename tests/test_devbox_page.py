"""The team's devbox page: it says where the VMs are and how to get from A to C, and its layout
holds in a real browser (Chromium via Node Playwright, tests/browser/devbox_page.js).

The browser tests are skipped when Node.js or its Playwright package is not available. Set
WORMS_SCREENSHOTS=<dir> to keep the screenshots.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from conftest import REPO
from test_dashboard_login import HOST, proxied, site  # noqa: F401 (fixtures)
from test_dashboard_pages import team_upload  # noqa: F401 (fixture)

pytest.importorskip("django")

STATIC = REPO / "projects" / "dashboard" / "dashboard" / "static"
DRIVER = REPO / "tests" / "browser" / "devbox_page.js"
LOGIN = "ssh -A -J team@worms.example:2222 root@10.7.1.11"


def _playwright_available() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True, cwd=REPO).returncode == 0


def _page(site, team, addresses=True) -> str:  # noqa: F811
    """The dev view of a team whose devbox has index 1, as the reconciler leaves it."""
    from core.models.devbox import Devbox, DevboxSSHKey, DevboxVM

    devbox = Devbox.objects.create(team=team, index=1, ssh_gateway_host="worms.example", ssh_gateway_port=2222, ssh_gateway_user="team")
    links = {"a": ["10.128.2.11"], "b": ["10.128.2.12", "10.128.3.12"], "c": ["10.128.3.13"]}
    for n, (role, on_links) in enumerate(links.items()):
        DevboxVM.objects.create(
            devbox=devbox, vm_identifier=f"devbox-t1-{role}", role=role, ssh_fabric_ip=f"10.7.1.{11 + n}",
            vm_maintenance_ip=f"10.6.1.{11 + n}", addresses=on_links if addresses else [],
        )  # fmt: skip
    # With a key: its table is wider than a phone, and that must stay inside the table's own scroll area.
    DevboxSSHKey.objects.create(team=team, name="laptop", public_key="ssh-ed25519 " + "A" * 68 + " someone@laptop")
    page = site.get("/", **proxied(HOST)).content.decode()
    token = page.split('name="csrfmiddlewaretoken" value="', 1)[1].split('"', 1)[0]
    switched = site.post("/environment/", {"csrfmiddlewaretoken": token, "environment": "dev"}, HTTP_ORIGIN=f"https://{HOST}", **proxied(HOST))
    assert switched.status_code == 302
    response = site.get("/", **proxied(HOST))
    assert response.status_code == 200
    return response.content.decode()


def test_the_page_says_where_the_vms_are_and_how_to_go_on_from_a(site, team_upload):  # noqa: F811
    page = _page(site, team_upload[0])
    assert LOGIN in page
    assert "ssh -A root@10.128.2.12" in page  # on A: B's address on the A-B link
    assert "ssh root@10.128.3.13" in page  # on B: C's address on the B-C link
    for address in ("10.128.2.11", "10.128.3.12"):
        assert f'<span class="address">{address}</span>' in page


def test_the_page_shows_no_address_a_team_cannot_use(site, team_upload):  # noqa: F811
    page = _page(site, team_upload[0])
    assert "10.6.1." not in page  # the maintenance network
    assert "10.7.1.12" not in page and "10.7.1.13" not in page  # the gateway only reaches A


def test_vms_built_before_jobs_reported_addresses_still_get_the_login_command(site, team_upload):  # noqa: F811
    page = _page(site, team_upload[0], addresses=False)
    assert LOGIN in page
    assert "data-ssh-hop" not in page
    assert "—" in page


@pytest.fixture
def measured(site, team_upload, tmp_path):  # noqa: F811
    if not _playwright_available():
        pytest.skip("Node.js Playwright not available")
    html = tmp_path / "page.html"
    html.write_text(_page(site, team_upload[0]))
    shots = os.environ.get("WORMS_SCREENSHOTS", "")
    if shots:
        os.makedirs(shots, exist_ok=True)
    run = subprocess.run(["node", str(DRIVER), str(html), str(STATIC), shots], capture_output=True, text=True, cwd=REPO, timeout=120)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_the_copy_button_never_covers_a_command(measured):
    assert measured["errors"] == []
    for width, seen in measured["widths"].items():
        assert len(seen["blocks"]) == 4, width  # agent, A, B, C
        for block in seen["blocks"]:
            assert block["hasButton"], (width, block["text"])
            assert not block["overlaps"], (width, block["text"])
            assert not block["clipped"], (width, block["text"])  # all of it is on screen


def test_copy_gives_exactly_the_command(measured):
    assert measured["copied"] == LOGIN


def test_the_reset_button_has_room_above_it(measured):
    for width, seen in measured["widths"].items():
        assert seen["resetGap"] >= 12, width


def test_the_page_fits_a_phone(measured):
    assert not measured["widths"]["390"]["pageScrolls"]
