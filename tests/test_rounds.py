"""Round orchestration against the test database: the planner, and the conductor running each
team's attempt by the round's gameplan (src/gameplan), with a fake Proxmox (power) and a fake
round agent that answers host jobs as the real one does (scripts/round_agent/handlers.py,
the sandbox described by the real gameplan build). An attempt the infrastructure fails is
run again; only a finished attempt's captures count (docs/05-live-environment.md).
"""

from __future__ import annotations

import hashlib
import json
import sys

import pytest

from conftest import REPO

pytest.importorskip("django")
pytest.importorskip("psycopg2")

DASHBOARD = REPO / "projects" / "dashboard"
sys.path.insert(0, str(DASHBOARD))
sys.path.insert(0, str(REPO / "src"))  # the gameplan framework, as the orchestrator's GAMEPLAN_LIB has it

# Two VMs on one network. vm1's flag is gone while the worm starts on vm0; a minute later vm0
# is stopped and vm1's flag comes back.
GAMEPLAN = '''
from gameplan import BasicVM, Gameplan


class Two(Gameplan):
    def __init__(self):
        self.round_duration = 600
        self.vm0 = BasicVM()
        self.vm1 = BasicVM()
        self.register_network([self.vm0, self.vm1])
        self.set_worm_entrypoint(self.vm0)

    def execute(self):
        flag = self.remove_flag(self.vm1)
        self.deploy_worm()
        self.wait(60)
        self.vm0.stop()
        self.add_flag(flag)
'''


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def flag_hash(run_uuid, vm):
    """The flag the fake agent places: a new one per attempt and VM."""
    return sha(f"{run_uuid}:{vm}")


# --- fakes ---------------------------------------------------------------------------


class Clock:
    """The conductor's clock: its sleeps pass time at once."""

    def __init__(self):
        from django.utils import timezone

        self.t = timezone.now()

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.advance(seconds)

    def advance(self, seconds):
        from django.utils import timezone

        self.t += timezone.timedelta(seconds=seconds)


class FakePower:
    def __init__(self, broken=()):
        self.stopped, self.started = [], []
        self.state = {}
        self.broken = set(broken)  # VMs whose power control fails

    def _check(self, vm):
        from orchestrator.rounds.power import PowerError

        if vm.vm_identifier in self.broken:
            raise PowerError("the API refused")

    def stop(self, vm, wait=False):
        self._check(vm)
        self.stopped.append(vm.vm_identifier)
        self.state[vm.vm_identifier] = "stopped"

    def start(self, vm, wait=False):
        self._check(vm)
        self.started.append(vm.vm_identifier)
        self.state[vm.vm_identifier] = "running"

    def shutdown(self, vm, timeout=60):
        self.stop(vm)

    def suspend(self, vm):
        self.state[vm.vm_identifier] = "paused"

    def resume(self, vm):
        self.state[vm.vm_identifier] = "running"

    def status(self, vm):
        return self.state.get(vm.vm_identifier, "running")


class Agent:
    """The round agent: every host job answered at once, as the real one answers it. `fail`
    makes a kind of job fail (a number of times, or always); `hold` leaves it queued (the agent
    has not got to it); `armed_at` is what a deploy job reports."""

    def __init__(self, directory, clock):
        self.directory, self.clock = directory, clock
        self.fail, self.hold, self.armed_at = {}, set(), None

    def request(self, run, kind, **params):
        from core.models.rounds import HostJob

        job = HostJob.objects.create(run=run, kind=kind, params=params)
        if kind not in self.hold:
            self.answer(job)
        return job

    def run(self, run, kind, **params):
        from core.models.rounds import HostJob
        from gameplan.definition import RoundError

        job = self.request(run, kind, **params)
        if job.state != HostJob.State.DONE:
            raise RoundError(f"{kind} job failed: {job.error}")
        return job.result

    def answer(self, job):
        from core.models.rounds import HostJob
        from django.utils import timezone

        left = self.fail.get(job.kind, 0)
        if left:
            self.fail[job.kind] = left if left is True else left - 1
            job.state, job.error = HostJob.State.FAILED, f"the {job.kind} job failed: its log on the node says why"
        else:
            job.state, job.result = HostJob.State.DONE, self.result(job)
        job.finished_at = timezone.now()
        job.save()

    def spec(self, name):
        from gameplan import loader
        from gameplan.runtime import instantiate

        loaded = loader.load(self.directory, name)
        return loaded, instantiate(loaded.cls).spec()

    def result(self, job):
        from core.models.rounds import HostJob
        from gameplan import build, loader

        K, params, run = HostJob.Kind, job.params, job.run
        if job.kind == K.GAMEPLANS:
            return {"gameplans": [{"file": name, "sha256": loader.read(self.directory, name)[1], "valid": True}
                                  for name in loader.available(self.directory)]}  # fmt: skip
        if job.kind == K.PREPARE:
            loaded, spec = self.spec(params["gameplan"])
            assert loaded.sha256 == params["gameplan_sha256"]  # the node runs only the pinned version
            networks = build.vm_networks(spec)
            return {
                "topology": build.topology(spec),
                "entrypoint": spec["entrypoint"],
                "released": [{"vm": vm["name"], "flag_hash": flag_hash(run.uuid, vm["name"])} for vm in spec["vms"]],
                "vms": [{"name": vm["name"], "maintenance_ip": vm["maintenance"].split("/")[0], "vmid": 900 + i,
                         "addresses": [nic["address"].split("/")[0] for nic in vm["nics"]],
                         "networks": networks.get(vm["name"], [])}
                        for i, vm in enumerate(spec["vms"])],
            }  # fmt: skip
        if job.kind == K.CHECK:
            return {"ok": True, "failures": []}
        if job.kind == K.DEPLOY:
            armed = self.armed_at if self.armed_at is not None else self.clock.now().isoformat()
            return {"start_vm": params["start_vm"], "armed_at": armed}
        if job.kind == K.REMOVE_FLAGS:
            return {"removed": params["vms"], "failed": []}
        if job.kind == K.PLACE_FLAGS:
            return {"released": [{"vm": vm, "flag_hash": flag_hash(run.uuid, vm)} for vm in params["vms"]], "failed": []}
        if job.kind == K.TEARDOWN:
            return {"destroyed": not params["keep"], "kept": params["keep"]}
        return {}


