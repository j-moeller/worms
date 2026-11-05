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
from test_dashboard_login import HOST, login, proxied, site  # noqa: F401 (fixtures)
from test_dashboard_pages import team_upload  # noqa: F401 (fixture)

pytest.importorskip("django")

STATIC = REPO / "projects" / "dashboard" / "dashboard" / "static"
DRIVER = REPO / "tests" / "browser" / "devbox_page.js"
LOGIN = "ssh -A -J team@worms.example:2222 root@10.7.1.11"


def _playwright_available() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True, cwd=REPO).returncode == 0


def _page(site, team, index=1, addresses=True, captured=()) -> str:  # noqa: F811
    """The dev view of a team, as the reconciler leaves it."""
    from core.models.devbox import Devbox, DevboxCapture, DevboxSSHKey, DevboxVM

    devbox = Devbox.objects.create(team=team, index=index, ssh_gateway_host="worms.example", ssh_gateway_port=2222, ssh_gateway_user="team")
    ab, bc = f"10.128.{2 * index}", f"10.128.{2 * index + 1}"
    links = {"a": [f"{ab}.11"], "b": [f"{ab}.12", f"{bc}.12"], "c": [f"{bc}.13"]}
    for n, (role, on_links) in enumerate(links.items()):
        vm = DevboxVM.objects.create(
            devbox=devbox, vm_identifier=f"devbox-t1-{role}", role=role, ssh_fabric_ip=f"10.7.{index}.{11 + n}",
            vm_maintenance_ip=f"10.6.{index}.{11 + n}", addresses=on_links if addresses else [],
        )  # fmt: skip
        if role in captured:
            DevboxCapture.objects.create(team=team, devbox_vm=vm, devbox_version=team.devbox_version)
    DevboxSSHKey.objects.create(team=team, name="laptop", public_key="ssh-ed25519 " + "A" * 68 + " someone@laptop")
    page = site.get("/", **proxied(HOST)).content.decode()
    token = page.split('name="csrfmiddlewaretoken" value="', 1)[1].split('"', 1)[0]
    switched = site.post("/environment/", {"csrfmiddlewaretoken": token, "environment": "dev"}, HTTP_ORIGIN=f"https://{HOST}", **proxied(HOST))
    assert switched.status_code == 302
    response = site.get("/", **proxied(HOST))
    assert response.status_code == 200
    return response.content.decode()


def _profile(site) -> str:  # noqa: F811
    response = site.get("/profile/", **proxied(HOST))
    assert response.status_code == 200
    return response.content.decode()


def _post(site, path, data=None):  # noqa: F811
    page = _profile(site)
    token = page.split('name="csrfmiddlewaretoken" value="', 1)[1].split('"', 1)[0]
    return site.post(path, {**(data or {}), "csrfmiddlewaretoken": token}, HTTP_ORIGIN=f"https://{HOST}", **proxied(HOST))


def _port(iface, ip) -> str:
    return f'<span class="vm-port__iface">{iface}</span><span class="vm-port__ip">{ip}</span>'


def test_the_page_gives_the_commands_from_the_laptop_to_c(site, team_upload):  # noqa: F811
    page = _page(site, team_upload[0])
    assert LOGIN in page
    assert "ssh -A root@10.128.2.12" in page  # on A: B's address on the A-B link
    assert "ssh root@10.128.3.13" in page  # on B: C's address on the B-C link


def test_the_page_tells_you_to_check_your_agent_and_does_not_start_one(site, team_upload):  # noqa: F811
    page = _page(site, team_upload[0])
    assert "<code>ssh-add -L</code>" in page
    assert "ssh-agent -s" not in page and "id_ed25519" not in page


def test_ssh_keys_are_on_the_profile_and_not_on_the_devbox_page(site, team_upload):  # noqa: F811
    page = _page(site, team_upload[0])
    assert "keys-title" not in page and "/devbox/ssh-keys/" not in page
    assert '<a href="/profile/">your profile</a>' in page
    profile = _profile(site)
    assert 'id="keys-title"' in profile and ">1 registered<" in profile
    assert "someone@laptop" in profile
    assert 'action="/devbox/ssh-keys/add/"' in profile and "/devbox/ssh-keys/" in profile.split("someone@laptop", 1)[1]


def test_adding_and_removing_a_key_come_back_to_the_profile(site, team_upload, monkeypatch):  # noqa: F811
    from core.models.devbox import Devbox, DevboxSSHKey
    from dashboard import orchestrator_client

    team = team_upload[0]
    Devbox.objects.create(team=team, index=1)
    monkeypatch.setattr(orchestrator_client, "post", lambda path, timeout=15: {"reconfigure_enqueued": True})

    key = "ssh-ed25519 " + "B" * 68 + " me@laptop"
    added = _post(site, "/devbox/ssh-keys/add/", {"name": "laptop", "public_key": key})
    assert (added.status_code, added["Location"]) == (302, "/profile/")
    profile = _profile(site)
    assert "SSH key added." in profile and "me@laptop" in profile

    refused = _post(site, "/devbox/ssh-keys/add/", {"name": "bad", "public_key": "not a key"})
    assert (refused.status_code, refused["Location"]) == (302, "/profile/")
    assert "not a single-line" in _profile(site)

    removed = _post(site, f"/devbox/ssh-keys/{DevboxSSHKey.objects.get(team=team).id}/delete/")
    assert (removed.status_code, removed["Location"]) == (302, "/profile/")
    profile = _profile(site)
    assert "me@laptop" not in profile and ">0 registered<" in profile


