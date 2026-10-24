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