# --- fixtures ------------------------------------------------------------------------


@pytest.fixture
def gameplans(tmp_path, request):
    """The orchestrator's gameplans: two.py."""
    from django.test import override_settings

    (tmp_path / "two.py").write_text(GAMEPLAN)
    settings = override_settings(GAMEPLAN_DIR=str(tmp_path), GAMEPLAN_LIB=str(REPO / "src"))
    settings.enable()
    request.addfinalizer(settings.disable)
    return tmp_path


def make_team(competition, name, upload_before=None):
    from core.models.team import Team, TeamMembership
    from core.models.user import User
    from core.models.worms import MalwareArtifact, Upload

    user = User.objects.create(username=f"u-{name}")
    team = Team.objects.create(name=name, competition=competition)
    TeamMembership.objects.create(user=user, team=team)
    if upload_before is not None:
        artifact = MalwareArtifact.objects.create(content_hash=sha(name), storage_key=f"k-{name}")
        Upload.objects.create(artifact=artifact, user=user, team=team, created_at=upload_before)
    return team


@pytest.fixture
def one_run(db_clean, gameplans):
    """A competition with one team that uploaded, a round past its deadline (two.py, 600 s per
    team), and the team's run."""
    from core.models.competition import ApplicationSetting, Competition, Round
    from django.utils import timezone
    from orchestrator.rounds.planner import plan_round

    competition = Competition.objects.create(name="c", registration_starts_at=timezone.now(), registration_ends_at=timezone.now())
    ApplicationSetting.objects.create(key="current", current_competition=competition)
    past = timezone.now() - timezone.timedelta(minutes=1)
    team = make_team(competition, "team-a", upload_before=past - timezone.timedelta(minutes=5))
    round_obj = Round.objects.create(seed=5, name="R1", competition=competition, round_start=past,
                                     round_end=past + timezone.timedelta(days=1), gameplan="two.py", team_duration=600)  # fmt: skip
    [run] = plan_round(round_obj)
    return {"round": round_obj, "team": team, "run": run, "competition": competition}


def make_conductor(gameplans, power=None, clock=None):
    from orchestrator.rounds.conductor import Conductor

    clock = clock or Clock()
    agent = Agent(gameplans, clock)
    conductor = Conductor(power=power or FakePower(), now=clock.now, sleep=clock.sleep, jobs=agent)
    conductor.cfg = {**conductor.cfg, "worm_start_delay": 10, "end_drain": 5}
    return conductor, agent, clock


def attempts(round_obj, team):
    from core.models.rounds import RoundRun

    return list(RoundRun.objects.filter(round=round_obj, team=team).order_by("attempt"))


@pytest.fixture
def ran(one_run, gameplans):
    """The team's first attempt, run start to finish."""
    from core.models.rounds import RoundRun

    conductor, agent, clock = make_conductor(gameplans)
    assert conductor.tick() is True
    run = RoundRun.objects.get(pk=one_run["run"].pk)
    assert run.state == RoundRun.State.FINISHED, run.error
    return {**one_run, "run": run, "conductor": conductor, "agent": agent, "clock": clock}


# --- the conductor ---------------------------------------------------------------------


