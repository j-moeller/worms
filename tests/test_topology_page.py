"""The TOPOLOGY page in a real browser (Chromium via Node Playwright).

The page (templates/topology/player.html + topology/app.js) is loaded with
fixture data (tests/topology_fixtures.py) in place of the three JSON
endpoints, and driven by tests/browser/topology_player.js: the live layout and
hover cards, the round/team tabs, playback (dots), event badges (click pauses
and shows details), VM power state and captures over time, clicking a dot, the
attacker-controlled log rendered as text, and a run that fails to load.

Skipped when Node.js or its Playwright package is not available. Set
WORMS_SCREENSHOTS=<dir> to keep the screenshots.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
import topology_fixtures
from conftest import REPO

TEMPLATES = REPO / "projects" / "dashboard" / "dashboard" / "templates"
DRIVER = REPO / "tests" / "browser" / "topology_player.js"


def _playwright_available() -> bool:
    if not shutil.which("node"):
        return False
    probe = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True, cwd=REPO)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _playwright_available(), reason="Node.js Playwright not available")


def page_html() -> str:
    player = (TEMPLATES / "topology" / "player.html").read_text()
    app = (TEMPLATES / "topology" / "app.js").read_text()
    # The same structure as templates/topology.html renders, with the
    # endpoints pointing at the fixtures.
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>topology</title></head><body>"
        "<div class='topo' id='topo' data-model='/model' data-runs='/runs' data-refresh-ms='700' "
        "data-timeline='/rounds/00000000-0000-0000-0000-000000000000/timeline/'>"
        f"{player}</div><script>{app}</script></body></html>"
    )


def test_the_page_has_no_template_syntax_collisions():
    """app.js and player.html are pulled in with {% include %}, so Django
    would interpret any template syntax inside them."""
    for name in ("app.js", "player.html"):
        text = (TEMPLATES / "topology" / name).read_text()
        for token in ("{%", "{{", "{#"):
            assert token not in text, (name, token)


@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    work = tmp_path_factory.mktemp("topology-page")
    (work / "page.html").write_text(page_html())
    (work / "model.json").write_text(json.dumps(topology_fixtures.model()))
    (work / "runs.json").write_text(json.dumps(topology_fixtures.runs()))
    (work / "timeline.json").write_text(json.dumps(topology_fixtures.timeline()))
    shots = work / "shots"
    shots.mkdir()
    proc = subprocess.run(
        ["node", str(DRIVER), str(work / "page.html"), str(work), str(shots)],
        capture_output=True, text=True, timeout=180, cwd=REPO,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, proc.stdout + proc.stderr
    result = json.loads(lines[-1])
    assert "fatal" not in result, (result["fatal"], result.get("errors"))
    keep = os.environ.get("WORMS_SCREENSHOTS")
    if keep:
        os.makedirs(keep, exist_ok=True)
        for shot in shots.iterdir():
            shutil.copy(shot, keep)
    return result


def test_no_script_errors(observed):
    # The one failed request is the run that is made to fail (404) below.
    failed = [e for e in observed["errors"] if "Failed to load resource" in e]
    assert len(failed) == 1 and "404" in failed[0]
    assert [e for e in observed["errors"] if e not in failed] == []


def test_live_layout_follows_the_spec(observed):
    live = observed["checks"]["live"]
    # Top row: Proxmox host left, orchestrator centre, dashboard right.
    assert live["proxmox"]["x"] < live["orchestrator"]["x"] < live["dashboard"]["x"]
    assert live["proxmox"]["y"] == live["orchestrator"]["y"] == live["dashboard"]["y"]
    # Database centred below; scoring and logging below it.
    assert live["database"]["x"] == live["orchestrator"]["x"] and live["database"]["y"] > live["orchestrator"]["y"]
    assert live["scoring"]["y"] == live["logging"]["y"] > live["database"]["y"]
    assert live["scoring"]["x"] < live["database"]["x"] < live["logging"]["x"]
    # Then the devboxes and the sandbox, the private internet to their right.
    assert live["sandbox"]["y"] > live["scoring"]["y"]
    assert live["pi"]["x"] > live["sandbox"]["x"] + live["sandbox"]["w"] - 1
    assert live["devboxes"] == ["group:devbox:t1", "group:devbox:t2"]
    # The database's ingress, as its firewall says.
    assert live["dbSources"] == ["dashboard", "logging", "monitor", "orchestrator", "scoring", "worms-pve"]
    assert {"db", "lan devbox", "lan_hub", "service", "service pi"} <= set(live["edgeKinds"])


def test_live_status_and_hover_card(observed):
    checks = observed["checks"]
    assert checks["inactive"] == 1  # devpi is stopped in the fixture
    tip = checks["dbTip"]
    assert "database" in tip and "security groups" in tip.lower() and "vmbr21" in tip
    assert "vmbr2 (management — not drawn)" in tip
    side = checks["dbSide"]
    assert "rules (" in side.lower() and "5432" in side


def test_round_and_team_tabs_load_the_run(observed):
    checks = observed["checks"]
    assert checks["teamTabs"][0].startswith("team-a") and checks["teamTabs"][1].startswith("team-b")
    assert checks["playerVisible"]
    rnd = checks["round"]
    assert rnd["sandboxVMs"] == 6
    assert rnd["devboxes"] == ["group:devbox:t1"]  # only the selected team's devbox
    assert checks["marks"] > 5


def test_playback_moves_time_and_draws_dots(observed):
    checks = observed["checks"]
    assert checks["afterPlay"]["dots"] > 0 and checks["afterPlay"]["playing"]
    assert checks["afterPause"]["playing"] is False


def test_event_badges_pause_and_show_details(observed):
    checks = observed["checks"]
    assert abs(checks["at95"]["t"] - 95.4) < 1.5
    assert checks["badges95"] >= 1
    assert "captured" in checks["capturedClass"]
    assert checks["afterBadge"]["playing"] is False and checks["afterBadge"]["selected"] == "event"
    assert "Continue" in checks["badgeSide"]


def test_attacker_controlled_log_is_shown_as_text(observed):
    checks = observed["checks"]
    assert topology_fixtures.XSS in checks["logSide"]
    assert checks["xssFired"] is False and checks["imgInSide"] == 0


def test_vm_power_state_follows_the_timeline(observed):
    checks = observed["checks"]
    assert "inactive" in checks["stoppedClass"].split()
    assert "inactive" not in checks["restartedClass"].split()


def test_clicking_a_dot_pauses_and_shows_the_connection(observed):
    checks = observed["checks"]
    assert checks["afterDot"]["selected"] == "flow" and checks["afterDot"]["playing"] is False
    assert "Zeek uid" in checks["dotSide"]


def test_next_event_and_shareable_hash(observed):
    checks = observed["checks"]
    assert checks["afterNext"]["selected"] == "event"
    assert checks["hash"].startswith(f"#run={topology_fixtures.RUN_UUID}&t=")


def test_live_refresh_keeps_speed_and_zoom(observed):
    refresh = observed["checks"]["refresh"]
    assert refresh["modelFetches"] > refresh["fetchesBefore"]  # it did refresh
    assert refresh["speed"] == 60
    assert abs(refresh["k"] - refresh["k0"]) < 1e-9


def test_failed_run_and_back_to_live(observed):
    checks = observed["checks"]
    assert "Could not load the run" in checks["errorText"]
    assert checks["backLive"]["mode"] == "live" and checks["playerHiddenLive"]


# --- the replay bundle: download, offline viewer, broken and hostile files ----

REPLAY_DRIVER = REPO / "tests" / "browser" / "topology_replay.js"
FORMAT = "worms-topology-replay"
XSS = topology_fixtures.XSS


def hostile_bundle() -> dict:
    """Valid envelope, everything inside odd or malicious."""
    return {
        "format": FORMAT, "version": 1, "exported_at": "not a date", "source": {"mode": {"evil": 1}},
        "model": {
            "host": {"node": XSS, "errors": [{"path": XSS, "error": 5}], "firewall_services": []},
            "nodes": [
                {"id": "vm:1", "name": XSS, "role": "__proto__", "status": XSS, "active": "yes", "aux": True,
                 "addresses": [XSS, {"x": 1}], "nics": [{"name": XSS, "bridge": 7, "kind": XSS}],
                 "firewall": {"groups": [{"name": XSS, "iface": XSS}], "rules": [{"direction": "in", "action": XSS, "source": XSS}]}},
                {"id": "vm:2", "name": "database", "role": "database", "active": True},
                "junk", None, 5,
            ],
            "groups": [{"id": "group:sandbox", "kind": "sandbox", "members": ["s1"]}, {"id": "x", "kind": "<script>"}],
            "networks": [{"id": "net:x", "kind": XSS, "members": ["s1"]}],
            "edges": [{"id": XSS, "source": "vm:1", "target": "vm:2", "kind": "db", "verified": "true"}],
            "security_groups": {"__proto__": {"polluted": True}, "constructor": {"rules": []},
                                "sandbox": {"rules": [{"direction": "in", "action": "ACCEPT", "source": XSS}]}},
        },
        "timeline": {
            "run": {"team": XSS, "round": XSS, "t_min": "abc", "t_max": 1e30, "duration": 100, "team_slug": XSS},
            "nodes": [{"id": "s1", "label": XSS, "networks": [{"name": "vmbr1000", "cidr": XSS}], "addresses": ["10.64.0.1"]}],
            "networks": [{"id": "vmbr1000", "label": XSS, "cidr": XSS}],
            "edges": [{"source": "s1", "target": "vmbr1000", "network": "vmbr1000"}],
            "vm_status": {"__proto__": [[0, "running"]], "s1": [[0, "running"], ["x", "stopped"], [50, XSS]]},
            "events": [
                {"t": 5, "kind": "log_received", "node": "role:logging", "vm": "s1",
                 "data": {"log": {"data": XSS}, "source_vm": "s1", "source_label": XSS}},
                {"t": "NaN", "kind": "state"}, {"t": 1e20, "kind": "state"},
                {"t": "12", "kind": XSS, "node": "s1", "data": "not an object"}, None,
            ],
            "flows": [{"t": 1, "d": -5, "src": "s1", "dst": "vm:2", "src_name": XSS, "dst_name": XSS, "kind": XSS, "conn_state": XSS},
                      {"t": "x"}],
        },
    }


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    work = tmp_path_factory.mktemp("topology-replay")
    # A small file limit, so the "too large" path can be tested cheaply.
    html = page_html().replace("data-refresh-ms='700'", "data-refresh-ms='600000' data-max-file-bytes='200000'")
    (work / "page.html").write_text(html)
    (work / "model.json").write_text(json.dumps(topology_fixtures.model()))
    (work / "runs.json").write_text(json.dumps(topology_fixtures.runs()))
    (work / "timeline.json").write_text(json.dumps(topology_fixtures.timeline()))
    (work / "notjson.json").write_text("{this is not json")
    (work / "wrongformat.json").write_text(json.dumps({"format": "something-else", "version": 1}))
    (work / "version99.json").write_text(json.dumps({"format": FORMAT, "version": 99, "model": {"nodes": []}}))
    (work / "nodesnotlist.json").write_text(json.dumps({"format": FORMAT, "version": 1, "model": {"nodes": {}}}))
    (work / "big.json").write_text(json.dumps({"format": FORMAT, "version": 1, "pad": "x" * 300000}))
    (work / "hostile.json").write_text(json.dumps(hostile_bundle()))
    out = work / "out"
    out.mkdir()
    proc = subprocess.run(["node", str(REPLAY_DRIVER), str(work / "page.html"), str(work), str(out)],
                          capture_output=True, text=True, timeout=180, cwd=REPO)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, proc.stdout + proc.stderr
    result = json.loads(lines[-1])
    assert "fatal" not in result, (result["fatal"], result.get("errors"), result.get("checks"))
    keep = os.environ.get("WORMS_SCREENSHOTS")
    if keep:
        os.makedirs(keep, exist_ok=True)
        for shot in out.glob("*.png"):
            shutil.copy(shot, keep)
    return result


def test_replay_no_script_errors(replayed):
    assert replayed["errors"] == []


def test_download_is_a_self_contained_bundle(replayed):
    d = replayed["checks"]["download"]
    assert d["keys"] == ["exported_at", "format", "model", "source", "timeline", "version"]
    assert d["format"] == FORMAT and d["version"] == 1
    assert d["source"] == {"mode": "round", "run": topology_fixtures.RUN_UUID, "from_file": None}
    fixture = topology_fixtures.timeline()
    assert d["flows"] == len(fixture["flows"]) and d["events"] == len(fixture["events"]) and d["sandboxVMs"] == 6
    assert d["modelNodes"] == len(topology_fixtures.model()["nodes"])
    for key in ("nodes", "edges", "groups", "networks", "security_groups", "host"):
        assert key in d["modelKeys"]
    assert d["replayName"].startswith("worms-R1-team-a-") and d["replayName"].endswith(".json")
    # From the live tab: the infrastructure only.
    assert d["infraTimeline"] is None and d["infraNodes"] == d["modelNodes"]
    assert d["infraName"].startswith("worms-infrastructure-")


def test_opened_file_is_viewed_offline(replayed):
    c = replayed["checks"]
    assert c["offlineBefore"]["mode"] == "live"  # every endpoint failed
    assert c["opened"]["mode"] == "file" and c["opened"]["timeline"] and c["opened"]["sandboxVMs"] == 6
    assert c["fileTab"].startswith("File: replay.json")
    assert "viewed locally" in c["meta"]
    assert c["playerVisible"]
    assert c["playing"]["dots"] > 0
    assert c["badge"]["selected"] == "event" and c["badge"]["playing"] is False
    assert "Continue" in c["badgeSide"]
    assert c["requestsWhileViewing"] == []  # nothing fetched for the file


def test_bundle_round_trip_is_lossless(replayed):
    rt = replayed["checks"]["roundTrip"]
    assert rt["model"] and rt["timeline"]
    assert rt["source"] == {"mode": "file", "run": topology_fixtures.RUN_UUID, "from_file": "replay.json"}


def test_infrastructure_only_file(replayed):
    c = replayed["checks"]
    assert c["infraOpened"]["mode"] == "file" and not c["infraOpened"]["timeline"] and c["infraOpened"]["nodes"] > 10
    assert c["infraPlayerHidden"]


def test_broken_files_are_refused_and_the_view_stays(replayed):
    broken = replayed["checks"]["broken"]
    expect = {
        "notjson.json": "not valid JSON",
        "wrongformat.json": "not a WORMS topology file",
        "version99.json": "unsupported version 99",
        "nodesnotlist.json": "model.nodes must be a list",
        "big.json": "too large",
    }
    for name, text in expect.items():
        assert text in broken[name]["banner"], (name, broken[name]["banner"])
        assert broken[name]["state"]["file"] == "infrastructure.json"  # still the previous file


def test_hostile_file_is_contained(replayed):
    h = replayed["checks"]["hostile"]
    assert h["state"]["mode"] == "file" and h["banner"]
    assert h["xss"] is False and h["imgs"] == 0 and h["protoPolluted"] is False
    assert XSS in h["svgText"]  # shown, as text
    assert h["events"] == [5, 12]  # NaN / 1e20 / null dropped, "12" coerced
    assert h["roles"] == ["other", "database"]  # unknown role -> other; junk entries dropped
    assert replayed["checks"]["hostilePlayed"]["mode"] == "file"
