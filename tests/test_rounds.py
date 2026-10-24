"""Round orchestration: the game plan generator, the planner and the
conductor state machine driven end to end against the test database with a
fake Proxmox (power) and a fake round agent (host jobs).

Nothing here talks to a real Proxmox host; the agent side is simulated exactly
as the real one behaves, so this exercises the conductor's whole lifecycle,
the game plan timing, secret release and capture processing.
"""

from __future__ import annotations

import hashlib
import os
import sys

import pytest

from conftest import REPO

pytest.importorskip("django")
pytest.importorskip("psycopg2")

DASHBOARD = REPO / "projects" / "dashboard"
sys.path.insert(0, str(DASHBOARD))


@pytest.fixture(scope="module")
def django_env(request):
    """Django set up against the test database (module scope: one setup)."""
    from pg import pg_bindir

    if pg_bindir() is None:
        pytest.skip("PostgreSQL server binaries not installed")
    import pg

    bindir = pg_bindir()
    cluster = pg.Cluster(bindir)
    cluster.start()
    try:
        import django

        os.environ.update(
            PRODUCTION="1",
            DJANGO_SETTINGS_MODULE="config.settings.orchestrator",
            DJANGO_SECRET_KEY="test",
            DJANGO_ALLOWED_HOSTS="localhost",
            DB_MIGRATOR_USER=pg.MIGRATOR,
            DB_MIGRATOR_PASSWORD=pg.PASSWORD,
            DB_USER="postgres",
            DB_PASSWORD="trust",
            DB_HOST="127.0.0.1",
            DB_PORT=str(cluster.port),
            DB_NAME=pg.DB_NAME,
            DB_SSLMODE="disable",
            PROXMOX_HOST="x", PROXMOX_USER="x", PROXMOX_TOKEN_NAME="x", PROXMOX_TOKEN_VALUE="x",
        )
        # Create the database and schema (migrations) as the migrator, like the
        # real deployment; then let Django connect as postgres (superuser) so
        # the test can set up fixtures without juggling grants.
        admin = cluster.connect(dbname="postgres")
        with admin.cursor() as cur:
            cur.execute(f"CREATE DATABASE {pg.DB_NAME}")
            cur.execute(f"CREATE ROLE {pg.MIGRATOR} LOGIN PASSWORD %s SUPERUSER", (pg.PASSWORD,))
        admin.close()
        django.setup()
        from django.core.management import call_command

        call_command("migrate", "--no-input", database="migrator", verbosity=0)
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture
def db_clean(django_env):
    """A clean set of round tables for each test."""
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute(
            "TRUNCATE run_events, game_plan_steps, host_jobs, flag_captures, logging,"
            " flag_capture_attempts, logging_attempts, virtual_machines, round_runs,"
            " access_tokens, rounds, team_memberships, teams, uploads, malware_artifacts,"
            " application_setting, competitions, users, network_flows, topology_snapshots"
            " RESTART IDENTITY CASCADE"
        )
    return django_env


# --- game plan ---------------------------------------------------------------


def test_gameplan_is_deterministic_and_well_formed():
    from orchestrator.rounds import gameplan

    vms = [f"vm{i}" for i in range(6)]
    a = gameplan.generate(vms, seed=42, duration=3600)
    b = gameplan.generate(vms, seed=42, duration=3600)
    assert [(s.action, s.vm, s.at_seconds, s.after) for s in a.steps] == [
        (s.action, s.vm, s.at_seconds, s.after) for s in b.steps
    ]
    assert a.start_vm == b.start_vm
    # Every VM's secret is released exactly once.
    released = [s.vm for s in a.steps if s.action == "release_secret"]
    assert sorted(released) == sorted(vms)
    # The start VM's secret is released before the start (at_seconds None).
    start_release = next(s for s in a.steps if s.action == "release_secret" and s.vm == a.start_vm)
    assert start_release.at_seconds is None


def test_gameplan_delayed_secret_follows_a_stop():
    from orchestrator.rounds import gameplan

    plan = gameplan.generate([f"vm{i}" for i in range(6)], seed=7, duration=3600)
    stops = {s.position for s in plan.steps if s.action == "stop_vm"}
    delayed = [s for s in plan.steps if s.action == "release_secret" and s.at_seconds is not None]
    assert delayed, "expected some secrets to be delayed"
    for step in delayed:
        assert step.after, f"delayed secret for {step.vm} has no precondition"
        assert set(step.after) & stops, "a delayed secret must follow a stop"


