"""A team's round progression (orchestrator/rounds/progression.py,
docs/progression.md): the document, what a team may see of it, the Dev
variant, the clock, log records, the zip, and the endpoints that serve it
(team API, the PROGRESSION page and its data, the staff player's).

Against PostgreSQL with the real migrations and the dashboard's real
middleware (the `site` fixture of test_dashboard_login.py).
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from test_dashboard_login import HOST, proxied, site  # noqa: F401 (fixture)

pytest.importorskip("django")

TOKEN_A = "tok-A_" + "a" * 37
TOKEN_B = "tok-B_" + "b" * 37


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def world(site):  # noqa: F811
    """Team A's finished run in R1 (three VMs: it captured two, never the
    third), team B in the same round, a running round R2, both teams' devboxes,
    the monitor's flows and a Proxmox snapshot."""
    import infra_fixture
    from core.models.competition import ApplicationSetting, Round
    from core.models.devbox import Devbox, DevboxCapture, DevboxLog, DevboxVM
    from core.models.logging import Logging, LoggingAttempt
    from core.models.monitoring import NetworkFlow
    from core.models.rounds import RoundRun, RunEvent
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from core.models.team import AccessTokens, Team, TeamMembership
    from core.models.topology import TopologySnapshot
    from core.models.user import User
    from core.models.vm import VirtualMachine
    from dashboard.infra_view import address_index, analyze
    from dashboard.views import topology as topology_views
    from django.utils import timezone

    now = timezone.now()
    hours = lambda h: timezone.timedelta(hours=h)  # noqa: E731
    comp = ApplicationSetting.get_current_competition()
    a = Team.objects.create(name="team-a", competition=comp)
    b = Team.objects.create(name="team-b", competition=comp)
    staff = User.objects.get(username="admin")
    student = User.objects.create(username="student-a")
    TeamMembership.objects.create(user=student, team=a)
    other = User.objects.create(username="student-b")
    TeamMembership.objects.create(user=other, team=b)
    AccessTokens.objects.create(name="a", access_token_hash=sha(TOKEN_A), team=a, created_by=staff)
    AccessTokens.objects.create(name="b", access_token_hash=sha(TOKEN_B), team=b, created_by=staff)

    start = now - hours(2)
    r1 = Round.objects.create(competition=comp, name="R1", seed=1, round_start=start - hours(1), round_end=now - hours(0.2))
    r2 = Round.objects.create(competition=comp, name="R2", seed=2, round_start=now - hours(0.1), round_end=now + hours(2))
    run = RoundRun.objects.create(
        round=r1, team=a, position=0, seed=1, state="finished", worm_start_at=start, ends_at=start + timezone.timedelta(minutes=10),
        sandbox_params={"start_vm": "vm-start"}, topology={"secret_architecture": "ARCH-SECRET"},
    )  # fmt: skip
    RoundRun.objects.create(round=r1, team=b, position=1, seed=1, state="skipped")
    vm = {}
    for name, addresses, nets, vmid in [
        ("vm-start", ["10.64.0.10"], [("vmbr1000", "10.64.0.0/24")], 97770),
        ("vm-pivot", ["10.64.0.11", "10.64.1.11"], [("vmbr1000", "10.64.0.0/24"), ("vmbr1001", "10.64.1.0/24")], 97771),
        ("vm-hidden", ["10.64.2.12"], [("vmbr1002", "10.64.2.0/24")], 97772),
    ]:
        vm[name] = VirtualMachine.objects.create(
            vm_identifier=name, vm_maintenance_ip=f"10.4.0.{vmid - 97700}", round=r1, run=run, proxmox_vm_id=vmid,
            addresses=addresses, networks=[{"name": n, "cidr": c} for n, c in nets], ctf_flag_hash=sha(f"flag-{name}"),
        )  # fmt: skip
    at = lambda s: start + timezone.timedelta(seconds=s)  # noqa: E731

    def event(s, kind, v=None, **data):
        RunEvent.objects.create(run=run, ts=at(s), kind=kind, vm=v, data=data)

    def attempt(team, flag, s, devbox=False, processed=True):
        return FlagCaptureAttempt.objects.create(team_uuid=str(team.uuid), vm_flag_hash=flag, created_at=at(s), devbox=devbox, processed=processed)

    event(-60, "state", state="preparing")
    event(-30, "error", message="ERRSECRET /root/.ssh/id_ed25519 on 10.4.0.70")
    event(-10, "worm_deployed", worm_start_at=start.isoformat())
    event(0, "worm_started")
    event(0, "state", state="running")
    for v in vm.values():
        event(1, "secret_released", v)
    event(100, "vm_stopped", vm["vm-hidden"])
    for s, name in [(20, "vm-start"), (50, "vm-pivot")]:
        captured = attempt(a, sha(f"flag-{name}"), s)
        FlagCapture.objects.create(team=a, round=r1, vm=vm[name], flag_capture_attempt=captured)
        event(s, "captured", vm[name], team=a.id, attempt=captured.id)
    event(700, "state", state="finished")
    attempt(a, sha("flag-vm-start"), 60)  # duplicate
    attempt(a, sha("flag-vm-hidden"), 70)  # a run VM, not counted: its VM stays hidden
    attempt(a, "0" * 64, 80)  # invalid
    attempt(b, sha("flag-vm-pivot"), 25)  # another team's submission

    def log(team, s, data, rnd=r1, devbox=False):
        entry = LoggingAttempt.objects.create(team_uuid=str(team.uuid), log_data=data, created_at=at(s), devbox=devbox, processed=True)
        if not devbox:
            Logging.objects.create(team=team, round=rnd, logging_attempt=entry)
        return entry

    log_a = log(a, 30, {"note": "first"})
    log_hidden = log(a, 95, {"note": "sent from the hidden VM"})
    log_b = log(b, 31, {"note": "team b's secret"})

    flows = [
        ("F-lan", 10, "10.64.0.10", "10.64.0.11", 22, "tcp"),
        ("F-to-hidden", 40, "10.64.1.11", "10.64.2.12", 22, "tcp"),
        ("F-scoring", 19.5, "10.64.0.10", "10.2.0.14", 443, "tcp"),
        ("F-hidden-logging", 94.5, "10.64.2.12", "10.2.0.15", 443, "tcp"),
        ("F-dns", 12, "10.64.0.10", "10.2.0.11", 53, "udp"),
        ("F-proxmox", 13, "10.64.0.10", "10.4.0.1", 8006, "tcp"),
        ("F-internet", 14, "10.64.0.10", "8.8.8.8", 53, "udp"),
        ("F-gateway", 15, "10.64.0.10", "10.64.0.1", 80, "tcp"),
    ]
    for uid, s, src, dst, port, transport in flows:
        NetworkFlow.objects.create(uid=uid, ts=at(s), duration=0.5, src_ip=src, dst_ip=dst, src_port=40000, dst_port=port,
                                   transport=transport, conn_state="SF")  # fmt: skip

    # Devboxes: team A's A was found (and again: a duplicate); its B long ago,
    # before its devbox's current version; its C is pending. Team B's token
    # submitted by A is not A's. Team B's own activity is B's.
    devbox_vms = {}
    for team, slug, index in [(a, "t1", 1), (b, "t2", 2)]:
        box = Devbox.objects.create(team=team, index=index, services_source_ip=f"10.2.0.{32 + index}", gateway_vm_id=98800 + index)
        for role in "abc":
            devbox_vms[f"{slug}-{role}"] = DevboxVM.objects.create(
                devbox=box, vm_identifier=f"devbox-{slug}-{role}", role=role, vm_maintenance_ip=f"10.6.{index}.1{role}",
                ssh_fabric_ip=f"10.7.{index}.77", proxmox_vm_id=98880 + index * 10 + "abc".index(role),
                ctf_flag_hash=sha(f"token-{slug}-{role}"),
            )  # fmt: skip
    dev_at = lambda h: now - hours(h)  # noqa: E731

    def dev_attempt(team, token, h, processed=True):
        return FlagCaptureAttempt.objects.create(team_uuid=str(team.uuid), vm_flag_hash=sha(token), created_at=dev_at(h),
                                                 devbox=True, processed=processed)  # fmt: skip

    found = dev_attempt(a, "token-t1-a", 20)
    DevboxCapture.objects.create(team=a, devbox_vm=devbox_vms["t1-a"], devbox_version=1, flag_capture_attempt=found)
    before = dev_attempt(a, "token-t1-b", 48)  # long ago
    DevboxCapture.objects.create(team=a, devbox_vm=devbox_vms["t1-b"], devbox_version=1, flag_capture_attempt=before)
    dev_attempt(a, "token-t2-a", 10)  # team B's token
    dev_attempt(a, "token-t1-a", 5)  # duplicate
    dev_attempt(a, "token-t1-c", 1, processed=False)  # pending
    found_b = dev_attempt(b, "token-t2-a", 3)
    DevboxCapture.objects.create(team=b, devbox_vm=devbox_vms["t2-a"], devbox_version=1, flag_capture_attempt=found_b)

    def dev_log(team, h, data):
        entry = LoggingAttempt.objects.create(team_uuid=str(team.uuid), log_data=data, created_at=dev_at(h), devbox=True, processed=True)
        DevboxLog.objects.create(team=team, devbox_version=1, logging_attempt=entry)
        return entry

    dev_log_a = dev_log(a, 4, {"dev": "a-log"})
    dev_log_b = dev_log(b, 4, {"dev": "b-log"})
    dev_log(a, 72, {"dev": "long ago"})

    TopologySnapshot.objects.create(source="proxmox", data=infra_fixture.snapshot(), collected_at=now)
    topology_views._cache.clear()
    return {
        "now": now, "a": a, "b": b, "r1": r1, "r2": r2, "run": run, "vm": vm, "devbox_vms": devbox_vms,
        "staff": staff, "student": student, "other": other, "client": site,
        "logs": {"a": log_a, "hidden": log_hidden, "b": log_b, "dev_a": dev_log_a, "dev_b": dev_log_b},
        "infra": address_index(analyze(infra_fixture.snapshot())),
    }


