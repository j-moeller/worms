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
            " application_setting, competitions, users RESTART IDENTITY CASCADE"
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
                "topology": {"networks": [{"name": "vmbr1000", "cidr": "10.64.0.0/24"}]},
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

    # The worm submits the start VM's flag during the run (created_at on the
    # conductor's clock, as the scoring API would stamp it live).
    vm = run.virtual_machines.get(vm_identifier=run.sandbox_params["start_vm"])
    clock.advance(30)
    FlagCaptureAttempt.objects.create(
        team_token_hash=hashlib.sha256(one_run["token"].encode()).hexdigest(),
        vm_flag_hash=vm.ctf_flag_hash,
        created_at=clock.now(),
    )
    conductor.tick()
    assert FlagCapture.objects.filter(team=one_run["team"], vm=vm).count() == 1
    assert run.events.filter(kind=RunEvent.Kind.CAPTURED, vm=vm).exists()

    # A second submission of the same flag does not count twice.
    FlagCaptureAttempt.objects.create(
        team_token_hash=hashlib.sha256(one_run["token"].encode()).hexdigest(),
        vm_flag_hash=vm.ctf_flag_hash,
    )
    conductor.tick()
    assert FlagCapture.objects.filter(team=one_run["team"], vm=vm).count() == 1


def test_wrong_team_token_does_not_capture(one_run):
    from core.models.scoring import FlagCapture, FlagCaptureAttempt

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
    FlagCaptureAttempt.objects.create(team_token_hash=hashlib.sha256(b"someone-else").hexdigest(), vm_flag_hash=vm.ctf_flag_hash, created_at=clock.now())
    clock.advance(30)
    conductor.tick()
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


def test_process_captures_command(one_run):
    """manage.py process_captures turns a pending attempt into a capture."""
    import hashlib

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
        team_token_hash=hashlib.sha256(one_run["token"].encode()).hexdigest(),
        vm_flag_hash=vm.ctf_flag_hash,
        created_at=clock.now(),
    )
    call_command("process_captures", "--grace", str(conductor.cfg["capture_grace"]))
    assert FlagCapture.objects.filter(team=one_run["team"], vm=vm).count() == 1