def test_gameplan_keeps_min_alive():
    from orchestrator.rounds import gameplan

    plan = gameplan.generate([f"vm{i}" for i in range(5)], seed=1, duration=3600, params={"min_alive": 2})
    assert sum(1 for s in plan.steps if s.action == "stop_vm") <= 3


def test_gameplan_single_vm():
    from orchestrator.rounds import gameplan

    plan = gameplan.generate(["only"], seed=1, duration=3600)
    assert plan.start_vm == "only"
    assert [s.action for s in plan.steps] == ["release_secret"]


# --- fakes for the conductor --------------------------------------------------


class FakePower:
    def __init__(self):
        self.stopped, self.started = [], []
        self.state = {}

    def stop(self, vm):
        self.stopped.append(vm.vm_identifier)
        self.state[vm.vm_identifier] = "stopped"

    def start(self, vm):
        self.started.append(vm.vm_identifier)
        self.state[vm.vm_identifier] = "running"

    def status(self, vm):
        return self.state.get(vm.vm_identifier, "running")


def fake_agent(run, sha):
    """Complete every queued host job the way the real agent would."""
    from core.models.rounds import HostJob
    from django.utils import timezone

    for job in run.jobs.filter(state=HostJob.State.QUEUED):
        result = {}
        if job.kind == HostJob.Kind.PREPARE:
            result = {
                "topology": {
                    "nodes": [{"id": "network-1000", "label": "vmbr1000", "type": "network", "cidr": "10.64.0.0/24"}],
                    "edges": [],
                },
                "sandbox_definition": "locals { seed = 0 }",
                "vms": [
                    {
                        "name": f"sandbox-vm{i}",
                        "maintenance_ip": f"10.4.0.{10 + i}",
                        "vmid": 900 + i,
                        "addresses": [f"10.64.0.{10 + i}"],
                        "networks": [{"name": "vmbr1000", "cidr": "10.64.0.0/24"}],
                    }
                    for i in range(4)
                ],
            }
        elif job.kind == HostJob.Kind.CHECK:
            result = {"ok": True, "failures": []}
        elif job.kind == HostJob.Kind.DEPLOY:
            result = {"released": [{"vm": vm, "flag_hash": sha(vm)} for vm in job.params["initial_secrets"]]}
        elif job.kind == HostJob.Kind.RELEASE_SECRETS:
            result = {"released": [{"vm": vm, "flag_hash": sha(vm)} for vm in job.params["vms"]]}
        elif job.kind == HostJob.Kind.TEARDOWN:
            result = {"destroyed": True}
        job.state = HostJob.State.DONE
        job.result = result
        job.finished_at = timezone.now()
        job.save()


def sha(name):
    return hashlib.sha256(name.encode()).hexdigest()


@pytest.fixture
def one_run(db_clean):
    """A competition with one team that uploaded, a due round, and its run."""
    from core.models.competition import ApplicationSetting, Competition, Round
    from core.models.team import AccessTokens, Team, TeamMembership
    from core.models.user import User
    from core.models.worms import MalwareArtifact, Upload
    from django.utils import timezone
    from orchestrator.rounds.planner import plan_round

    competition = Competition.objects.create(name="c", registration_starts_at=timezone.now(), registration_ends_at=timezone.now())
    ApplicationSetting.objects.create(key="current", current_competition=competition)
    user = User.objects.create(username="u")
    team = Team.objects.create(name="team-a", competition=competition)
    TeamMembership.objects.create(user=user, team=team)
    token = "tok-a"
    AccessTokens.objects.create(name="t", access_token_hash=hashlib.sha256(token.encode()).hexdigest(), team=team, created_by=user)
    artifact = MalwareArtifact.objects.create(content_hash="abc", storage_key="k")
    past = timezone.now() - timezone.timedelta(minutes=1)
    # Upload before the round's deadline (round_start).
    Upload.objects.create(artifact=artifact, user=user, created_at=past - timezone.timedelta(minutes=5))
    round = Round.objects.create(seed=5, name="R1", competition=competition, round_start=past, round_end=past + timezone.timedelta(days=1))
    # Short game-plan intervals so stops and delayed secrets happen within the
    # 600 s test round (the conductor's round_duration below).
    runs = plan_round(round, sandbox_params={"game_plan": {"first_stop_after": 60, "stop_interval": 60, "min_alive": 1}})
    return {"round": round, "team": team, "token": token, "run": runs[0]}