def test_someone_without_a_team_gets_no_keys_panel(site):  # noqa: F811
    assert login(site, HOST, f"https://{HOST}").status_code == 302
    assert "keys-title" not in _profile(site)


def test_the_picture_joins_the_vms_interface_to_interface(site, team_upload):  # noqa: F811
    page = _page(site, team_upload[0])
    assert "<table" not in page.split('id="vms-title"', 1)[1].split("</section>", 1)[0]
    # In the order of the chain: the gateway reaches A on the SSH fabric, then link by link.
    order = [_port(*port) for port in (
        ("eth1", "10.7.1.11"), ("eth0", "10.128.2.11"), ("eth0", "10.128.2.12"), ("eth1", "10.128.3.12"), ("eth0", "10.128.3.13"),
    )]  # fmt: skip
    positions = [page.index(port) for port in order]
    assert positions == sorted(positions)
    assert page.index("worms.example:2222</span>") < positions[0]


def test_a_captured_vm_says_so_and_only_that_vm(site, team_upload):  # noqa: F811
    page = _page(site, team_upload[0], captured={"b"})
    nodes = {node.split('"', 1)[0]: node for node in page.split('data-vm="')[1:]}
    assert ">Captured<" in nodes["B"] and ">Not captured<" not in nodes["B"].split("</li>")[0]
    for letter in "AC":
        assert ">Not captured<" in nodes[letter] and ">Captured<" not in nodes[letter].split("</li>")[0]
    assert page.count("is-captured") == 1
    assert "1 of 3 captured" in page


def test_the_page_shows_no_address_a_team_cannot_use(site, team_upload):  # noqa: F811
    page = _page(site, team_upload[0])
    assert "10.6.1." not in page  # the maintenance network
    assert "10.7.1.12" not in page and "10.7.1.13" not in page  # the gateway only reaches A


def test_vms_built_before_jobs_reported_addresses_still_get_the_login_command(site, team_upload):  # noqa: F811
    page = _page(site, team_upload[0], addresses=False)
    assert LOGIN in page
    assert "data-ssh-hop" not in page
    assert '<span class="vm-port__ip">—</span>' in page


@pytest.fixture
def measured(site, team_upload, tmp_path):  # noqa: F811
    if not _playwright_available():
        pytest.skip("Node.js Playwright not available")
    html = tmp_path / "page.html"
    # The highest index has the longest addresses, so the least room.
    html.write_text(_page(site, team_upload[0], index=31, captured={"a", "c"}))
    profile = tmp_path / "profile.html"
    profile.write_text(_profile(site))
    shots = os.environ.get("WORMS_SCREENSHOTS", "")
    if shots:
        os.makedirs(shots, exist_ok=True)
    run = subprocess.run(["node", str(DRIVER), str(html), str(profile), str(STATIC), shots], capture_output=True, text=True, cwd=REPO, timeout=120)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


WIDE, NARROW = ("1280", "1101"), ("1100", "900", "390")


def test_the_copy_button_never_covers_a_command(measured):
    assert measured["errors"] == []
    for width, seen in measured["widths"].items():
        assert len(seen["blocks"]) == 3, width  # A, B, C
        for block in seen["blocks"]:
            assert block["hasButton"], (width, block["text"])
            assert not block["overlaps"], (width, block["text"])
            assert not block["clipped"], (width, block["text"])  # all of it is on screen


def test_copy_gives_exactly_the_command(measured):
    assert measured["copied"] == "ssh -A -J team@worms.example:2222 root@10.7.31.11"


def test_the_reset_button_has_room_above_it(measured):
    for width, seen in measured["widths"].items():
        assert seen["resetGap"] >= 12, width


def test_the_pages_fit_a_phone(measured):
    assert not measured["widths"]["390"]["pageScrolls"]
    # The keys table is wider than a phone: that must stay inside its own scroll area.
    assert measured["profile"]["390"]["keysPanel"] and not measured["profile"]["390"]["pageScrolls"]


def test_every_box_holds_its_text(measured):
    for width, seen in measured["widths"].items():
        assert len(seen["nodes"]) == 4, width  # the gateway, A, B, C
        for node in seen["nodes"]:
            assert not node["portsOverlap"], (width, node)
            assert not node["spills"], (width, node)
            assert not node["ipClipped"], (width, node)


def test_the_chain_runs_across_a_wide_screen_on_one_line(measured):
    for width in WIDE:
        nodes = measured["widths"][width]["nodes"]
        assert [n["left"] for n in nodes] == sorted(n["left"] for n in nodes), width
        assert len({n["top"] for n in nodes}) == 1, width
        for node in nodes:
            assert abs(node["portsMiddle"] - node["middle"]) <= 1, (width, node)  # the wires meet the ports


def test_the_chain_runs_downwards_when_it_is_too_narrow(measured):
    for width in NARROW:
        nodes = measured["widths"][width]["nodes"]
        assert [n["top"] for n in nodes] == sorted(n["top"] for n in nodes), width
        assert len({n["left"] for n in nodes}) == 1, width


def test_a_captured_box_looks_different(measured):
    nodes = measured["widths"]["1280"]["nodes"]
    a, b, c = nodes[1], nodes[2], nodes[3]
    assert a["border"] == c["border"] != b["border"]
    assert a["background"] == c["background"] != b["background"]