def build(world, team="a", rnd="r1", **kw):
    from orchestrator.rounds import progression

    kw.setdefault("infra", world["infra"])
    return progression.build(world[team], world[rnd], now=world["now"], **kw)


def walk(value):
    """Every string in a JSON value (keys and values)."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)
    elif value is not None:
        yield str(value)


# What a team must never see: the hidden VM, Proxmox ids and internals,
# infrastructure, other teams, secrets. (What it submitted itself -- the
# hash prefix of a flag, also of the hidden VM's -- is its own.)
def forbidden(world):
    hidden = world["vm"]["vm-hidden"]
    return [
        str(hidden.uuid), "vm-hidden", "10.64.2.12", "10.64.2.0/24", "vmbr", "9777", "10.4.0.",
        "worms-pve", "sandbox-gw", "proxmox", "database", "orchestrator", "dashboard", "monitor", "syslog", "firewall", "rules",
        "security_groups", "nics", "ERRSECRET", "id_ed25519", "ARCH-SECRET", "architecture", str(world["run"].uuid), "team-b",
        str(world["b"].uuid), "team b's secret", "devbox-t2", "9888", "10.7.", "10.6.", str(world["logs"]["hidden"].log_data),
    ]


def assert_clean(doc, world):
    text = "\n".join(walk(doc))
    for word in forbidden(world):
        assert word not in text, word


# --- the document -------------------------------------------------------------------


def test_document_shape(world):
    doc = build(world, audience="team")
    assert doc["format"] == "worms-progression" and doc["version"] == 1
    assert set(doc) == {"format", "version", "generated_at", "audience", "environment", "round", "team", "clock", "scene",
                        "timeline", "logs", "network"}
    assert doc["audience"] == "team" and doc["environment"] == "live"
    assert doc["round"] == {"id": world["r1"].id, "name": "R1", "competition": "main", "start": world["r1"].round_start.isoformat(),
                            "end": world["r1"].round_end.isoformat()}
    assert doc["team"] == {"uuid": str(world["a"].uuid), "name": "team-a"}
    # Logs are references; the flows are counted, not inline.
    assert "flows" not in doc["timeline"]
    assert doc["logs"] == {"count": 2, "truncated": False, "included": False, "path": None, "url": None}
    assert doc["network"]["available"] and not doc["network"]["included"] and doc["network"]["count"] == 3
    logs = [e for e in doc["timeline"]["events"] if e["kind"] == "log_received"]
    assert logs[0]["data"]["log"]["ref"] == str(world["logs"]["a"].uuid) and logs[0]["data"]["log"]["size"] > 10
    assert "first" not in json.dumps(doc)  # the record itself is not in the document


def test_team_sees_only_what_it_captured(world):
    doc = build(world, audience="team", flows=True)
    tl = doc["timeline"]
    vm = world["vm"]
    start, pivot = str(vm["vm-start"].uuid), str(vm["vm-pivot"].uuid)
    assert_clean(doc, world)

    assert [n["label"] for n in tl["nodes"]] == ["vm-start", "vm-pivot"]
    assert all(set(n) == {"id", "label", "group", "addresses", "networks", "start_vm", "captured", "captured_at"} for n in tl["nodes"])
    # Networks: opaque ids, labelled by their CIDR; only those a visible VM is on.
    assert {n["id"] for n in tl["networks"]} == {"net-1", "net-2"}
    assert {n["cidr"] for n in tl["networks"]} == {"10.64.0.0/24", "10.64.1.0/24"}
    assert set(tl["vm_status"]) == {start, pivot}
    assert [c["vm"] for c in tl["captures"]] == [start, pivot] and all(set(c) == {"t", "vm"} for c in tl["captures"])
    assert tl["run"]["uuid"] is None and tl["run"]["start_vm"] == "vm-start"

    kinds = [e["kind"] for e in tl["events"]]
    assert "error" not in kinds and kinds.count("secret_released") == 2 and "vm_stopped" not in kinds
    for e in tl["events"]:
        if e["kind"] == "state":
            assert set(e["data"]) == {"state"}
        if e["kind"] in ("captured", "secret_released", "worm_started", "worm_deployed"):
            assert e["data"] == {}
    submissions = {e["t"]: e["data"] for e in tl["events"] if e["kind"] == "flag_submitted"}
    assert [d["result"] for d in submissions.values()] == ["captured", "captured", "duplicate", "not_counted", "invalid"]
    assert submissions[20.0]["flag_of"] == start and submissions[20.0]["source_vm"] == start  # via F-scoring
    assert submissions[70.0]["flag_of"] is None and submissions[70.0]["flag_of_label"] is None  # the hidden VM's flag
    # The log sent while only the hidden VM talked to logging: its source is
    # blanked (the staff view infers it).
    logs = {e["t"]: e for e in tl["events"] if e["kind"] == "log_received"}
    assert logs[95.0]["data"]["source_vm"] is None and logs[95.0]["vm"] is None and not logs[95.0]["data"]["source_inferred"]
    staff = {e["t"]: e for e in build(world, audience="staff")["timeline"]["events"] if e["kind"] == "log_received"}
    assert staff[95.0]["data"]["source_vm"] == str(vm["vm-hidden"].uuid)

    flows = {f["uid"]: f for f in tl["flows"]}
    assert set(flows) == {"F-lan", "F-scoring", "F-dns"}
    assert flows["F-scoring"]["dst"] == "role:scoring" and flows["F-scoring"]["dst_name"] == "scoring"
    assert flows["F-dns"]["dst"] == "pi:dns" and flows["F-lan"]["dst"] == pivot

    scene = doc["scene"]
    assert [g["id"] for g in scene["groups"]] == ["group:sandbox"]
    assert {n["role"] for n in scene["nodes"]} == {"scoring", "logging", "dns", "private_internet"}
    assert all(set(n) == {"id", "name", "role", "status", "active"} for n in scene["nodes"])
    assert all(e["verified"] for e in scene["edges"])


def _entry_never_captured(world, **run):
    """The run's entry VM is the hidden one, and a further VM exists that it also never
    captured, so "the entry VM" and "every uncaptured VM" cannot be confused."""
    from core.models.rounds import RoundRun
    from core.models.vm import VirtualMachine

    RoundRun.objects.filter(pk=world["run"].pk).update(sandbox_params={"start_vm": "vm-hidden"}, **run)
    VirtualMachine.objects.create(
        vm_identifier="vm-other", vm_maintenance_ip="10.4.0.99", round=world["r1"], run=world["run"], proxmox_vm_id=97799,
        addresses=["10.64.3.13"], networks=[{"name": "vmbr1003", "cidr": "10.64.3.0/24"}], ctf_flag_hash=sha("flag-vm-other"),
    )  # fmt: skip


def test_the_entry_vm_is_visible_from_the_worms_start_without_a_capture(world):
    _entry_never_captured(world)

    doc = build(world, audience="team", flows=True)

    tl = doc["timeline"]
    hidden = str(world["vm"]["vm-hidden"].uuid)
    entry = next(n for n in tl["nodes"] if n["id"] == hidden)
    assert entry["start_vm"] is True and entry["captured"] is False and entry["captured_at"] is None
    assert entry["addresses"] == ["10.64.2.12"] and entry["networks"][0]["cidr"] == "10.64.2.0/24"
    assert tl["run"]["start_vm"] == "vm-hidden"
    # Not a capture: the score's list stays the two it captured.
    assert hidden not in {c["vm"] for c in tl["captures"]} and len(tl["captures"]) == 2
    assert not [e for e in tl["events"] if e["kind"] == "captured" and e["vm"] == hidden]
    # Its power state, the worm's start and its own traffic come with it.
    assert hidden in tl["vm_status"] and [e for e in tl["events"] if e["kind"] == "vm_stopped" and e["vm"] == hidden]
    assert next(e for e in tl["events"] if e["kind"] == "worm_started")["node"] == hidden
    assert {"F-to-hidden", "F-hidden-logging"} <= {f["uid"] for f in tl["flows"]}
    # Every other VM it did not capture stays out.
    text = "\n".join(walk(doc))
    for word in ("vm-other", "10.64.3.13", "10.64.3.0/24"):
        assert word not in text, word
    assert {n["label"] for n in tl["nodes"]} == {"vm-start", "vm-pivot", "vm-hidden"}


@pytest.mark.parametrize("started", ["armed", "preparing"])
def test_the_entry_vm_stays_hidden_until_the_worm_starts(world, started):
    from django.utils import timezone

    # Armed: the start time is known but has not come. Preparing: there is none yet.
    fields = {"state": started, "worm_start_at": world["now"] + timezone.timedelta(minutes=5) if started == "armed" else None}
    _entry_never_captured(world, **fields)

    doc = build(world, audience="team", flows=True)

    tl = doc["timeline"]
    assert tl["run"]["start_vm"] is None
    assert not [n for n in tl["nodes"] if n["start_vm"]]
    assert not [e for e in tl["events"] if e["kind"] in ("worm_started", "worm_deployed") and e["node"] is not None]
    text = "\n".join(walk(doc))
    for word in ("vm-hidden", "10.64.2.12", "10.64.2.0/24", str(world["vm"]["vm-hidden"].uuid)):
        assert word not in text, word


def test_the_entry_vm_is_visible_at_the_moment_the_worm_starts(world):
    _entry_never_captured(world, state="armed", worm_start_at=world["now"])

    tl = build(world, audience="team")["timeline"]

    assert tl["run"]["start_vm"] == "vm-hidden"


def test_staff_see_everything(world):
    doc = build(world, audience="staff", flows=True)
    tl = doc["timeline"]
    assert doc["scene"] is None  # the page has the live model
    assert {n["label"] for n in tl["nodes"]} == {"vm-start", "vm-pivot", "vm-hidden"}
    assert "error" in {e["kind"] for e in tl["events"]} and tl["architecture"] == {"secret_architecture": "ARCH-SECRET"}
    assert {f["uid"] for f in tl["flows"]} == {f"F-{x}" for x in ("lan", "to-hidden", "scoring", "hidden-logging", "dns", "proxmox", "internet", "gateway")}
    assert tl["run"]["uuid"] == str(world["run"].uuid) and tl["run"]["team_slug"] == f"t{world['a'].id}"


def test_another_team_sees_nothing_of_it(world):
    doc = build(world, team="b", audience="team", flows=True)
    tl = doc["timeline"]
    assert tl["nodes"] == [] and tl["flows"] == [] and tl["run"]["state"] == "skipped"
    text = json.dumps(doc)
    for word in ("vm-start", "vm-pivot", str(world["a"].uuid), "team-a", "first"):
        assert word not in text


def _devbox_since(world, team="a", hours_ago=30):
    """The world's devbox has existed long enough to cover its submissions."""
    from core.models.devbox import Devbox
    from django.utils import timezone

    Devbox.objects.filter(team=world[team]).update(created_at=world["now"] - timezone.timedelta(hours=hours_ago))