def test_conductor_runs_a_full_round(one_run, gameplans):
    """A round without a gameplan waits for one; then the team's attempt runs start to finish."""
    from core.models.competition import Round
    from core.models.rounds import HostJob, RoundRun, RunEvent

    run, round_obj = one_run["run"], one_run["round"]
    power = FakePower()
    conductor, agent, clock = make_conductor(gameplans, power)

    Round.objects.filter(pk=round_obj.pk).update(gameplan="")
    assert conductor.tick() is False and conductor.tick() is False
    run.refresh_from_db()
    assert run.state == RoundRun.State.PENDING and not run.jobs.exists()
    Round.objects.filter(pk=round_obj.pk).update(gameplan="two.py")

    start = clock.now()
    assert conductor.tick() is True
    run.refresh_from_db()
    assert run.state == RoundRun.State.FINISHED, run.error
    # Pinned: this version of the gameplan, for every attempt still to come.
    assert run.gameplan_sha256 == sha(GAMEPLAN) and run.duration == 600
    assert Round.objects.get(pk=round_obj.pk).gameplan_status == "ok"
    kinds = list(run.jobs.order_by("id").values_list("kind", flat=True))
    assert kinds == ["prepare", "check", "remove_flags", "deploy", "place_flags", "teardown"]
    assert run.jobs.get(kind=HostJob.Kind.TEARDOWN).params["keep"] is False
    # The sandbox as built, for the replay.
    assert run.entrypoint == "sandbox-vm0" and run.topology["nodes"]
    assert sorted(run.virtual_machines.values_list("vm_identifier", flat=True)) == ["sandbox-vm0", "sandbox-vm1"]
    # The worm started after its fuse; the run lasted its duration from then.
    assert run.worm_start_at == start + clock_delta(10)
    assert run.ends_at == run.worm_start_at + clock_delta(600)
    # The gameplan's stop, then every VM at the end; vm1's flag is the one placed back.
    assert power.stopped[0] == "sandbox-vm0" and set(power.stopped[1:]) == {"sandbox-vm0", "sandbox-vm1"}
    vm1 = run.virtual_machines.get(vm_identifier="sandbox-vm1")
    assert vm1.ctf_flag_hash == flag_hash(run.uuid, "sandbox-vm1")
    events = list(run.events.order_by("id").values_list("kind", flat=True))
    for kind in (RunEvent.Kind.FLAG_REMOVED, RunEvent.Kind.WORM_DEPLOYED, RunEvent.Kind.WORM_STARTED,
                 RunEvent.Kind.VM_STOPPED, RunEvent.Kind.SECRET_RELEASED):  # fmt: skip
        assert kind in events, kind
    states = [e.data["state"] for e in run.events.filter(kind=RunEvent.Kind.STATE).order_by("id")]
    assert states == ["preparing", "checking", "running", "ending", "tearing_down", "finished"]
    assert conductor.tick() is False  # nothing left


def test_capture_is_recorded_when_a_released_secret_is_submitted(ran):
    from core.models.rounds import RunEvent
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from orchestrator.rounds import captures

    run, team = ran["run"], ran["team"]
    vm = run.virtual_machines.get(vm_identifier="sandbox-vm0")
    FlagCaptureAttempt.objects.create(team_uuid=str(team.uuid), vm_flag_hash=vm.ctf_flag_hash,
                                      created_at=run.worm_start_at + clock_delta(30))  # fmt: skip
    captures.process_flags(ran["conductor"].cfg["capture_grace"])
    assert FlagCapture.objects.filter(team=team, vm=vm).count() == 1
    assert FlagCapture.published().filter(team=team, vm=vm).count() == 1
    assert run.events.filter(kind=RunEvent.Kind.CAPTURED, vm=vm).exists()

    # A second submission of the same flag does not count twice.
    FlagCaptureAttempt.objects.create(team_uuid=str(team.uuid), vm_flag_hash=vm.ctf_flag_hash,
                                      created_at=run.worm_start_at + clock_delta(40))  # fmt: skip
    captures.process_flags(ran["conductor"].cfg["capture_grace"])
    assert FlagCapture.objects.filter(team=team, vm=vm).count() == 1


def test_wrong_team_does_not_capture(ran):
    import uuid as uuidlib

    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from orchestrator.rounds import captures

    run = ran["run"]
    vm = run.virtual_machines.get(vm_identifier="sandbox-vm0")
    # Some other team's uuid: the flag is right but the team's run does not own this VM.
    FlagCaptureAttempt.objects.create(team_uuid=str(uuidlib.uuid4()), vm_flag_hash=vm.ctf_flag_hash,
                                      created_at=run.worm_start_at + clock_delta(30))  # fmt: skip
    captures.process_flags(ran["conductor"].cfg["capture_grace"])
    assert FlagCapture.objects.count() == 0