def drive(conductor, run, agent_run, max_ticks=200, clock=None, step=10):
    """Tick the conductor until the run is finished, running the fake agent
    between ticks and letting a little time pass each tick. Returns the number
    of ticks."""
    from core.models.rounds import RoundRun

    for i in range(max_ticks):
        conductor.tick()
        if clock is not None:
            clock.advance(step)
        fake_agent(agent_run, sha)
        agent_run.refresh_from_db()
        run.refresh_from_db()
        if run.state in (RoundRun.State.FINISHED, RoundRun.State.FAILED, RoundRun.State.SKIPPED):
            return i + 1
    raise AssertionError(f"run did not finish: {run.state}")


def make_conductor(power, clock):
    from orchestrator.rounds.conductor import Conductor

    conductor = Conductor(power=power, now=clock.now)
    conductor.cfg = {**conductor.cfg, "worm_start_delay": 10, "round_duration": 600, "end_drain": 5}
    return conductor


class Clock:
    def __init__(self):
        from django.utils import timezone

        self.t = timezone.now()

    def now(self):
        return self.t

    def advance(self, seconds):
        from django.utils import timezone

        self.t += timezone.timedelta(seconds=seconds)


def test_conductor_runs_a_full_round(one_run):
    from core.models.rounds import GamePlanStep, RoundRun, RunEvent

    run = one_run["run"]
    power, clock = FakePower(), Clock()
    conductor = make_conductor(power, clock)

    # Prepare, check, deploy.
    for _ in range(6):
        conductor.tick()
        fake_agent(run, sha)
        run.refresh_from_db()
        if run.state == RoundRun.State.ARMED:
            break
    assert run.state == RoundRun.State.ARMED
    assert run.virtual_machines.count() == 4
    assert run.steps.count() > 0
    # The full topology and the generated OpenTofu config are saved on the run.
    assert run.topology.get("nodes") and run.sandbox_definition == "locals { seed = 0 }"
    # Start VM's secret is released at deploy.
    start_vm = run.virtual_machines.get(vm_identifier=run.sandbox_params["start_vm"])
    assert start_vm.ctf_flag_hash == sha(start_vm.vm_identifier)

    # Fuse: worm starts.
    clock.advance(11)
    conductor.tick()
    run.refresh_from_db()
    assert run.state == RoundRun.State.RUNNING
    assert run.events.filter(kind=RunEvent.Kind.WORM_STARTED).exists()

    # Run the game plan to the end, letting time pass gradually (30 s/tick)
    # so the game plan's stops and delayed-secret jobs unfold before the end.
    drive(conductor, run, run, max_ticks=80, clock=clock, step=30)
    assert run.state == RoundRun.State.FINISHED
    # Every game-plan stop happened (the end-drain also stops the rest).
    plan_stops = {s.vm.vm_identifier for s in run.steps.filter(action=GamePlanStep.Action.STOP_VM)}
    assert plan_stops and plan_stops.issubset(set(power.stopped))
    assert run.events.filter(kind=RunEvent.Kind.VM_STOPPED).count() >= len(plan_stops)
    assert run.virtual_machines.filter(ctf_flag_hash__isnull=True).count() == 0
    assert not run.steps.filter(state=GamePlanStep.State.PENDING).exists()