def _devbox_doc(world, team="a"):
    from orchestrator.rounds import progression

    return progression.build_devbox(world[team], now=world["now"])


def test_the_devbox_progression_has_no_round_and_is_never_finished(world):
    from django.utils import timezone

    _devbox_since(world)
    doc = _devbox_doc(world)
    assert doc["round"] is None and doc["environment"] == "dev" and doc["audience"] == "team"
    clock = doc["clock"]
    assert clock["finished"] is False and clock["now"] is not None
    assert clock["round_start"] is None and clock["round_end"] is None
    # The clock starts just before the first activity, and runs up to now.
    assert clock["t0"] == (world["now"] - timezone.timedelta(hours=20, seconds=5)).isoformat()
    assert clock["t_min"] == 0.0 and clock["t_max"] == clock["now"]
    tl = doc["timeline"]
    submissions = [e["data"]["result"] for e in tl["events"] if e["kind"] == "flag_submitted"]
    assert submissions == ["captured", "invalid", "duplicate", "pending"]
    assert [(n["label"], n["captured"]) for n in tl["nodes"]] == [("devbox-t1-a", True), ("devbox-t1-b", False), ("devbox-t1-c", False)]
    assert [e["data"]["log"]["ref"] for e in tl["events"] if e["kind"] == "log_received"] == [str(world["logs"]["dev_a"].uuid)]
    assert doc["network"]["available"] is False and "flows" not in tl
    text = "\n".join(walk(doc))
    for word in ("devbox-t2", "team-b", "10.6.", "10.7.", "9888", "10.2.0.3"):
        assert word not in text, word