def test_a_failed_attempt_is_thrown_away_and_the_team_runs_again(one_run, gameplans):
    """An attempt the infrastructure fails is thrown away (its sandbox torn down) and the
    team's worm runs again, at most max_retries more times; the next attempt finishes."""
    from core.models.rounds import HostJob, RoundRun

    conductor, agent, clock = make_conductor(gameplans)
    agent.fail[HostJob.Kind.PREPARE] = 1
    assert conductor.tick() is True
    first, second = attempts(one_run["round"], one_run["team"])
    assert first.state == RoundRun.State.FAILED and "prepare job failed" in first.error
    assert first.jobs.filter(kind=HostJob.Kind.TEARDOWN).exists()  # it may have built half a sandbox
    assert (second.state, second.attempt, second.upload_id) == (RoundRun.State.PENDING, 1, first.upload_id)
    assert second.gameplan_sha256 == first.gameplan_sha256
    assert conductor.tick() is True
    second.refresh_from_db()
    assert second.state == RoundRun.State.FINISHED, second.error

    # Never more than max_retries more: in the next round every check fails.
    from core.models.competition import Round
    from orchestrator.rounds.planner import plan_round

    r2 = Round.objects.create(seed=6, name="R2", competition=one_run["competition"], round_start=one_run["round"].round_start,
                              round_end=one_run["round"].round_end, gameplan="two.py", team_duration=600)  # fmt: skip
    plan_round(r2)
    agent.fail[HostJob.Kind.CHECK] = True
    while conductor.tick():
        pass
    runs = attempts(r2, one_run["team"])
    assert [r.state for r in runs] == [RoundRun.State.FAILED] * (conductor.cfg["max_retries"] + 1)


def test_next_run_waits_for_the_previous_teardown(one_run, gameplans):
    """A failed attempt tears its sandbox down asynchronously; the next must not build over it
    until that teardown has completed (docs/07-security.md, S7)."""
    from core.models.rounds import HostJob, RoundRun

    conductor, agent, clock = make_conductor(gameplans)
    agent.fail[HostJob.Kind.CHECK] = 1
    agent.hold = {HostJob.Kind.TEARDOWN}
    assert conductor.tick() is True
    first, second = attempts(one_run["round"], one_run["team"])
    assert first.state == RoundRun.State.FAILED
    teardown = first.jobs.get(kind=HostJob.Kind.TEARDOWN)
    assert teardown.state == HostJob.State.QUEUED

    # The teardown is still pending: the retry waits.
    assert conductor.tick() is False
    second.refresh_from_db()
    assert second.state == RoundRun.State.PENDING

    # Teardown done -> the retry runs.
    agent.hold = set()
    agent.answer(teardown)
    assert conductor.tick() is True
    second.refresh_from_db()
    assert second.state == RoundRun.State.FINISHED


def _two_teams(one_run, second_upload=True):
    from core.models.rounds import RoundRun
    from django.utils import timezone
    from orchestrator.rounds.planner import plan_round

    deadline = one_run["round"].round_start
    make_team(one_run["competition"], "team-b", upload_before=deadline - timezone.timedelta(minutes=5) if second_upload else None)
    RoundRun.objects.all().delete()
    return plan_round(one_run["round"])


def test_the_runs_of_a_round_share_its_sandbox(one_run, gameplans):
    """Built once from the round's gameplan and rolled back for every team: the same sandbox
    for all, and the teardown in between keeps it."""
    from core.models.rounds import HostJob

    first, second = _two_teams(one_run)
    conductor, agent, clock = make_conductor(gameplans)
    assert conductor.tick() is True and conductor.tick() is True
    prepares = HostJob.objects.filter(kind=HostJob.Kind.PREPARE).order_by("id")
    assert [(job.params["gameplan"], job.params["gameplan_sha256"]) for job in prepares] == [("two.py", sha(GAMEPLAN))] * 2
    teardowns = HostJob.objects.filter(kind=HostJob.Kind.TEARDOWN).order_by("id")
    assert [(job.run_id, job.params["keep"]) for job in teardowns] == [(first.id, True), (second.id, False)]


def test_no_sandbox_is_kept_for_a_run_that_has_nothing_to_run(one_run, gameplans):
    from core.models.rounds import HostJob

    first = next(run for run in _two_teams(one_run, second_upload=False) if run.upload_id)
    conductor, agent, clock = make_conductor(gameplans)
    while conductor.tick():
        pass
    assert [job.params["keep"] for job in first.jobs.filter(kind=HostJob.Kind.TEARDOWN)] == [False]


def test_a_sandbox_kept_for_a_run_that_is_then_skipped_is_destroyed(one_run, gameplans):
    """The next team's run was given up: nothing is left to roll the sandbox back."""
    from core.models.rounds import HostJob, RoundRun

    first, second = _two_teams(one_run)
    conductor, agent, clock = make_conductor(gameplans)
    assert conductor.tick() is True
    RoundRun.objects.filter(pk=second.pk).update(state=RoundRun.State.SKIPPED)
    agent.hold = {HostJob.Kind.TEARDOWN}
    assert conductor.tick() is True
    final = first.jobs.filter(kind=HostJob.Kind.TEARDOWN).order_by("-id").first()
    assert final.params == {"keep": False, "run_uuid": str(first.uuid)} and final.state == HostJob.State.QUEUED
    assert conductor.tick() is False  # it waits for that teardown, and asks for no other
    agent.answer(final)
    assert conductor.tick() is False
    assert first.jobs.filter(kind=HostJob.Kind.TEARDOWN).count() == 2