def test_capture_is_recorded_when_a_released_secret_is_submitted(one_run):
    from core.models.rounds import RoundRun, RunEvent
    from core.models.scoring import FlagCapture, FlagCaptureAttempt

    run = one_run["run"]
    power, clock = FakePower(), Clock()
    conductor = make_conductor(power, clock)
    for _ in range(6):
        conductor.tick()
        fake_agent(run, sha)
        run.refresh_from_db()
        if run.state == RoundRun.State.ARMED:
            break
    clock.advance(11)
    conductor.tick()
    run.refresh_from_db()

    # The worm submits the start VM's flag during the run, recorded with the
    # running team's uuid (as the scoring API now does), created_at on the
    # conductor's clock so it is live.
    from orchestrator.rounds import captures

    vm = run.virtual_machines.get(vm_identifier=run.sandbox_params["start_vm"])
    clock.advance(30)
    FlagCaptureAttempt.objects.create(
        team_uuid=str(one_run["team"].uuid),
        vm_flag_hash=vm.ctf_flag_hash,
        created_at=clock.now(),
    )
    captures.process_flags(conductor.cfg["capture_grace"])
    assert FlagCapture.objects.filter(team=one_run["team"], vm=vm).count() == 1
    assert run.events.filter(kind=RunEvent.Kind.CAPTURED, vm=vm).exists()

    # A second submission of the same flag does not count twice.
    FlagCaptureAttempt.objects.create(
        team_uuid=str(one_run["team"].uuid),
        vm_flag_hash=vm.ctf_flag_hash,
        created_at=clock.now(),
    )
    captures.process_flags(conductor.cfg["capture_grace"])
    assert FlagCapture.objects.filter(team=one_run["team"], vm=vm).count() == 1


def test_wrong_team_does_not_capture(one_run):
    import uuid as uuidlib

    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from orchestrator.rounds import captures

    run = one_run["run"]
    power, clock = FakePower(), Clock()
    conductor = make_conductor(power, clock)
    for _ in range(6):
        conductor.tick()
        fake_agent(run, sha)
        run.refresh_from_db()
        if run.state == "armed":
            break
    clock.advance(11)
    conductor.tick()
    run.refresh_from_db()
    vm = run.virtual_machines.get(vm_identifier=run.sandbox_params["start_vm"])
    # Some other team's uuid: the flag is right but the team's run does not own
    # this VM, so it is not scored.
    FlagCaptureAttempt.objects.create(team_uuid=str(uuidlib.uuid4()), vm_flag_hash=vm.ctf_flag_hash, created_at=clock.now())
    captures.process_flags(conductor.cfg["capture_grace"])
    assert FlagCapture.objects.count() == 0


def test_failed_prepare_job_fails_the_run_without_teardown(one_run):
    from core.models.rounds import HostJob, RoundRun

    run = one_run["run"]
    power, clock = FakePower(), Clock()
    conductor = make_conductor(power, clock)
    conductor.tick()  # -> PREPARING, prepare job queued
    run.refresh_from_db()
    job = run.jobs.get(kind=HostJob.Kind.PREPARE)
    job.state, job.error = HostJob.State.FAILED, "no storage"
    job.save()
    conductor.tick()
    run.refresh_from_db()
    assert run.state == RoundRun.State.FAILED
    # Nothing was built, so no teardown job.
    assert not run.jobs.filter(kind=HostJob.Kind.TEARDOWN).exists()


def test_run_without_upload_is_skipped(db_clean):
    from core.models.competition import ApplicationSetting, Competition, Round
    from core.models.rounds import RoundRun
    from core.models.team import Team
    from django.utils import timezone
    from orchestrator.rounds.planner import plan_round

    competition = Competition.objects.create(name="c", registration_starts_at=timezone.now(), registration_ends_at=timezone.now())
    ApplicationSetting.objects.create(key="current", current_competition=competition)
    Team.objects.create(name="no-upload", competition=competition)
    past = timezone.now() - timezone.timedelta(minutes=1)
    round = Round.objects.create(seed=1, name="R1", competition=competition, round_start=past, round_end=past + timezone.timedelta(days=1))
    run = plan_round(round)[0]
    assert run.upload_id is None

    power, clock = FakePower(), Clock()
    conductor = make_conductor(power, clock)
    conductor.tick()
    run.refresh_from_db()
    assert run.state == RoundRun.State.SKIPPED


def test_timeline_export(one_run):
    from core.models.rounds import RoundRun
    from orchestrator.rounds import timeline

    run = one_run["run"]
    power, clock = FakePower(), Clock()
    conductor = make_conductor(power, clock)
    for _ in range(6):
        conductor.tick()
        fake_agent(run, sha)
        run.refresh_from_db()
        if run.state == RoundRun.State.ARMED:
            break
    clock.advance(11)
    conductor.tick()
    clock.advance(600)
    drive(conductor, run, run, max_ticks=80, clock=clock, step=10)

    data = timeline.build(run)
    assert len(data["nodes"]) == 4
    assert data["run"]["start_vm"]
    kinds = {e["kind"] for e in data["events"]}
    assert "state" in kinds and "vm_stopped" in kinds and "secret_released" in kinds
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