def test_the_devbox_progression_starts_over_at_a_reset(world):
    from core.models.rounds import HostJob
    from django.utils import timezone

    _devbox_since(world)
    team = world["a"]
    team.devbox_version = 2
    team.save(update_fields=["devbox_version"])
    requested = world["now"] - timezone.timedelta(hours=2)
    HostJob.objects.create(kind=HostJob.Kind.DEVBOX_REBUILD, params={"team_uuid": str(team.uuid), "devbox_version": 2}, requested_at=requested)
    # An older reset, and another team's, do not count.
    HostJob.objects.create(kind=HostJob.Kind.DEVBOX_REBUILD, params={"team_uuid": str(team.uuid), "devbox_version": 1}, requested_at=world["now"])
    HostJob.objects.create(kind=HostJob.Kind.DEVBOX_REBUILD, params={"team_uuid": str(world["b"].uuid), "devbox_version": 2}, requested_at=world["now"])
    tl = _devbox_doc(world)["timeline"]
    assert [e["data"]["result"] for e in tl["events"] if e["kind"] == "flag_submitted"] == ["pending"]
    # The VMs are still there, with nothing captured yet.
    assert [n["captured"] for n in tl["nodes"]] == [False, False, False] and tl["captures"] == []
    assert not [e for e in tl["events"] if e["kind"] == "log_received"]