def test_run_without_upload_is_skipped(one_run, gameplans):
    from core.models.rounds import RoundRun

    _first, second = sorted(_two_teams(one_run, second_upload=False), key=lambda r: r.upload_id is None)
    assert second.upload_id is None
    conductor, agent, clock = make_conductor(gameplans)
    while conductor.tick():
        pass
    second.refresh_from_db()
    assert second.state == RoundRun.State.SKIPPED and second.error == "no upload before the round's start"
    assert not second.jobs.exists()


def test_timeline_export(ran):
    from orchestrator.rounds import timeline

    data = timeline.build(ran["run"])
    assert len(data["nodes"]) == 2
    assert data["run"]["start_vm"] == "sandbox-vm0"
    kinds = {e["kind"] for e in data["events"]}
    assert {"state", "vm_stopped", "secret_released", "flag_removed"} <= kinds
    # The event timeline is ordered by t.
    ts = [e["t"] for e in data["events"]]
    assert ts == sorted(ts)
    assert [e["id"] for e in data["events"]] == list(range(len(data["events"])))
    # Every VM is running once built, stopped at ENDING, destroyed at the end.
    for vm_id, points in data["vm_status"].items():
        states = [state for _, state in points]
        assert states[0] == "running" and states[-1] == "destroyed", (vm_id, states)
        assert "stopped" in states
    assert data["run"]["t_min"] <= 0 <= data["run"]["t_max"]


def test_timeline_maps_flows_and_derives_submissions(ran):
    """The player's data: flows to scoring / the private internet are mapped to infrastructure
    nodes through the live snapshot's address index; flag submissions and logs become events on
    scoring / logging, attributed to the VM that connected just before (the server itself only
    sees sandbox-gw)."""
    import infra_fixture
    from core.models.logging import LoggingAttempt
    from core.models.monitoring import NetworkFlow
    from core.models.scoring import FlagCaptureAttempt
    from dashboard.infra_view import address_index, analyze
    from django.core.management import call_command
    from django.utils import timezone
    from orchestrator.rounds import timeline

    run, team = ran["run"], ran["team"]
    start = run.worm_start_at
    at = lambda s: start + timezone.timedelta(seconds=s)  # noqa: E731
    vm0 = run.virtual_machines.get(vm_identifier="sandbox-vm0")
    vm1 = run.virtual_machines.get(vm_identifier="sandbox-vm1")
    ip0, ip1 = vm0.addresses[0], vm1.addresses[0]

    def flow(uid, s, src, dst, dport, **kw):
        NetworkFlow.objects.create(uid=uid, ts=at(s), src_ip=src, dst_ip=dst, src_port=40000, dst_port=dport,
                                   transport="tcp", conn_state="SF", duration=0.5, **kw)  # fmt: skip

    flow("C1", 5, ip0, ip1, 22)  # vm0 -> vm1 (lan)
    flow("C2", 7, ip1, "10.2.0.14", 443, service="ssl")  # vm1 -> scoring
    flow("C3", 8, ip0, "10.2.0.20", 3142)  # vm0 -> apt-cache
    flow("C4", 9, ip0, "10.2.0.15", 443)  # vm0 -> logging
    flow("C5", 6, "10.99.0.1", "10.99.0.2", 80)  # not this run
    FlagCaptureAttempt.objects.create(team_uuid=str(team.uuid), vm_flag_hash=vm0.ctf_flag_hash, created_at=at(7.5))
    FlagCaptureAttempt.objects.create(team_uuid=str(team.uuid), vm_flag_hash="0" * 64, created_at=at(20))
    FlagCaptureAttempt.objects.create(team_uuid="someone-else", vm_flag_hash="1" * 64, created_at=at(21))
    log = LoggingAttempt.objects.create(team_uuid=str(team.uuid), log_data={"msg": "<img src=x onerror=alert(1)>"}, created_at=at(9.2))
    call_command("process_attempts", "--once", "--grace", "120")

    infra = address_index(analyze(infra_fixture.snapshot()))
    data = timeline.build(run, infra=infra)

    by_uid = {f["uid"]: f for f in data["flows"]}
    assert set(by_uid) == {"C1", "C2", "C3", "C4"}
    assert by_uid["C1"]["kind"] == "lan" and by_uid["C1"]["dst"] == str(vm1.uuid)
    assert by_uid["C2"]["kind"] == "scoring" and by_uid["C2"]["dst"] == "vm:104" and by_uid["C2"]["src"] == str(vm1.uuid)
    assert by_uid["C3"]["kind"] == "private_internet" and by_uid["C3"]["dst_name"] == "apt-cache"
    assert by_uid["C2"]["t"] == 7.0 and by_uid["C2"]["d"] == 0.5

    flags = [e for e in data["events"] if e["kind"] == "flag_submitted"]
    assert [f["data"]["result"] for f in flags] == ["captured", "invalid"]  # the other team's is not shown
    assert flags[0]["node"] == "role:scoring"
    assert flags[0]["data"]["flag_of"] == str(vm0.uuid)
    # Submitted by vm1 (its connection to scoring at t=7, just before 7.5).
    assert flags[0]["data"]["source_vm"] == str(vm1.uuid) and flags[0]["data"]["source_inferred"]
    assert flags[1]["data"]["source_vm"] is None  # no connection within the window
    captured = [e for e in data["events"] if e["kind"] == "captured"]
    assert captured and captured[0]["node"] == str(vm0.uuid)
    assert next(n for n in data["nodes"] if n["id"] == str(vm0.uuid))["captured"]

    logs = [e for e in data["events"] if e["kind"] == "log_received"]
    assert len(logs) == 1 and logs[0]["node"] == "role:logging"
    assert logs[0]["data"]["source_vm"] == str(vm0.uuid) and logs[0]["data"]["stored"]
    # A log is a reference to its record (fetched on demand), with its size: the record itself
    # is not in the timeline.
    assert logs[0]["data"]["log"]["ref"] == str(log.uuid) and logs[0]["data"]["log"]["size"] > 20
    assert "onerror" not in json.dumps(data)

    # Without the live index the far end is still reported, by address.
    bare = {f["uid"]: f for f in timeline.build(run)["flows"]}
    assert bare["C2"]["dst"] == "ip:10.2.0.14" and bare["C2"]["kind"] == "other"

    # The browser test's synthetic timeline (tests/topology_fixtures.py) has exactly this
    # schema, so the page is tested against what is served.
    import topology_fixtures

    fx = topology_fixtures.timeline()
    assert set(data) == set(fx)
    assert set(data["run"]) == set(fx["run"])
    assert set(data["nodes"][0]) == set(fx["nodes"][0])
    assert set(data["flows"][0]) == set(fx["flows"][0])
    assert set(data["events"][0]) == set(fx["events"][0])
    for kind in ("flag_submitted", "log_received"):
        real = next(e for e in data["events"] if e["kind"] == kind)
        synthetic = next(e for e in fx["events"] if e["kind"] == kind)
        assert set(real["data"]) == set(synthetic["data"]), kind