def test_timeline_maps_flows_and_derives_submissions(one_run):
    """The player's data: flows to scoring / the private internet are mapped
    to infrastructure nodes through the live snapshot's address index; flag
    submissions and logs become events on scoring / logging, attributed to the
    VM that connected just before (the server itself only sees sandbox-gw)."""
    import infra_fixture
    from core.models.logging import LoggingAttempt
    from core.models.monitoring import NetworkFlow
    from core.models.rounds import RoundRun
    from core.models.scoring import FlagCaptureAttempt
    from dashboard.infra_view import address_index, analyze
    from django.core.management import call_command
    from django.utils import timezone
    from orchestrator.rounds import timeline

    run = one_run["run"]
    power, clock = FakePower(), Clock()
    conductor = make_conductor(power, clock)
    for _ in range(6):
        conductor.tick()
        fake_agent(run, sha)
        run.refresh_from_db()
        if run.state == RoundRun.State.ARMED:
            break
    clock.advance(11)
    conductor.tick()
    run.refresh_from_db()
    start = run.worm_start_at
    at = lambda s: start + timezone.timedelta(seconds=s)  # noqa: E731
    vm0 = run.virtual_machines.get(vm_identifier="sandbox-vm0")
    vm1 = run.virtual_machines.get(vm_identifier="sandbox-vm1")

    def flow(uid, s, src, dst, dport, **kw):
        NetworkFlow.objects.create(uid=uid, ts=at(s), src_ip=src, dst_ip=dst, src_port=40000, dst_port=dport,
                                   transport="tcp", conn_state="SF", duration=0.5, **kw)

    flow("C1", 5, "10.64.0.10", "10.64.0.11", 22)  # vm0 -> vm1 (lan)
    flow("C2", 7, "10.64.0.11", "10.2.0.14", 443, service="ssl")  # vm1 -> scoring
    flow("C3", 8, "10.64.0.10", "10.2.0.20", 3142)  # vm0 -> apt-cache
    flow("C4", 9, "10.64.0.10", "10.2.0.15", 443)  # vm0 -> logging
    flow("C5", 6, "10.99.0.1", "10.99.0.2", 80)  # not this run
    FlagCaptureAttempt.objects.create(team_uuid=str(one_run["team"].uuid), vm_flag_hash=vm0.ctf_flag_hash or sha("sandbox-vm0"), created_at=at(7.5))
    FlagCaptureAttempt.objects.create(team_uuid=str(one_run["team"].uuid), vm_flag_hash="0" * 64, created_at=at(20))
    FlagCaptureAttempt.objects.create(team_uuid="someone-else", vm_flag_hash="1" * 64, created_at=at(21))
    LoggingAttempt.objects.create(team_uuid=str(one_run["team"].uuid), log_data={"msg": "<img src=x onerror=alert(1)>"}, created_at=at(9.2))
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
    # Attacker-controlled content is passed through as data (the player
    # renders it as text), never interpreted here.
    assert logs[0]["data"]["log"]["data"] == {"msg": "<img src=x onerror=alert(1)>"}

    # Without the live index the far end is still reported, by address.
    bare = {f["uid"]: f for f in timeline.build(run)["flows"]}
    assert bare["C2"]["dst"] == "ip:10.2.0.14" and bare["C2"]["kind"] == "other"

    # The browser test's synthetic timeline (tests/topology_fixtures.py) has
    # exactly this schema, so the page is tested against what is served.
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