def test_the_devbox_vms_are_visible_with_no_activity_at_all(world):
    from core.models.logging import LoggingAttempt
    from core.models.scoring import FlagCaptureAttempt

    team = world["b"]
    FlagCaptureAttempt.objects.filter(team_uuid=str(team.uuid)).delete()
    LoggingAttempt.objects.filter(team_uuid=str(team.uuid)).delete()
    _devbox_since(world, "b")

    doc = _devbox_doc(world, "b")

    tl = doc["timeline"]
    assert tl["events"] == [] and tl["captures"] == []
    assert [(n["label"], n["captured"], n["captured_at"]) for n in tl["nodes"]] == [
        ("devbox-t2-a", False, None), ("devbox-t2-b", False, None), ("devbox-t2-c", False, None),
    ]  # fmt: skip
    assert all(tl["vm_status"][n["id"]] == [[0.0, "running"]] for n in tl["nodes"])
    # Drawn as a chain: A-B and B-C.
    ids = {n["devbox_role"]: n["id"] for n in tl["nodes"]}
    assert doc["scene"]["groups"][0]["members"] == [ids["a"], ids["b"], ids["c"]]
    links = {(e["source"], e["target"]) for e in doc["scene"]["edges"] if e["kind"] == "lan"}
    assert links == {(ids["a"], ids["b"]), (ids["b"], ids["c"])}


def test_the_devbox_vms_are_not_shown_before_they_are_set_up(world):
    from core.models.devbox import DevboxVM

    DevboxVM.objects.filter(devbox__team=world["a"]).delete()
    _devbox_since(world)

    doc = _devbox_doc(world)

    assert doc["timeline"]["nodes"] == [] and doc["scene"]["groups"][0]["members"] == []


def test_the_devbox_summary_lists_only_what_was_captured(world):
    from dashboard.views.progression import devbox_summary

    _devbox_since(world)

    summary = devbox_summary(_devbox_doc(world))

    assert [c["vm"] for c in summary["captures"]] == ["devbox-t1-a"]


def _uploaded_worm(world, version=1):
    from core.models.devbox import DevboxRun
    from core.models.user import User
    from core.models.worms import MalwareArtifact, Upload
    from django.utils import timezone

    armed = world["now"] - timezone.timedelta(hours=1)
    artifact = MalwareArtifact.objects.create(content_hash="d" * 64, storage_key="malware-artifacts/dd/dd/" + "d" * 64 + ".zip")
    upload = Upload.objects.create(artifact=artifact, user=User.objects.get(username="student-a"), team=world["a"], devbox=True)
    return DevboxRun.objects.create(
        upload=upload, team=world["a"], state="running", devbox_version=version, armed_at=armed,
        worm_start_at=armed + timezone.timedelta(seconds=10), ends_at=armed + timezone.timedelta(hours=1),
    )  # fmt: skip


def test_the_devbox_progression_shows_an_uploaded_worm_on_vm_a(world):
    _devbox_since(world)
    run = _uploaded_worm(world)

    doc = _devbox_doc(world)

    tl = doc["timeline"]
    entry = {n["label"]: n["start_vm"] for n in tl["nodes"]}
    assert entry == {"devbox-t1-a": True, "devbox-t1-b": False, "devbox-t1-c": False}
    a = next(n["id"] for n in tl["nodes"] if n["label"] == "devbox-t1-a")
    events = {e["kind"]: e for e in tl["events"]}
    assert events["worm_deployed"]["node"] == a and events["worm_started"]["node"] == a and events["worm_started"]["data"] == {}
    assert events["worm_started"]["t"] - events["worm_deployed"]["t"] == 10
    assert run.upload.artifact.content_hash not in "\n".join(walk(doc))


def test_a_worm_from_before_a_reset_is_not_shown(world):
    from core.models.team import Team

    _devbox_since(world)
    _uploaded_worm(world, version=1)
    Team.objects.filter(pk=world["a"].pk).update(devbox_version=2)
    world["a"].refresh_from_db()

    tl = _devbox_doc(world)["timeline"]

    assert not [n for n in tl["nodes"] if n["start_vm"]]
    assert not [e for e in tl["events"] if e["kind"] in ("worm_deployed", "worm_started")]


def test_a_team_without_a_devbox_has_no_devbox_progression(world):
    from core.models.devbox import Devbox

    Devbox.objects.filter(team=world["a"]).delete()
    assert _devbox_doc(world) is None