def test_topology_endpoints_are_staff_only_and_serve_the_player(ran, request):
    """/topology/data/, /topology/runs/ and the run's progression: staff only; the model comes
    from the stored snapshot; the runs feed the round/team tabs; the progression resolves
    service addresses through the live snapshot."""
    from django.test import override_settings

    urls = override_settings(ROOT_URLCONF="dashboard.urls")  # the progression links to its flows / logs
    urls.enable()
    request.addfinalizer(urls.disable)

    import infra_fixture
    from core.models.monitoring import NetworkFlow
    from core.models.topology import TopologySnapshot
    from core.models.user import User
    from dashboard.views import progression as progression_views
    from dashboard.views import topology as topology_views
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory
    from django.utils import timezone

    run = ran["run"]
    staff = User.objects.create(username="staff", is_staff=True)
    student = User.objects.create(username="student")
    factory = RequestFactory()

    def call(view, user, *args):
        request = factory.get("/", HTTP_HOST="localhost")
        request.user = user
        return view(request, *args)

    # No snapshot yet: a clear 503, not a crash.
    response = call(topology_views.topology_data, staff)
    assert response.status_code == 503 and "collect_topology" in json.loads(response.content)["error"]

    TopologySnapshot.objects.create(source="proxmox", data=infra_fixture.snapshot(), collected_at=timezone.now())
    topology_views._cache.clear()

    for view, args in [(topology_views.topology_data, ()), (topology_views.topology_runs, ()),
                       (progression_views.run_document, (run.uuid,)), (progression_views.run_flows, (run.uuid,))]:  # fmt: skip
        assert call(view, student, *args).status_code == 403
        assert call(view, AnonymousUser(), *args).status_code == 302  # to the login
        assert call(view, staff, *args).status_code == 200

    data = json.loads(call(topology_views.topology_data, staff).content)
    assert {"nodes", "edges", "groups", "networks", "collected_at", "stale"} <= set(data)
    assert data["stale"] is False
    # Devbox groups carry their team's name (VM names only have t<team id>).
    t1 = next(g for g in data["groups"] if g.get("team") == f"t{ran['team'].id}")
    assert t1["team_name"] == "team-a" and t1["label"] == "Devbox team-a"

    runs = json.loads(call(topology_views.topology_runs, staff).content)
    assert runs["rounds"][0]["name"] == "R1"
    assert runs["rounds"][0]["runs"][0] == {
        "uuid": str(run.uuid), "team": "team-a", "state": run.state, "position": run.position,
        "worm_start_at": run.worm_start_at.isoformat(),
    }  # fmt: skip

    ip0 = run.virtual_machines.get(vm_identifier="sandbox-vm0").addresses[0]
    NetworkFlow.objects.create(uid="V1", ts=run.worm_start_at + timezone.timedelta(seconds=3), src_ip=ip0,
                               dst_ip="10.2.0.14", dst_port=443, transport="tcp")  # fmt: skip
    doc = json.loads(call(progression_views.run_document, staff, run.uuid).content)
    assert doc["format"] == "worms-progression" and doc["audience"] == "staff" and "flows" not in doc["timeline"]
    assert doc["network"]["count"] == 1 and doc["network"]["url"].endswith(f"/rounds/{run.uuid}/progression/flows/")
    flows = json.loads(call(progression_views.run_flows, staff, run.uuid).content)["flows"]
    assert flows[0]["dst"] == "vm:104" and flows[0]["kind"] == "scoring"


