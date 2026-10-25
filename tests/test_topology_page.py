"""The round player in a real browser (Chromium via Node Playwright): the
TOPOLOGY page (staff) and the PROGRESSION page (a team).

The page (templates/topology/player.html + static/dashboard/js/topology.js) is
loaded with fixture data (tests/topology_fixtures.py) in place of the JSON
endpoints, and driven by tests/browser/:
- topology_player.js: the live layout and hover cards, the round/team tabs,
  playback (dots), event badges (click pauses and shows details), VM power
  state and captures over time, clicking a dot, the attacker-controlled log
  record fetched on demand and rendered as text, a run that fails to load;
- topology_replay.js: downloading what is shown, opening .json and .zip files
  offline (log records read from the zip), legacy, broken and hostile files;
- progression_page.js: the team's page -- its scene only (no infrastructure,
  no firewall), VMs faint until captured, lazy flows and log records, the
  round's clock, the zip download, a Dev round without network data.

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
STATIC = REPO / "projects" / "dashboard" / "dashboard" / "static" / "dashboard"
DRIVER = REPO / "tests" / "browser" / "topology_player.js"


def _playwright_available() -> bool:
    if not shutil.which("node"):
        return False
    probe = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True, cwd=REPO)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _playwright_available(), reason="Node.js Playwright not available")


ZERO = "00000000-0000-0000-0000-000000000000"
STAFF_DATA = (
    "data-audience='staff' data-model='/model' data-runs='/runs' data-refresh-ms='700' "
    f"data-progression='/rounds/{ZERO}/progression/' data-download='/rounds/{ZERO}/progression/download/'"
)
TEAM_DATA = (
    "hidden data-audience='team' data-environment='live' data-round='1' data-rounds='/progression/rounds/' "
    "data-progression='/progression/rounds/0/' data-download='/progression/rounds/0/download/' data-refresh-ms='600000'"
)


def page_html(data=STAFF_DATA) -> str:
    # What templates/topology.html (or progression.html) serves: the
    # markup-only player partial, the topology stylesheet and script from
    # static/, endpoints pointing at the fixtures.
    player = (TEMPLATES / "topology" / "player.html").read_text()
    css = (STATIC / "css" / "topology.css").read_text()
    app = (STATIC / "js" / "topology.js").read_text()
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>topology</title>"
        f"<style>{css}</style></head><body>"
        "<nav data-progression-tabs>server-rendered round links</nav><section data-progression-summary>summary</section>"
        f"<div class='topo' id='topo' {data}>"
        f"{player}</div><script>{app}</script></body></html>"
    )


def write_fixtures(work):
    (work / "model.json").write_text(json.dumps(topology_fixtures.model()))
    (work / "runs.json").write_text(json.dumps(topology_fixtures.runs()))
    (work / "progression.json").write_text(json.dumps(topology_fixtures.progression()))
    (work / "flows.json").write_text(json.dumps(topology_fixtures.flows()))
    (work / "log.json").write_text(json.dumps(topology_fixtures.log_record()))


def test_the_player_partial_is_template_free():
    """player.html is pulled in with {% include %}, so Django would interpret
    any template syntax inside it; the moved-out JS/CSS must also be clean."""
    files = [TEMPLATES / "topology" / "player.html", STATIC / "js" / "topology.js", STATIC / "css" / "topology.css"]
    for path in files:
        text = path.read_text()
        for token in ("{%", "{{", "{#"):
            assert token not in text, (path.name, token)


@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    work = tmp_path_factory.mktemp("topology-page")
    (work / "page.html").write_text(page_html())
    write_fixtures(work)
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
    # The record was fetched when the event was opened, once.
    assert checks["logFetches"] == 1


def test_flows_are_fetched_after_the_progression(observed):
    checks = observed["checks"]
    assert checks["flowsLoaded"] == len(topology_fixtures.flows()["flows"])
    assert checks["requests"].index("progression") < checks["requests"].index("flows")


def test_zip_download_link_follows_the_options(observed):
    links = observed["checks"]["zipLinks"]
    run = topology_fixtures.RUN_UUID
    assert links["full"] == f"/rounds/{run}/progression/download/?logs=1&network=1"
    assert links["lean"] == f"/rounds/{run}/progression/download/?logs=0&network=0"
    assert links["live"] is None  # nothing to download from the server on the live tab


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


# --- files: download, offline viewer, zips, legacy, broken and hostile files ----

REPLAY_DRIVER = REPO / "tests" / "browser" / "topology_replay.js"
FORMAT = "worms-progression"
LEGACY = "worms-topology-replay"
XSS = topology_fixtures.XSS


def legacy_bundle() -> dict:
    """A file saved by the earlier TOPOLOGY view: model + timeline, flows and
    log records inline."""
    tl = topology_fixtures.timeline()
    for e in tl["events"]:
        if e["kind"] == "log_received":
            e["data"]["log"] = {"data": {"msg": XSS}, "truncated": False}
    return {"format": LEGACY, "version": 1, "exported_at": "2026-10-24T12:00:00+00:00",
            "source": {"mode": "round", "run": topology_fixtures.RUN_UUID, "from_file": None},
            "model": topology_fixtures.model(), "timeline": tl}


def hostile_bundle() -> dict:
    """A legacy envelope, everything inside odd or malicious."""
    return {
        "format": LEGACY, "version": 1, "exported_at": "not a date", "source": {"mode": {"evil": 1}},
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


def hostile_progression() -> dict:
    """A progression file pointing at other servers: a file's URLs are never
    fetched (its log records are only ever read from its own zip)."""
    doc = topology_fixtures.progression()
    doc["logs"]["url"] = "https://evil.example/{ref}"
    doc["network"]["url"] = "//evil.example/flows"
    doc["round"]["name"] = XSS
    return doc


def zip_bytes(members: dict, lie=None) -> bytes:
    """A zip of {name: text}. lie=(name, size): the central directory claims
    that member is `size` bytes uncompressed (a crafted zip)."""
    import io
    import struct
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    data = bytearray(buffer.getvalue())
    if lie:
        name, size = lie
        p = data.rfind(b"PK\x05\x06")
        p = struct.unpack_from("<I", data, p + 16)[0]
        while data[p:p + 4] == b"PK\x01\x02":
            n, extra, comment = struct.unpack_from("<HHH", data, p + 28)
            if data[p + 46:p + 46 + n].decode() == name:
                struct.pack_into("<I", data, p + 24, size)
            p += 46 + n + extra + comment
    return bytes(data)


def progression_zip(**lie) -> bytes:
    """What the server's download holds (orchestrator/rounds/progression.package):
    progression.json, logs/<ref>.json, network/flows.jsonl."""
    doc = topology_fixtures.progression()
    doc["logs"].update(included=True, path="logs/{ref}.json", url=None)
    doc["network"].update(included=True, path="network/flows.jsonl", url=None)
    flows = topology_fixtures.flows()["flows"]
    return zip_bytes({
        "progression.json": json.dumps(doc),
        "README.txt": "WORMS round progression",
        f"logs/{topology_fixtures.LOG_REF}.json": json.dumps(topology_fixtures.log_record()),
        "network/flows.jsonl": "".join(json.dumps(f) + "\n" for f in flows),
    }, **lie)


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    work = tmp_path_factory.mktemp("topology-replay")
    # A small file limit, so the "too large" path can be tested cheaply.
    html = page_html().replace("data-refresh-ms='700'", "data-refresh-ms='600000' data-max-file-bytes='200000'")
    (work / "page.html").write_text(html)
    write_fixtures(work)
    (work / "notjson.json").write_text("{this is not json")
    (work / "wrongformat.json").write_text(json.dumps({"format": "something-else", "version": 1}))
    (work / "version99.json").write_text(json.dumps({"format": FORMAT, "version": 99, "scene": {"nodes": []}}))
    (work / "nodesnotlist.json").write_text(json.dumps({"format": FORMAT, "version": 1, "scene": {"nodes": {}}}))
    (work / "big.json").write_text(json.dumps({"format": FORMAT, "version": 1, "pad": "x" * 300000}))
    (work / "hostile.json").write_text(json.dumps(hostile_bundle()))
    (work / "legacy.json").write_text(json.dumps(legacy_bundle()))
    (work / "elsewhere.json").write_text(json.dumps(hostile_progression()))
    (work / "round.zip").write_bytes(progression_zip())
    (work / "nodoc.zip").write_bytes(zip_bytes({"README.txt": "nothing here"}))
    (work / "liar.zip").write_bytes(progression_zip(lie=("network/flows.jsonl", 10)))
    (work / "huge.zip").write_bytes(progression_zip(lie=("progression.json", 2_000_000_000)))
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


def test_download_is_a_self_contained_progression(replayed):
    d = replayed["checks"]["download"]
    assert d["keys"] == ["audience", "clock", "environment", "format", "generated_at", "logs", "network", "round", "scene",
                         "team", "timeline", "version"]
    assert d["format"] == FORMAT and d["version"] == 1 and d["audience"] == "staff"
    fixture = topology_fixtures.timeline()
    assert d["flows"] == len(fixture["flows"]) and d["events"] == len(fixture["events"]) and d["sandboxVMs"] == 6
    # The flows are inline (they were loaded); log records stay references.
    assert d["network"]["included"] is True and d["network"]["url"] is None
    assert d["logs"]["included"] is False and d["logs"]["url"] is None
    assert d["sceneNodes"] == len(topology_fixtures.model()["nodes"])
    for key in ("nodes", "edges", "groups", "networks", "security_groups", "host"):
        assert key in d["sceneKeys"]
    assert d["replayName"].startswith("worms-R1-team-a-live-") and d["replayName"].endswith(".json")
    # From the live tab: the infrastructure only.
    assert d["infraTimeline"] is None and d["infraNodes"] == d["sceneNodes"]
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


def test_file_round_trip_is_lossless(replayed):
    rt = replayed["checks"]["roundTrip"]
    assert rt == {"scene": True, "timeline": True, "clock": True, "round": True, "team": True}


def test_zip_is_viewed_offline_with_its_log_records(replayed):
    z = replayed["checks"]["zip"]
    assert z["state"]["mode"] == "file" and z["state"]["timeline"] and z["state"]["sandboxVMs"] == 6
    assert z["flows"] == len(topology_fixtures.flows()["flows"])  # from network/flows.jsonl
    assert XSS in z["log"] and z["xss"] is False and z["imgs"] == 0  # from logs/<ref>.json, as text
    assert z["requests"] == []


def test_legacy_file_still_opens(replayed):
    lg = replayed["checks"]["legacy"]
    assert lg["state"]["mode"] == "file" and lg["state"]["sandboxVMs"] == 6
    assert lg["flows"] == len(topology_fixtures.timeline()["flows"])
    assert XSS in lg["log"]  # inline in the old format


def test_a_files_urls_are_never_fetched(replayed):
    e = replayed["checks"]["elsewhere"]
    assert e["state"]["mode"] == "file"
    assert "no log records" in e["log"]
    assert e["evilRequests"] == [] and e["xss"] is False


def test_infrastructure_only_file(replayed):
    c = replayed["checks"]
    assert c["infraOpened"]["mode"] == "file" and not c["infraOpened"]["timeline"] and c["infraOpened"]["nodes"] > 10
    assert c["infraPlayerHidden"]


def test_broken_files_are_refused_and_the_view_stays(replayed):
    broken = replayed["checks"]["broken"]
    expect = {
        "notjson.json": "neither a zip nor valid JSON",
        "wrongformat.json": "not a WORMS progression",
        "version99.json": "unsupported version 99",
        "nodesnotlist.json": "model.nodes must be a list",
        "big.json": "too large",
        "nodoc.zip": "no progression.json in the zip",
        "liar.zip": "larger than the zip says",
        "huge.zip": "too large",
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


# --- the PROGRESSION page: a team's view ------------------------------------------

TEAM_DRIVER = REPO / "tests" / "browser" / "progression_page.js"
# What a team must never see in its view (docs/progression.md).
INTERNALS = ["vmbr", "sandbox-gw", "devbox-gw", "worms-pve", "Proxmox", "database", "orchestrator", "dashboard", "monitor",
             "wazuh", "Firewall", "firewall", "Security groups", "Rules", "NICs", "VMID", "10.4.0.", "sandbox-vm2"]


@pytest.fixture(scope="module")
def team_page(tmp_path_factory):
    work = tmp_path_factory.mktemp("progression-page")
    (work / "page.html").write_text(page_html(TEAM_DATA))
    (work / "team_rounds.json").write_text(json.dumps(topology_fixtures.team_rounds()))
    (work / "team1.json").write_text(json.dumps(topology_fixtures.team_progression(1)))
    (work / "team3.json").write_text(json.dumps(topology_fixtures.team_progression(3)))
    (work / "team_flows.json").write_text(json.dumps(topology_fixtures.team_flows()))
    (work / "log.json").write_text(json.dumps(topology_fixtures.log_record()))
    shots = work / "shots"
    shots.mkdir()
    proc = subprocess.run(["node", str(TEAM_DRIVER), str(work / "page.html"), str(work), str(shots)],
                          capture_output=True, text=True, timeout=180, cwd=REPO)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, proc.stdout + proc.stderr
    result = json.loads(lines[-1])
    assert "fatal" not in result, (result["fatal"], result.get("errors"), result.get("checks"))
    keep = os.environ.get("WORMS_SCREENSHOTS")
    if keep:
        os.makedirs(keep, exist_ok=True)
        for shot in shots.iterdir():
            shutil.copy(shot, keep)
    return result


def test_team_page_no_script_errors(team_page):
    # The one failed request is the upcoming round (404) below.
    failed = [e for e in team_page["errors"] if "Failed to load resource" in e]
    assert len(failed) == 1 and "404" in failed[0]
    assert [e for e in team_page["errors"] if e not in failed] == []


def test_team_page_replaces_the_no_script_parts(team_page):
    c = team_page["checks"]
    assert c["serverPartsHidden"] and c["playerShown"]
    assert c["title"] == "// PROGRESSION" and c["auxHidden"]
    assert [t.split("\n")[0].strip() for t in c["tabs"]] == ["R1", "R2", "Dev week"]
    assert "Infrastructure" not in " ".join(c["tabs"])
    assert c["liveState"]["round"] == 1 and c["liveState"]["flowsLoaded"]
    # The progression first, its connections after it; no staff endpoint.
    assert c["requestsAtLoad"][:3] == ["/progression/rounds/", "/progression/rounds/1/", "/progression/rounds/1/flows/"]


def test_team_scene_has_no_infrastructure(team_page):
    live = team_page["checks"]["live"]
    assert live["team"] is True
    assert set(live["roles"]) <= {"scoring", "logging", "dns", "private_internet", "sandbox", "anchor"}
    assert live["groups"] == ["group:sandbox", "group:pi"]
    assert live["vms"] == ["sandbox-vm0", "sandbox-vm1"]  # what it captured
    assert live["unverified"] == 0
    for text in ("svgText", "legend", "summary", "vmTip", "scoringTip", "anchorTip", "vmSide", "scoringSide"):
        for word in INTERNALS:
            assert word not in team_page["checks"][text], (text, word)


def test_team_vm_is_faint_until_captured(team_page):
    c = team_page["checks"]
    assert "uncaptured" in c["at30"]["vm0"].split() and "uncaptured" in c["at30"]["vm1"].split()
    assert "uncaptured" not in c["at100"]["vm0"].split() and "captured" in c["at100"]["vm0"].split()
    assert "captured" in c["at100"]["vm1"].split()


def test_team_playback_log_and_download(team_page):
    c = team_page["checks"]
    assert c["played"]["dots"] > 0
    assert topology_fixtures.XSS in c["logSide"] and c["xss"] is False and c["imgs"] == 0
    assert c["logRequests"] == [f"/progression/rounds/1/logs/{topology_fixtures.LOG_REF}/"]
    assert c["zip"] == {"full": "/progression/rounds/1/download/?logs=1&network=1",
                        "noNetwork": "/progression/rounds/1/download/?logs=1&network=0"}
    assert c["hash"].startswith("#round=1&t=")
    assert "Live" in c["summary"] and "2 captured" in c["summary"]
    assert "the round is running" in c["meta"]


def test_team_round_that_fails_to_load(team_page):
    assert "Could not load the round" in team_page["checks"]["errorText"]


def test_team_dev_round_shows_the_devbox_without_traffic(team_page):
    c = team_page["checks"]
    dev = c["dev"]
    assert "group:sandbox" not in dev["groups"] and "group:devbox:own" in dev["groups"]
    assert dev["devboxMembers"] == ["devbox-t1-a", "devbox-t1-b"]
    assert "lan devbox" in dev["edgeKinds"]
    assert c["devRequests"] == ["/progression/rounds/3/"]  # no flows to fetch
    assert "not recorded" in c["devSummary"] and "Dev" in c["devSummary"]
    assert c["devNetworkDisabled"] and c["devZip"] == "/progression/rounds/3/download/?logs=1&network=0"
    # The round's start and now are on the track.
    assert any("edge" in m for m in c["devMarks"]) and any("now" in m for m in c["devMarks"])
    # At 5000 s, A was captured (3600 s), B not yet (9000 s).
    classes = dict(c["devAt5000"])
    assert "uncaptured" not in classes["devbox-t1-a"].split() and "uncaptured" in classes["devbox-t1-b"].split()


def test_running_round_refreshes_in_place(team_page):
    r = team_page["checks"]["refresh"]
    assert r["restored"] == {"round": 3, "t": 6000}
    assert r["fetchesAfter"] > r["fetchesBefore"]
    assert r["round"] == 3 and abs(r["t"] - 6000) < 1e-6 and r["selected"] == "node"