def test_the_devbox_progression_can_be_downloaded_with_the_teams_bearer_token(world):
    _devbox_since(world)
    response, body = api_get(world, "/api/v1/devbox/progression/")
    assert response.status_code == 200 and body["round"] is None and body["team"]["name"] == "team-a"
    ref = str(world["logs"]["dev_a"].uuid)
    assert body["logs"]["url"] == "/api/v1/devbox/progression/logs/{ref}/"

    response, record = api_get(world, body["logs"]["url"].replace("{ref}", ref))
    assert response.status_code == 200 and record["data"] == {"dev": "a-log"}
    response, missing = api_get(world, body["logs"]["url"].replace("{ref}", str(world["logs"]["dev_b"].uuid)))
    assert response.status_code == 404 and missing["error"]["code"] == "not_found"  # team B's

    response, _ = api_get(world, "/api/v1/devbox/progression.zip")
    assert response.status_code == 200 and response["Content-Type"] == "application/zip"
    assert response["Content-Disposition"] == 'attachment; filename="worms-devbox-team-a-dev.zip"'
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert f"logs/{ref}.json" in archive.namelist() and "network/flows.jsonl" not in archive.namelist()
    assert json.loads(archive.read("progression.json"))["round"] is None
    response, _ = api_get(world, "/api/v1/devbox/progression.zip?logs=0")
    assert zipfile.ZipFile(io.BytesIO(response.content)).namelist() == ["progression.json", "README.txt"]
    response, bad = api_get(world, "/api/v1/devbox/progression.zip?logs=maybe")
    assert response.status_code == 400 and bad["error"]["code"] == "bad_flag"


def test_the_devbox_download_is_each_teams_own_and_needs_a_token(world):
    from core.models.devbox import Devbox

    _devbox_since(world)
    _devbox_since(world, "b")
    _, own = api_get(world, "/api/v1/devbox/progression/", token=TOKEN_B)
    assert own["team"]["name"] == "team-b"
    assert "devbox-t1-a" not in "\n".join(walk(own))
    for path in ("/api/v1/devbox/progression/", "/api/v1/devbox/progression.zip"):
        assert world["client"].get(path, **proxied(HOST)).status_code == 401
        assert world["client"].get(path, HTTP_AUTHORIZATION="Bearer wrong", **proxied(HOST)).status_code == 401
    assert world["client"].post("/api/v1/devbox/progression.zip", HTTP_AUTHORIZATION=f"Bearer {TOKEN_A}", **proxied(HOST)).status_code == 405
    Devbox.objects.filter(team=world["a"]).delete()
    response, body = api_get(world, "/api/v1/devbox/progression.zip")
    assert response.status_code == 404 and body["error"]["code"] == "not_found"


def test_clock(world):
    from django.utils import timezone

    now = world["now"]
    r1 = build(world, audience="team")
    zero = world["run"].worm_start_at
    c = r1["clock"]
    assert c["t0"] == zero.isoformat() and c["finished"] is True and c["now"] is None
    assert c["round_start"] == round((world["r1"].round_start - zero).total_seconds(), 2)
    assert c["round_end"] == round((world["r1"].round_end - zero).total_seconds(), 2)
    assert c["t_min"] == -60.0 and c["t_max"] == 700.0

    # A round in progress, the team's run not planned: the round's start is t0.
    r2 = build(world, rnd="r2", audience="team")
    assert r2["clock"]["finished"] is False and r2["clock"]["round_start"] == 0.0
    assert r2["clock"]["now"] == round((now - world["r2"].round_start).total_seconds(), 2)
    assert r2["timeline"]["run"] is None and r2["timeline"]["events"] == []

    # A run in progress plays up to now.
    from core.models.rounds import RoundRun, RunEvent

    live = RoundRun.objects.create(round=world["r2"], team=world["a"], position=0, seed=1, state="running",
                                   worm_start_at=now - timezone.timedelta(minutes=3))  # fmt: skip
    RunEvent.objects.create(run=live, ts=now - timezone.timedelta(minutes=4), kind="state", data={"state": "preparing"})
    running = build(world, rnd="r2", audience="team")
    assert running["clock"]["t_max"] == running["clock"]["now"] == 180.0


def test_log_records_are_only_the_teams_own_in_the_round(world):
    from orchestrator.rounds import progression

    logs, now = world["logs"], world["now"]
    record = lambda team, rnd, entry: progression.log_record(world[team], world[rnd], entry.uuid, now=now)  # noqa: E731
    assert record("a", "r1", logs["a"]) == {"ref": str(logs["a"].uuid), "received_at": logs["a"].created_at.isoformat(),
                                            "data": {"note": "first"}}
    assert record("a", "r1", logs["b"]) is None  # another team's
    assert record("b", "r1", logs["a"]) is None
    assert record("a", "r1", logs["dev_a"]) is None  # a devbox log is not the round's
    assert record("a", "r2", logs["a"]) is None  # no run there


def test_zip_package(world):
    from orchestrator.rounds import progression

    doc = build(world, audience="team", flows=True)
    data = progression.package(doc, progression.log_files(doc))
    archive = zipfile.ZipFile(io.BytesIO(data))
    ref = str(world["logs"]["a"].uuid)
    hidden_ref = str(world["logs"]["hidden"].uuid)
    assert sorted(archive.namelist()) == sorted(["progression.json", "README.txt", f"logs/{ref}.json", f"logs/{hidden_ref}.json",
                                                 "network/flows.jsonl"])
    inner = json.loads(archive.read("progression.json"))
    assert "flows" not in inner["timeline"]
    assert inner["network"] | {"count": 0} == {"available": True, "reason": None, "count": 0, "truncated": False, "included": True,
                                               "path": "network/flows.jsonl", "url": None}
    assert inner["logs"]["included"] and inner["logs"]["path"] == "logs/{ref}.json"
    lines = archive.read("network/flows.jsonl").decode().splitlines()
    assert [json.loads(line)["uid"] for line in lines] == [f["uid"] for f in doc["timeline"]["flows"]]
    assert json.loads(archive.read(f"logs/{ref}.json"))["data"] == {"note": "first"}
    assert "flows" in doc["timeline"]  # the document itself is untouched

    lean = zipfile.ZipFile(io.BytesIO(progression.package(build(world, audience="team"))))
    assert lean.namelist() == ["progression.json", "README.txt"]
    inner = json.loads(lean.read("progression.json"))
    assert not inner["logs"]["included"] and not inner["network"]["included"] and inner["network"]["path"] is None
    assert progression.filename(doc) == "worms-R1-team-a-live.zip"