@pytest.mark.parametrize("command", ["process_captures", "process_attempts"])
def test_attempt_processing_commands(ran, command):
    """manage.py process_captures / process_attempts --once turn a pending attempt into a
    capture (both are the single-pass entry points; the daemon just loops the latter)."""
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from django.core.management import call_command

    run = ran["run"]
    vm = run.virtual_machines.get(vm_identifier="sandbox-vm0")
    FlagCaptureAttempt.objects.create(team_uuid=str(ran["team"].uuid), vm_flag_hash=vm.ctf_flag_hash,
                                      created_at=run.worm_start_at + clock_delta(30))  # fmt: skip
    args = ["--grace", str(ran["conductor"].cfg["capture_grace"])]
    if command == "process_attempts":
        args.append("--once")
    call_command(command, *args)
    assert FlagCapture.objects.filter(team=ran["team"], vm=vm).count() == 1


# --- deploy timing, failure, late releases (docs/05-live-environment.md) ---------------------


def clock_delta(seconds):
    from django.utils import timezone

    return timezone.timedelta(seconds=seconds)


def test_the_worm_start_is_dated_from_when_the_agent_armed_it(one_run, gameplans):
    """The fuse starts on the VM when the agent arms it, not when the conductor notices the
    deploy job is done (that lag cost early captures)."""
    from core.models.rounds import RoundRun

    conductor, agent, clock = make_conductor(gameplans)
    armed = clock.now() - clock_delta(4)
    agent.armed_at = armed.isoformat()
    conductor.tick()
    run = RoundRun.objects.get(pk=one_run["run"].pk)
    assert run.worm_start_at == armed + clock_delta(10)  # worm_start_delay in make_conductor
    assert run.ends_at == run.worm_start_at + clock_delta(600)


@pytest.mark.parametrize("armed_at", ["", "yesterday", "2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00",
                                      "2026-01-01T00:00:00"])  # fmt: skip
def test_an_implausible_armed_at_is_ignored(one_run, gameplans, armed_at):
    from core.models.rounds import RoundRun

    conductor, agent, clock = make_conductor(gameplans)
    start = clock.now()
    agent.armed_at = armed_at
    conductor.tick()
    run = RoundRun.objects.get(pk=one_run["run"].pk)
    assert run.worm_start_at == start + clock_delta(10)


def test_a_failed_run_stops_its_vms_and_scores_no_more(one_run, gameplans):
    """Power control fails during execute(): the attempt is thrown away, its VMs stopped and
    its window closed; what it captured never counts; the team's worm runs again."""
    from core.models.rounds import HostJob, RoundRun
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from orchestrator.rounds import captures

    power = FakePower()
    conductor, agent, clock = make_conductor(gameplans, power)

    def broken_stop(vm, wait=False):
        from orchestrator.rounds.power import PowerError

        if wait:  # the gameplan's stop (the end and a failure stop without waiting)
            raise PowerError("the API refused")
        FakePower.stop(power, vm)

    power.stop = broken_stop
    conductor.tick()
    first, second = attempts(one_run["round"], one_run["team"])
    assert first.state == RoundRun.State.FAILED and "power control" in first.error
    assert first.ends_at == first.finished_at == first.worm_start_at + clock_delta(60)
    assert set(power.stopped) == {"sandbox-vm0", "sandbox-vm1"}
    assert first.jobs.filter(kind=HostJob.Kind.TEARDOWN).exists()
    assert second.state == RoundRun.State.PENDING

    vm = first.virtual_machines.get(vm_identifier="sandbox-vm0")
    # During the attempt: recorded for staff, never published.
    FlagCaptureAttempt.objects.create(team_uuid=str(one_run["team"].uuid), vm_flag_hash=vm.ctf_flag_hash,
                                      created_at=first.worm_start_at + clock_delta(30))  # fmt: skip
    # After its end and the grace: not even recorded.
    FlagCaptureAttempt.objects.create(team_uuid=str(one_run["team"].uuid), vm_flag_hash=vm.ctf_flag_hash,
                                      created_at=first.ends_at + clock_delta(conductor.cfg["capture_grace"] + 1))  # fmt: skip
    captures.process_flags(conductor.cfg["capture_grace"])
    assert FlagCapture.objects.filter(vm=vm).count() == 1
    assert not FlagCapture.published().filter(vm=vm).exists()