def test_topology_endpoints_are_staff_only_and_serve_the_player(one_run):
    """/topology/data/, /topology/runs/ and the run timeline: staff only; the
    model comes from the stored snapshot; the runs feed the round/team tabs;
    the timeline resolves service addresses through the live snapshot."""
    import json as _json

    import infra_fixture
    from core.models.monitoring import NetworkFlow
    from core.models.rounds import RoundRun
    from core.models.topology import TopologySnapshot
    from core.models.user import User
    from dashboard.views import rounds as rounds_views
    from dashboard.views import topology as topology_views
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory
    from django.utils import timezone

    run = one_run["run"]
    power, clock = FakePower(), Clock()
    conductor = make_conductor(power, clock)
    for _ in range(6):
        conductor.tick()
        fake_agent(run, sha)
        run.refresh_from_db()
        if run.state == RoundRun.State.ARMED:
            break
    staff = User.objects.create(username="staff", is_staff=True)
    student = User.objects.create(username="student")
    factory = RequestFactory()

    def call(view, user, *args):
        request = factory.get("/", HTTP_HOST="localhost")
        request.user = user
        return view(request, *args)

    # No snapshot yet: a clear 503, not a crash.
    response = call(topology_views.topology_data, staff)
    assert response.status_code == 503 and "collect_topology" in _json.loads(response.content)["error"]

    TopologySnapshot.objects.create(source="proxmox", data=infra_fixture.snapshot(), collected_at=timezone.now())
    topology_views._cache.clear()

    for view, args in [(topology_views.topology_data, ()), (topology_views.topology_runs, ()),
                       (rounds_views.round_timeline_data, (run.uuid,))]:
        assert call(view, student, *args).status_code == 403
        assert call(view, AnonymousUser(), *args).status_code == 302  # to the login
        assert call(view, staff, *args).status_code == 200

    data = _json.loads(call(topology_views.topology_data, staff).content)
    assert {"nodes", "edges", "groups", "networks", "collected_at", "stale"} <= set(data)
    assert data["stale"] is False
    # Devbox groups carry their team's name (VM names only have t<team id>).
    t1 = next(g for g in data["groups"] if g.get("team") == f"t{one_run['team'].id}")
    assert t1["team_name"] == "team-a" and t1["label"] == "Devbox team-a"

    runs = _json.loads(call(topology_views.topology_runs, staff).content)
    assert runs["rounds"][0]["name"] == "R1"
    assert runs["rounds"][0]["runs"][0] == {
        "uuid": str(run.uuid), "team": "team-a", "state": run.state, "position": run.position,
        "worm_start_at": run.worm_start_at.isoformat(),
    }

    NetworkFlow.objects.create(uid="V1", ts=run.worm_start_at + timezone.timedelta(seconds=3), src_ip="10.64.0.10",
                               dst_ip="10.2.0.14", dst_port=443, transport="tcp")
    timeline = _json.loads(call(rounds_views.round_timeline_data, staff, run.uuid).content)
    assert timeline["flows"][0]["dst"] == "vm:104" and timeline["flows"][0]["kind"] == "scoring"


def test_timeline_truncates_large_logs(one_run):
    from orchestrator.rounds import timeline

    small = timeline._preview({"a": 1})
    assert small == {"data": {"a": 1}, "truncated": False}
    big = timeline._preview({"a": "x" * 10000})
    assert big["truncated"] and len(big["data"]) == timeline.LOG_PREVIEW and big["size"] > 10000


@pytest.mark.parametrize("command", ["process_captures", "process_attempts"])
def test_attempt_processing_commands(one_run, command):
    """manage.py process_captures / process_attempts --once turn a pending
    attempt into a capture (both are the single-pass entry points; the daemon
    just loops the latter)."""
    from core.models.rounds import RoundRun
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from django.core.management import call_command

    run = one_run["run"]
    power, clock = FakePower(), Clock()
    conductor = make_conductor(power, clock)
    for _ in range(6):
        conductor.tick()
        fake_agent(run, sha)
        run.refresh_from_db()
        if run.state == RoundRun.State.ARMED:
            break
    clock.advance(11)
    conductor.tick()
    run.refresh_from_db()
    vm = run.virtual_machines.get(vm_identifier=run.sandbox_params["start_vm"])
    FlagCaptureAttempt.objects.create(
        team_uuid=str(one_run["team"].uuid),
        vm_flag_hash=vm.ctf_flag_hash,
        created_at=clock.now(),
    )
    args = ["--grace", str(conductor.cfg["capture_grace"])]
    if command == "process_attempts":
        args.append("--once")
    call_command(command, *args)
    assert FlagCapture.objects.filter(team=one_run["team"], vm=vm).count() == 1