def test_fixtures_match_the_builder(world):
    """The browser tests' synthetic documents (tests/topology_fixtures.py)
    have the builder's schema."""
    import topology_fixtures

    def same_shape(real, fake):
        assert set(real) == set(fake)
        assert set(real["clock"]) == set(fake["clock"]) and set(real["logs"]) == set(fake["logs"])
        assert set(real["network"]) == set(fake["network"]) and set(real["round"]) == set(fake["round"])
        rt, ft = real["timeline"], fake["timeline"]
        assert set(rt) == set(ft)
        if rt["run"] or ft["run"]:
            assert set(rt["run"]) == set(ft["run"])
        assert set(rt["nodes"][0]) == set(ft["nodes"][0])
        for kind in {e["kind"] for e in rt["events"]} & {e["kind"] for e in ft["events"]}:
            r = next(e for e in rt["events"] if e["kind"] == kind)
            f = next(e for e in ft["events"] if e["kind"] == kind)
            assert set(r) == set(f) and set(r["data"]) == set(f["data"]), kind
        if real["scene"] or fake["scene"]:
            assert set(real["scene"]) == set(fake["scene"])
            assert set(real["scene"]["groups"][0]) == set(fake["scene"]["groups"][0])

    same_shape(build(world, audience="team"), topology_fixtures.team_progression(1))
    _devbox_since(world)
    real, fake = _devbox_doc(world), topology_fixtures.devbox_progression()
    assert set(real) == set(fake) and real["round"] is None and fake["round"] is None
    assert set(real["clock"]) == set(fake["clock"]) and set(real["timeline"]) == set(fake["timeline"])
    assert set(real["timeline"]["nodes"][0]) == set(fake["timeline"]["nodes"][0])
    same_shape(build(world, audience="staff"), topology_fixtures.progression())


# --- the team API ----------------------------------------------------------------


def api_get(world, path, token=TOKEN_A):
    response = world["client"].get(path, HTTP_AUTHORIZATION=f"Bearer {token}", **proxied(HOST))
    body = json.loads(response.content) if response["Content-Type"].startswith("application/json") else None
    return response, body


def test_api_progression(world):
    r1 = world["r1"].id
    _, rounds = api_get(world, "/api/v1/rounds/")
    entry = next(r for r in rounds["rounds"] if r["id"] == r1)
    assert entry["progression"] == f"/api/v1/rounds/{r1}/progression/" and entry["download"] == f"/api/v1/rounds/{r1}/progression.zip"

    response, doc = api_get(world, f"/api/v1/rounds/{r1}/progression/")
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    assert doc["audience"] == "team" and doc["format"] == "worms-progression"
    assert doc["network"]["url"] == f"/api/v1/rounds/{r1}/progression/flows/"
    assert doc["logs"]["url"] == f"/api/v1/rounds/{r1}/progression/logs/{{ref}}/"
    assert_clean(doc, world)

    _, flows = api_get(world, doc["network"]["url"])
    assert {f["uid"] for f in flows["flows"]} == {"F-lan", "F-scoring", "F-dns"} and flows["count"] == 3
    assert_clean(flows, world)

    ref = str(world["logs"]["a"].uuid)
    response, record = api_get(world, doc["logs"]["url"].replace("{ref}", ref))
    assert response.status_code == 200 and record["data"] == {"note": "first"}
    response, body = api_get(world, doc["logs"]["url"].replace("{ref}", str(world["logs"]["b"].uuid)))
    assert response.status_code == 404 and body["error"]["code"] == "not_found"

    response, _ = api_get(world, f"/api/v1/rounds/{r1}/progression.zip")
    assert response.status_code == 200 and response["Content-Type"] == "application/zip"
    assert response["Content-Disposition"] == 'attachment; filename="worms-R1-team-a-live.zip"'
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert f"logs/{ref}.json" in names and "network/flows.jsonl" in names
    response, _ = api_get(world, f"/api/v1/rounds/{r1}/progression.zip?logs=0&network=0")
    assert zipfile.ZipFile(io.BytesIO(response.content)).namelist() == ["progression.json", "README.txt"]
    response, body = api_get(world, f"/api/v1/rounds/{r1}/progression.zip?logs=maybe")
    assert response.status_code == 400 and body["error"]["code"] == "bad_flag"

    # The rounds are the competition's only: the devbox has its own endpoint.
    assert [r["name"] for r in rounds["rounds"]] == ["R1", "R2"]
    assert all(r["competition"] == "main" for r in rounds["rounds"])