def test_a_flag_submitted_before_its_hash_was_stored_still_counts(ran):
    """The agent writes a flag into the VM before the conductor stores its hash; a worm quick
    enough to submit in between is credited afterwards."""
    from core.models.rounds import RunEvent
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from django.utils import timezone
    from orchestrator.rounds import captures

    run, conductor = ran["run"], ran["conductor"]
    vm = run.virtual_machines.get(vm_identifier="sandbox-vm1")
    vm.ctf_flag_hash = None  # removed, and its new hash not stored yet
    vm.save()
    secret_hash = sha("delayed-" + vm.vm_identifier)
    submitted = FlagCaptureAttempt.objects.create(team_uuid=str(ran["team"].uuid), vm_flag_hash=secret_hash,
                                                  created_at=run.worm_start_at + clock_delta(70))  # fmt: skip
    captures.process_flags(conductor.cfg["capture_grace"])  # no VM has this hash yet
    submitted.refresh_from_db()
    assert submitted.processed and not FlagCapture.objects.filter(vm=vm).exists()

    vm.ctf_flag_hash, vm.secret_released_at = secret_hash, timezone.now()  # the place_flags result arrives
    vm.save()
    assert captures.process_late_releases(conductor.cfg["capture_grace"]) == 1
    capture = FlagCapture.objects.get(vm=vm)
    assert capture.flag_capture_attempt_id == submitted.id and capture.team == ran["team"]
    assert run.events.filter(kind=RunEvent.Kind.CAPTURED, vm=vm).exists()
    assert captures.process_late_releases(conductor.cfg["capture_grace"]) == 0  # once


def test_late_releases_never_credit_another_team(ran):
    import uuid as uuidlib

    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from django.utils import timezone
    from orchestrator.rounds import captures

    run = ran["run"]
    vm = run.virtual_machines.get(vm_identifier="sandbox-vm1")
    secret_hash = sha("other-" + vm.vm_identifier)
    FlagCaptureAttempt.objects.create(team_uuid=str(uuidlib.uuid4()), vm_flag_hash=secret_hash,
                                      created_at=run.worm_start_at + clock_delta(70), processed=True)  # fmt: skip
    vm.ctf_flag_hash, vm.secret_released_at = secret_hash, timezone.now()
    vm.save()
    assert captures.process_late_releases(ran["conductor"].cfg["capture_grace"]) == 0
    assert not FlagCapture.objects.filter(vm=vm).exists()


def test_a_submission_just_before_the_dated_start_counts(ran):
    """The start is dated by the host's clock, submissions by the database's: the grace
    applies at the start as at the end."""
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from orchestrator.rounds import captures

    run = ran["run"]
    vm = run.virtual_machines.get(vm_identifier="sandbox-vm0")
    FlagCaptureAttempt.objects.create(team_uuid=str(ran["team"].uuid), vm_flag_hash=vm.ctf_flag_hash,
                                      created_at=run.worm_start_at - clock_delta(20))  # fmt: skip
    captures.process_flags(ran["conductor"].cfg["capture_grace"])
    assert FlagCapture.objects.filter(vm=vm).count() == 1


# --- the team's own uploads --------------------------------------------------------------


def test_the_entry_is_the_teams_own_latest_upload(db_clean):
    """A member who changes teams does not take their uploads along."""
    from core.models.competition import Competition, Round
    from core.models.team import Team, TeamMembership
    from core.models.user import User
    from core.models.worms import MalwareArtifact, Upload
    from django.utils import timezone
    from orchestrator.rounds.planner import plan_round

    competition = Competition.objects.create(name="c")
    red, blue = (Team.objects.create(name=n, competition=competition) for n in ("red", "blue"))
    mover, stayer = User.objects.create(username="mover"), User.objects.create(username="stayer")
    TeamMembership.objects.create(user=stayer, team=blue)
    start = timezone.now()
    art = [MalwareArtifact.objects.create(content_hash=c * 64, storage_key=c) for c in "abc"]
    red_entry = Upload.objects.create(artifact=art[0], user=mover, team=red, created_at=start - clock_delta(300))
    # The mover leaves red for blue (their red upload stays red's) and uploads for blue.
    TeamMembership.objects.create(user=mover, team=blue)
    blue_entry = Upload.objects.create(artifact=art[1], user=mover, team=blue, created_at=start - clock_delta(200))
    Upload.objects.create(artifact=art[2], user=stayer, team=blue, created_at=start + clock_delta(10))  # too late
    round_obj = Round.objects.create(competition=competition, name="R", seed=1, round_start=start,
                                     round_end=start + clock_delta(86400))  # fmt: skip
    runs = {run.team_id: run for run in plan_round(round_obj)}
    assert runs[red.id].upload_id == red_entry.id
    assert runs[blue.id].upload_id == blue_entry.id