def test_api_progression_scope(world):
    from core.models.competition import Competition, Round

    r1 = world["r1"].id
    _, doc = api_get(world, f"/api/v1/rounds/{r1}/progression/", token=TOKEN_B)
    assert doc["team"]["name"] == "team-b" and doc["timeline"]["nodes"] == []
    assert "vm-start" not in json.dumps(doc)
    response, _ = api_get(world, f"/api/v1/rounds/{r1}/progression/logs/{world['logs']['a'].uuid}/", token=TOKEN_B)
    assert response.status_code == 404
    elsewhere = Round.objects.create(competition=Competition.objects.create(name="x"), name="X", seed=9)
    for path in (f"/api/v1/rounds/{elsewhere.id}/progression/", f"/api/v1/rounds/{elsewhere.id}/progression.zip"):
        assert api_get(world, path)[0].status_code == 404
    assert api_get(world, f"/api/v1/rounds/{r1}/progression/", token=None)[0].status_code == 401
    response = world["client"].post(f"/api/v1/rounds/{r1}/progression.zip", HTTP_AUTHORIZATION=f"Bearer {TOKEN_A}", **proxied(HOST))
    assert response.status_code == 405


# --- the PROGRESSION page and its data (signed in) ----------------------------------


def as_user(world, user, dev=False):
    client = world["client"]
    client.force_login(user)
    session = client.session
    session["is_dev_environment"] = dev
    session.save()
    return lambda path: client.get(path, **proxied(HOST))


def test_progression_page_and_navigation(world):
    get = as_user(world, world["student"])
    page = get("/progression/").content.decode()
    assert 'href="/progression/"' in page and 'aria-current="page">Progression' in page
    assert "/captures/" not in page and "/logs/" not in page
    # Without JavaScript: the round links, the captured VMs, the downloads.
    assert f'href="?round={world["r1"].id}"' in page and f'href="?round={world["r2"].id}"' in page
    selected = get(f"/progression/?round={world['r1'].id}").content.decode()
    assert "vm-start" in selected and "vm-pivot" in selected and "vm-hidden" not in selected
    assert f'/progression/rounds/{world["r1"].id}/download/' in selected
    assert 'data-audience="team"' in selected and 'data-progression="/progression/rounds/0/"' in selected
    for gone in ("/captures/", "/logs/"):
        assert get(gone).status_code == 404

    # The Dev view has no rounds: the devbox so far, whatever the address says.
    _devbox_since(world)
    dev_page = as_user(world, world["student"], dev=True)(f"/progression/?round={world['r1'].id}").content.decode()
    assert f'href="?round={world["r1"].id}"' not in dev_page and "Your devbox so far" in dev_page
    assert "devbox-t1-a" in dev_page and "devbox-t1-b" not in dev_page


def test_progression_data_for_the_team(world):
    get = as_user(world, world["student"])
    r1 = world["r1"].id
    rounds = json.loads(get("/progression/rounds/").content)
    assert rounds["environment"] == "live" and [r["name"] for r in rounds["rounds"]] == ["R1", "R2"]
    assert rounds["rounds"][0]["run"] == "finished" and rounds["default"] == world["r2"].id
    doc = json.loads(get(f"/progression/rounds/{r1}/").content)
    assert doc["audience"] == "team" and doc["logs"]["url"] == f"/progression/rounds/{r1}/logs/{{ref}}/"
    assert_clean(doc, world)
    flows = json.loads(get(f"/progression/rounds/{r1}/flows/").content)
    assert {f["uid"] for f in flows["flows"]} == {"F-lan", "F-scoring", "F-dns"}
    assert json.loads(get(f"/progression/rounds/{r1}/logs/{world['logs']['a'].uuid}/").content)["data"] == {"note": "first"}
    assert get(f"/progression/rounds/{r1}/logs/{world['logs']['b'].uuid}/").status_code == 404
    response = get(f"/progression/rounds/{r1}/download/?network=0")
    assert response["Content-Type"] == "application/zip"
    assert "network/flows.jsonl" not in zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert get(f"/progression/rounds/{r1}/download/?logs=x").status_code == 400
    # The Dev view has no rounds: the devbox has its own document.
    dev = as_user(world, world["student"], dev=True)
    assert dev(f"/progression/rounds/{r1}/").status_code == 404
    dev_rounds = json.loads(dev("/progression/rounds/").content)
    assert dev_rounds["environment"] == "dev" and dev_rounds["rounds"] == [] and dev_rounds["default"] is None

    # Team B sees its own (empty) run, nothing of team A's.
    other = as_user(world, world["other"])
    body = other(f"/progression/rounds/{r1}/").content.decode()
    assert "vm-start" not in body and "team-a" not in body
    assert other(f"/progression/rounds/{r1}/logs/{world['logs']['a'].uuid}/").status_code == 404


def test_staff_run_progression(world):
    run = world["run"].uuid
    student = as_user(world, world["student"])
    for path in (f"/rounds/{run}/progression/", f"/rounds/{run}/progression/flows/", f"/rounds/{run}/progression/logs/{world['logs']['a'].uuid}/"):
        assert student(path).status_code == 403
    assert student(f"/rounds/{run}/progression/download/").status_code == 302

    staff = as_user(world, world["staff"])
    doc = json.loads(staff(f"/rounds/{run}/progression/").content)
    assert doc["audience"] == "staff" and {n["label"] for n in doc["timeline"]["nodes"]} == {"vm-start", "vm-pivot", "vm-hidden"}
    assert doc["network"]["url"] == f"/rounds/{run}/progression/flows/"
    flows = json.loads(staff(doc["network"]["url"]).content)["flows"]
    assert "F-to-hidden" in {f["uid"] for f in flows}
    hidden_log = staff(f"/rounds/{run}/progression/logs/{world['logs']['hidden'].uuid}/")
    assert json.loads(hidden_log.content)["data"] == {"note": "sent from the hidden VM"}
    archive = zipfile.ZipFile(io.BytesIO(staff(f"/rounds/{run}/progression/download/").content))
    inner = json.loads(archive.read("progression.json"))
    # A staff download embeds the infrastructure it was viewed against.
    assert inner["scene"]["nodes"] and inner["scene"]["groups"]
