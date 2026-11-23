"""Round orchestration against the test database: the planner, and the conductor running each
team's attempt by the round's gameplan (src/gameplan), with a fake Proxmox API (under the real
power control) and a fake round agent that answers host jobs as the real one does
(scripts/round_agent/handlers.py, the sandbox described by the real gameplan build). An attempt
the infrastructure fails is run again; only a finished attempt's captures count; what the worm
does to its VMs never fails one (docs/05-live-environment.md).
"""

from __future__ import annotations

import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from conftest import REPO

pytest.importorskip("django")
pytest.importorskip("psycopg2")

DASHBOARD = REPO / "projects" / "dashboard"
sys.path.insert(0, str(DASHBOARD))
sys.path.insert(0, str(REPO / "src"))  # the gameplan framework, as the orchestrator's GAMEPLAN_LIB has it

from orchestrator.rounds.power import ProxmoxPower  # noqa: E402

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
    """The conductor's clock: its sleeps pass time at once. `hooks`: called each time it moves
    (the fake agent, ending the job it is on when that is due)."""

    def __init__(self):
        from django.utils import timezone

        self.t = timezone.now()
        self.hooks = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.advance(seconds)

    def advance(self, seconds):
        from django.utils import timezone

        self.t += timezone.timedelta(seconds=seconds)
        for hook in self.hooks:
            hook()


class FakeProxmox:
    """The Proxmox API as proxmoxer gives it (nodes(n).qemu(vmid).status.<...>), for the VMs the
    fake agent built (`names`, vmid -> name): every request done at once, the VMs asked to stop
    or start in `stopped` and `started`. `at` changes a VM that nobody asked to, at a time of the
    clock (the worm powering it off, the host's OOM killer), with the cause the host's records
    give; a state that is QEMU's own (internal-error) is a running QEMU in that state. `flaky`
    makes that many calls fail as a dropped connection does. `goes_off`: {name: cause}, a VM that
    is off by the time the next start or suspend of it is done (a worm on its disk powering off as
    it boots; a poweroff under way, which QEMU ends as it stops the VM). `task_fails`: the VMs whose
    start task fails. Power control's pauses between tries pass the clock's time."""

    def __init__(self, clock=None):
        self.clock = clock
        self.names, self.state, self.cause = {}, {}, {}
        self.stopped, self.started = [], []
        self.changes = []  # (when, name, state, cause)
        self.flaky, self.waited = 0, 0.0
        self.goes_off, self.task_fails, self.tasks_read = {}, set(), []

    def pause(self, seconds):
        self.waited += seconds
        if self.clock is not None:
            self.clock.advance(seconds)

    def monotonic(self):
        return self.waited

    def built(self, vms):
        """{vmid: name}: the sandbox as prepare leaves it, every VM running."""
        self.names.update(vms)
        for name in vms.values():
            self.state[name], self.cause[name] = "running", None

    def at(self, when, name, state, cause):
        self.changes.append((when, name, state, cause))

    def vm(self, name):
        """A VM's state as the host has it, with the changes due by now."""
        now = self.clock.now() if self.clock is not None else None
        for change in [change for change in self.changes if now is not None and change[0] <= now]:
            self.changes.remove(change)
            _when, vm, self.state[vm], self.cause[vm] = change
        return self.state.get(name, "running")

    def nodes(self, node):
        return self

    def tasks(self, upid):
        def status():
            self._request()
            self.tasks_read.append(upid)
            name = upid.split(":")[2]
            return {"upid": upid, "status": "stopped", "exitstatus": "start failed: QEMU exited with code 1"
                    if name in self.task_fails else "OK"}  # fmt: skip

        return SimpleNamespace(status=SimpleNamespace(get=status))

    def qemu(self, vmid):
        vmid = int(vmid)

        def action(name):
            return SimpleNamespace(post=lambda **params: self._act(vmid, name))

        status = SimpleNamespace(current=SimpleNamespace(get=lambda: self._current(vmid)),
                                 **{name: action(name) for name in ("start", "stop", "shutdown", "suspend", "resume")})  # fmt: skip
        return SimpleNamespace(status=status)

    def _request(self):
        import requests

        if self.flaky:
            self.flaky -= 1
            raise requests.exceptions.ConnectionError("Connection reset by peer")

    def _current(self, vmid):
        self._request()
        name = self.names.get(vmid)
        state = self.vm(name)
        return {"vmid": vmid, "name": name, "status": "stopped" if state == "stopped" else "running", "qmpstatus": state}

    def _act(self, vmid, action):
        from proxmoxer.core import ResourceException

        self._request()
        name = self.names[vmid]
        state = self.vm(name)
        if action in ("stop", "shutdown"):
            self.stopped.append(name)
            self.state[name], self.cause[name] = "stopped", "host"
        elif action == "start":
            self.started.append(name)
            if name not in self.task_fails:  # else it stays stopped
                self.state[name], self.cause[name] = "running", None
        elif state == "stopped":  # as Proxmox answers a suspend or resume of a VM that is not running
            raise ResourceException(500, "Internal Server Error", f"VM {vmid} not running")
        elif state in ("running", "paused"):
            self.state[name] = "paused" if action == "suspend" else "running"
        # else (internal-error): QMP's stop and cont leave a QEMU its guest halted as it is
        if action in ("start", "suspend") and name in self.goes_off:
            self.state[name], self.cause[name] = "stopped", self.goes_off.pop(name)
        return f"UPID:fake:{name}:{action}:"


def fake_power(clock=None):
    """The orchestrator's power control as it is (orchestrator/rounds/power.py), on FakeProxmox."""
    proxmox = FakeProxmox(clock)
    return ProxmoxPower(api=proxmox, sleep=proxmox.pause, monotonic=proxmox.monotonic)


class Agent:
    """The host's steps: every one answered at once, as the real handlers answer it, and waited
    for on the clock. `fail` makes a kind of job fail (a
    number of times, or always); `hold` leaves it queued (the agent has not got to it);
    `armed_at` is what a deploy job reports. `after_deploy`: [(seconds, VM, state, cause)], what
    happens to a VM that long after the worm is deployed (the worm, the host). `busy_after_deploy`:
    (seconds, duration), that long after the deploy the agent is on a team's devbox rebuild for
    `duration` seconds, and every job asked for meanwhile waits its turn (one job at a time;
    `waited`: kind -> the seconds each waited). `preflight_failures`: the preflight checks that
    fail; `devboxes_running`: what an observe job finds until a hold_devboxes job shuts them down."""

    MEMORY = {"available_mb": 40960, "total_mb": 131072, "psi_some_avg10": 0.0, "psi_full_avg10": 0.0,
              "ksm_sharing_mb": 0, "arc_mb": 8192}  # fmt: skip

    def __init__(self, directory, clock, proxmox):
        self.directory, self.clock, self.proxmox = directory, clock, proxmox
        self.conductor = None  # make_conductor's
        self.fail, self.hold, self.armed_at = {}, set(), None
        self.after_deploy, self.preflight_failures, self.devboxes_running = [], set(), []
        self.busy_after_deploy, self.busy_from, self.busy, self.queue, self.waited = None, None, None, [], {}
        clock.hooks.append(self.tick)

    def request(self, run, kind, **params):
        from core.models.rounds import HostJob

        job = HostJob.objects.create(run=run, kind=kind, params=params, requested_at=self.clock.now())
        if kind in self.hold:
            pass
        elif self.busy is not None:
            self.queue.append((job, self.clock.now()))
        else:
            self.answer(job)
        return job

    def run(self, run, kind, within=None, **params):
        """The job's result, waited for on the clock as a queue's would be (waited)."""
        from core.models.rounds import HostJob
        from orchestrator.rounds.conductor import waited

        counted = self.conductor.now()
        job = self.request(run, kind, **params)
        while True:
            job.refresh_from_db()
            if job.state == HostJob.State.DONE:
                return job.result or {}
            counted = waited(self.conductor, job, counted, within)
            self.conductor.sleep(self.conductor.cfg["job_poll"])

    def tick(self):
        """The clock moved: the devbox job the agent is on (its heartbeat fresh) ends when it is
        due, and the jobs that waited their turn are answered."""
        from core.models.rounds import HostJob
        from django.utils import timezone

        now = self.clock.now()
        if self.busy is None and self.busy_from is not None and now >= self.busy_from[0]:
            job = HostJob.objects.create(kind=HostJob.Kind.DEVBOX_REBUILD, params={"team_uuid": "a team"},
                                         state=HostJob.State.RUNNING, started_at=timezone.now(), heartbeat_at=timezone.now())  # fmt: skip
            self.busy, self.busy_from = (self.busy_from[1], job), None
        if self.busy is None:
            return
        until, job = self.busy
        if now < until:
            HostJob.objects.filter(pk=job.pk).update(heartbeat_at=timezone.now())
            return
        HostJob.objects.filter(pk=job.pk).update(state=HostJob.State.DONE, finished_at=timezone.now())
        queue, self.busy, self.queue = self.queue, None, []
        for job, asked in queue:
            self.waited.setdefault(job.kind, []).append((now - asked).total_seconds())
            self.answer(job)

    def answer(self, job):
        from core.models.rounds import HostJob
        from django.utils import timezone

        left = self.fail.get(job.kind, 0)
        if left:
            self.fail[job.kind] = left if left is True else left - 1
            job.state, job.error = HostJob.State.FAILED, f"the {job.kind} job failed: its log on the host says why"
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
        if job.kind == K.PREFLIGHT:
            loaded, spec = self.spec(params["gameplan"])
            assert loaded.sha256 == params["gameplan_sha256"]
            names = ["host:internet-window-closed", "host:proxmox-firewall", "sandbox:not-running", "memory:admission",
                     "packages:in-cache"] + (["devboxes:held"] if params["hold_devboxes"] else [])  # fmt: skip
            checks = [{"check": name, "ok": name not in self.preflight_failures,
                       "detail": "not enough" if name in self.preflight_failures else ""} for name in names]  # fmt: skip
            failures = [check["check"] for check in checks if not check["ok"]]
            needed = 1024 * len(spec["vms"]) + 512
            return {"ok": not failures, "failures": failures, "checks": checks, "warnings": [],
                    "memory": {"available_mb": self.MEMORY["available_mb"], "needed_mb": needed, "reserve_mb": 1024,
                               "total_mb": self.MEMORY["total_mb"]}}  # fmt: skip
        if job.kind == K.PREPARE:
            loaded, spec = self.spec(params["gameplan"])
            assert loaded.sha256 == params["gameplan_sha256"]  # the host runs only the pinned version
            networks = build.vm_networks(spec)
            # Built, or rolled back to its snapshot: every VM runs, the gateway too.
            self.proxmox.built({**{900 + i: vm["name"] for i, vm in enumerate(spec["vms"])}, 899: "sandbox-gw"})
            return {
                "topology": build.topology(spec),
                "entrypoint": spec["entrypoint"],
                "released": [{"vm": vm["name"], "flag_hash": flag_hash(run.uuid, vm["name"])} for vm in spec["vms"]],
                "vms": [{"name": vm["name"], "maintenance_ip": vm["maintenance"].split("/")[0], "vmid": 900 + i,
                         "addresses": [nic["address"].split("/")[0] for nic in vm["nics"]],
                         "networks": networks.get(vm["name"], [])}
                        for i, vm in enumerate(spec["vms"])],
                "gateway": {"name": "sandbox-gw", "vmid": 899},
            }  # fmt: skip
        if job.kind == K.CHECK:
            return {"ok": True, "failures": []}
        if job.kind == K.DEPLOY:
            armed = self.armed_at if self.armed_at is not None else self.clock.now().isoformat()
            for seconds, vm, state, cause in self.after_deploy:
                self.proxmox.at(self.clock.now() + clock_delta(seconds), vm, state, cause)
            if self.busy_after_deploy is not None:
                seconds, duration = self.busy_after_deploy
                self.busy_from = (self.clock.now() + clock_delta(seconds), self.clock.now() + clock_delta(seconds + duration))
            self.after_deploy, self.busy_after_deploy = [], None
            return {"start_vm": params["start_vm"], "armed_at": armed}
        if job.kind == K.OBSERVE:
            vms = []
            for vmid in params["vmids"]:
                name = self.proxmox.names.get(vmid)
                qemu = self.proxmox.vm(name)
                # As the agent has it (host_state.state): a QEMU its guest halted is paused.
                status = qemu if qemu in ("running", "paused", "stopped") else "paused"
                vms.append({"vmid": vmid, "name": name, "status": status, "detail": "",
                            "cause": None if status == "running" else self.proxmox.cause.get(name) or "unknown"})  # fmt: skip
            return {"vms": vms, "memory": dict(self.MEMORY), "devboxes_running": list(self.devboxes_running),
                    "oom_adjusted": [vm["vmid"] for vm in vms if vm["status"] != "stopped"]}  # fmt: skip
        if job.kind == K.HOLD_DEVBOXES:
            stopped, self.devboxes_running = self.devboxes_running, []
            return {"stopped": stopped, "held": stopped}
        if job.kind == K.RELEASE_DEVBOXES:
            return {"started": []}
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
    power = power or fake_power(clock)
    power.api().clock = power.api().clock or clock
    agent = Agent(gameplans, clock, power.api())
    conductor = agent.conductor = Conductor(power=power, now=clock.now, sleep=clock.sleep, jobs=agent)
    conductor.cfg = {**conductor.cfg, "worm_start_delay": 10, "end_drain": 5}
    return conductor, agent, clock


def run_round(conductor, round_obj):
    """What `worms round start` does with the conductor (worms_cli/rounds.py): the round's gameplan
    pinned, then each team's attempt still to come, in order, the host's preflight before each.
    Returns (the attempts as they ended, why the round stopped or None)."""
    from core.models.rounds import RoundRun

    pending = RoundRun.objects.filter(round=round_obj, state=RoundRun.State.PENDING)
    problem = conductor.pin(round_obj) if pending.filter(upload__isnull=False).exists() else None  # none to run: no gameplan needed
    if problem:
        return [], problem
    ended = []
    for run in RoundRun.objects.filter(round=round_obj, state=RoundRun.State.PENDING).select_related("team", "round") \
            .order_by("position", "attempt"):  # fmt: skip
        if run.upload_id is not None:
            problem = conductor.preflight(run)
            if problem:
                return ended, problem
        ended.append(conductor.attempt(run))
    return ended, None


def publish(round_obj):
    from core.models.competition import Round
    from django.utils import timezone

    Round.objects.filter(pk=round_obj.pk).update(results_published_at=timezone.now())


def attempts(round_obj, team):
    from core.models.rounds import RoundRun

    return list(RoundRun.objects.filter(round=round_obj, team=team).order_by("attempt"))


@pytest.fixture
def ran(one_run, gameplans):
    """The team's first attempt, run start to finish, and the round's results published."""
    from core.models.rounds import RoundRun

    conductor, agent, clock = make_conductor(gameplans)
    assert run_round(conductor, one_run["round"])[1] is None
    run = RoundRun.objects.get(pk=one_run["run"].pk)
    assert run.state == RoundRun.State.FINISHED, run.error
    publish(one_run["round"])
    return {**one_run, "run": run, "conductor": conductor, "agent": agent, "clock": clock}


# --- the conductor ---------------------------------------------------------------------


def test_conductor_runs_a_full_round(one_run, gameplans):
    """A round without a gameplan does not start: it says so (for the staff pages too). Another
    round's team runs start to finish, with the upload from before its round's start: the host's
    preflight before it, its VMs watched while it runs. A VM the worm powers off, or whose QEMU it
    halts (a KVM internal error), is the worm's doing (the host says so), and a dropped connection
    to Proxmox is tried again: none of these fails the attempt. Nothing starts or stops a devbox."""
    from core.models.competition import Round
    from core.models.rounds import HostJob, RoundRun, RunEvent
    from core.models.worms import MalwareArtifact, Upload
    from django.utils import timezone
    from orchestrator.rounds.planner import plan_round

    run, round_obj = one_run["run"], one_run["round"]
    power = fake_power()
    conductor, agent, clock = make_conductor(gameplans, power)

    Round.objects.filter(pk=round_obj.pk).update(gameplan="")
    later = Round.objects.create(seed=6, name="R2", competition=one_run["competition"], gameplan="two.py", team_duration=600,
                                 round_start=round_obj.round_start + timezone.timedelta(seconds=30), round_end=round_obj.round_end)  # fmt: skip
    [later_run] = plan_round(later)
    assert run_round(conductor, round_obj) == ([], "it has no gameplan: staff choose one on its page (Staff -> Rounds)")
    waiting = Round.objects.get(pk=round_obj.pk)
    assert waiting.gameplan_status == "" and waiting.gameplan_checked_at is not None  # looked at, for the staff pages
    # In R2's attempt the worm powers vm1 off (20 s after it starts), and Proxmox drops the
    # orchestrator's first two requests.
    agent.after_deploy = [(30, "sandbox-vm1", "stopped", "guest")]
    power.api().flaky = 2
    assert run_round(conductor, later)[1] is None
    later_run.refresh_from_db()
    assert later_run.state == RoundRun.State.FINISHED, later_run.error
    assert power.api().waited == 2 * ProxmoxPower.RETRY_DELAY  # each tried again, 2 s later
    off = later_run.events.get(kind=RunEvent.Kind.VM_STOPPED, vm__vm_identifier="sandbox-vm1")
    assert off.data == {"how": "guest"}  # on its timeline; it stayed off, nothing else came of it
    explained = later_run.jobs.filter(kind=HostJob.Kind.OBSERVE, params__vmids=[901]).get()
    assert set(explained.params) == {"vmids", "since"}  # the host's records since it last was as it should be
    assert power.api().stopped == ["sandbox-vm0"]  # the gameplan's stop; at the end both were off already
    assert not later_run.events.filter(kind=RunEvent.Kind.ERROR).exists()
    run.refresh_from_db()
    assert run.state == RoundRun.State.PENDING and not run.jobs.exists()

    # An upload after the round's start does not take part, however late the gameplan comes.
    Upload.objects.create(artifact=MalwareArtifact.objects.create(content_hash=sha("late"), storage_key="k-late"),
                          user=one_run["team"].users.first(), team=one_run["team"], created_at=timezone.now())  # fmt: skip
    Round.objects.filter(pk=round_obj.pk).update(gameplan="two.py")
    power.api().stopped.clear()
    # The worm halts vm1 20 s into its run: its QEMU runs on, stopped for good (internal-error).
    agent.after_deploy = [(30, "sandbox-vm1", "internal-error", "guest")]

    start = clock.now()
    assert run_round(conductor, round_obj)[1] is None
    run.refresh_from_db()
    assert run.state == RoundRun.State.FINISHED, run.error
    assert run.upload_id == one_run["run"].upload_id
    # Pinned: this version of the gameplan, for every attempt still to come.
    assert run.gameplan_sha256 == sha(GAMEPLAN) and run.duration == 600
    assert Round.objects.get(pk=round_obj.pk).gameplan_status == "ok"
    kinds = list(run.jobs.order_by("id").values_list("kind", flat=True))
    assert [kind for kind in kinds if kind != "observe"] == ["preflight", "prepare", "check", "remove_flags", "deploy",
                                                             "place_flags", "teardown"]  # fmt: skip
    # Watched from the check to the run's end (the fuse, then 600 s): an observe job before the
    # gameplan, then one a minute; and one for vm1, found stopped.
    every = run.jobs.filter(kind=HostJob.Kind.OBSERVE, params__vmids=[899, 900, 901])
    assert every.count() == 1 + (10 + 600) // conductor.cfg["observe_interval"]
    assert kinds.count("observe") == every.count() + 1
    halted = run.events.get(kind=RunEvent.Kind.VM_STOPPED, vm__vm_identifier="sandbox-vm1")
    assert halted.data == {"how": "guest"} and run.jobs.filter(kind=HostJob.Kind.OBSERVE, params__vmids=[901]).exists()
    assert run.jobs.get(kind=HostJob.Kind.PREFLIGHT).params == {"gameplan": "two.py", "gameplan_sha256": sha(GAMEPLAN),
                                                                 "hold_devboxes": True}  # fmt: skip
    assert run.jobs.get(kind=HostJob.Kind.CHECK).params == {"run_uuid": str(run.uuid)}  # its probes inside the VMs
    # Nothing touched a devbox: the operator stopped them before the round.
    assert not HostJob.objects.filter(kind__in=[HostJob.Kind.HOLD_DEVBOXES, HostJob.Kind.RELEASE_DEVBOXES]).exists()
    # The sandbox kept, powered off, for another attempt: the session destroys it at its end.
    assert run.jobs.get(kind=HostJob.Kind.TEARDOWN).params["keep"] is True
    # The sandbox as built, for the replay.
    assert run.entrypoint == "sandbox-vm0" and run.topology["nodes"]
    assert sorted(run.virtual_machines.values_list("vm_identifier", flat=True)) == ["sandbox-vm0", "sandbox-vm1"]
    # The worm started after its fuse; the run lasted its duration from then.
    assert run.worm_start_at == start + clock_delta(10)
    assert run.ends_at == run.worm_start_at + clock_delta(600)
    # The gameplan's stop, then vm1 at the end (its halted QEMU; vm0 is off already); vm1's flag is
    # the one placed back.
    assert power.api().stopped == ["sandbox-vm0", "sandbox-vm1"]
    vm1 = run.virtual_machines.get(vm_identifier="sandbox-vm1")
    assert vm1.ctf_flag_hash == flag_hash(run.uuid, "sandbox-vm1")
    events = list(run.events.order_by("id").values_list("kind", flat=True))
    for kind in (RunEvent.Kind.FLAG_REMOVED, RunEvent.Kind.WORM_DEPLOYED, RunEvent.Kind.WORM_STARTED,
                 RunEvent.Kind.VM_STOPPED, RunEvent.Kind.SECRET_RELEASED):  # fmt: skip
        assert kind in events, kind
    states = [e.data["state"] for e in run.events.filter(kind=RunEvent.Kind.STATE).order_by("id")]
    assert states == ["preparing", "checking", "running", "ending", "tearing_down", "finished"]
    assert run_round(conductor, round_obj) == ([], None)  # nothing left
    assert conductor.destroy_sandbox() == {"destroyed": True, "kept": False}
    assert HostJob.objects.filter(kind=HostJob.Kind.TEARDOWN).order_by("-id").first().params == {"keep": False}


def test_capture_is_recorded_when_a_released_secret_is_submitted(ran):
    from core.models.competition import Round
    from core.models.rounds import RoundRun, RunEvent
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
    # Until the round's results are published (`worms round publish`), no team sees it.
    Round.objects.filter(pk=run.round_id).update(results_published_at=None)
    assert not FlagCapture.published().filter(team=team, vm=vm).exists()
    assert not RoundRun.objects.get(pk=run.pk).shown_to_team()
    publish(run.round)
    assert RoundRun.objects.get(pk=run.pk).shown_to_team()

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
    """An attempt the infrastructure fails is thrown away (its VMs stopped, its sandbox kept,
    powered off) and nothing runs it again by itself: the operator runs a new attempt (`worms round
    start --resume`, Conductor.again), with the same upload and gameplan, and it finishes. A VM the
    host's OOM killer takes while the worm runs is such a failure: the host's records say so, the
    worm has no say; so are the gateway stopped on the host, the host not answering an observe
    job, and a failed deploy. An attempt left under way by a session that was killed fails as the
    next session starts (abandon), not counted as the attempt's own failure."""
    from core.models.competition import Round
    from core.models.rounds import HostJob, RoundRun, RunEvent
    from orchestrator.rounds.planner import plan_round

    conductor, agent, clock = make_conductor(gameplans)
    agent.fail[HostJob.Kind.PREPARE] = 1
    ended, problem = run_round(conductor, one_run["round"])
    [first] = ended
    assert problem is None and first.state == RoundRun.State.FAILED and "prepare job failed" in first.error, first.error
    assert first.jobs.get(kind=HostJob.Kind.TEARDOWN).params["keep"] is True  # it may have built half a sandbox
    assert attempts(one_run["round"], one_run["team"]) == [first]  # no retry by itself
    assert run_round(conductor, one_run["round"]) == ([], None)  # nothing pending
    second = conductor.again(first)
    assert (second.state, second.attempt, second.upload_id) == (RoundRun.State.PENDING, 1, first.upload_id)
    assert second.gameplan_sha256 == first.gameplan_sha256
    with pytest.raises(ValueError):
        conductor.again(second)  # only a failed attempt is run again
    [second] = run_round(conductor, one_run["round"])[0]
    assert second.state == RoundRun.State.FINISHED, second.error

    # The failures, one attempt each: the host's OOM killer takes vm1 30 s after the deploy (20 s
    # into the worm's run); the host does not answer the observe job before the gameplan; a task
    # on the host stops the gateway; the deploy job fails.
    oom = [(30, "sandbox-vm1", "stopped", "oom")]
    r2 = Round.objects.create(seed=6, name="R2", competition=one_run["competition"], round_start=one_run["round"].round_start,
                              round_end=one_run["round"].round_end, gameplan="two.py", team_duration=600)  # fmt: skip
    plan_round(r2)
    runs = []
    for index in range(4):
        agent.after_deploy = {2: [(30, "sandbox-gw", "stopped", "host")], 3: []}.get(index, oom)
        agent.hold = {HostJob.Kind.OBSERVE} if index == 1 else set()
        agent.fail[HostJob.Kind.DEPLOY] = int(index == 3)
        runs.append(run_round(conductor, r2)[0][0] if index == 0 else conductor.attempt(conductor.again(runs[-1])))
    agent.hold = set()
    assert [r.state for r in runs] == [RoundRun.State.FAILED] * 4
    assert "sandbox-vm1 is stopped, it should be running: killed by the host's OOM killer (cause oom)" in runs[0].error, runs[0].error
    assert runs[0].ends_at == runs[0].worm_start_at + clock_delta(20)  # its window closed when the watch saw it
    explained = runs[0].jobs.filter(kind=HostJob.Kind.OBSERVE, params__vmids=[901])
    assert explained.count() == 1 and "since" in explained.get().params
    assert runs[1].error == ("the host did not say how the sandbox's VMs are: observe job not done "
                             f"{conductor.cfg['observe_timeout']} s after the agent could take it (queued)")  # fmt: skip
    assert runs[1].worm_start_at is None and not runs[1].jobs.filter(kind=HostJob.Kind.DEPLOY).exists()
    assert runs[2].error.startswith("the sandbox's gateway sandbox-gw is stopped, it should be running: stopped by a task on "
                                    "the host (cause host"), runs[2].error  # fmt: skip
    assert runs[3].error.startswith("deploy job failed") and runs[3].worm_start_at is None, runs[3].error
    errors = [r.events.get(kind=RunEvent.Kind.ERROR).data for r in runs]
    assert [(error["ours"], error["deploy_failed"]) for error in errors] == [(True, False)] * 3 + [(False, True)]

    # Left under way by a session that was stopped: failed as the next one starts.
    left = conductor.again(runs[3])
    RoundRun.objects.filter(pk=left.pk).update(state=RoundRun.State.PREPARING)
    left.refresh_from_db()
    conductor.abandon(left)
    left.refresh_from_db()
    assert left.state == RoundRun.State.FAILED and "left under way" in left.error
    assert left.events.get(kind=RunEvent.Kind.ERROR).data["conductor_stopped"] is True


def test_a_failed_preflight_starts_no_attempt(one_run, gameplans):
    """The host's preflight before each attempt: one that fails stops the round there, and no
    attempt is used up. Once it passes, an observe job that waits its turn behind another job of
    the host's (a team's devbox rebuild) is not late, however long that takes."""
    from core.models.rounds import HostJob, RoundRun

    conductor, agent, clock = make_conductor(gameplans)
    cfg = conductor.cfg
    agent.preflight_failures = {"memory:admission"}
    ended, problem = run_round(conductor, one_run["round"])
    assert ended == [] and problem == "preflight checks failed: memory:admission (not enough)"
    run = RoundRun.objects.get(pk=one_run["run"].pk)
    assert run.state == RoundRun.State.PENDING and set(run.jobs.values_list("kind", flat=True)) == {HostJob.Kind.PREFLIGHT}
    agent.preflight_failures = set()
    agent.busy_after_deploy = (100, 300)
    [run] = run_round(conductor, one_run["round"])[0]
    assert run.state == RoundRun.State.FINISHED, run.error
    assert max(agent.waited["observe"]) > cfg["observe_timeout"]  # it waited its turn, longer than its own time


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
    for all, kept by every teardown; the session destroys it at its end, asked."""
    from core.models.rounds import HostJob

    first, second = _two_teams(one_run)
    conductor, agent, clock = make_conductor(gameplans)
    assert [run.state for run in run_round(conductor, one_run["round"])[0]] == ["finished", "finished"]
    prepares = HostJob.objects.filter(kind=HostJob.Kind.PREPARE).order_by("id")
    assert [(job.params["gameplan"], job.params["gameplan_sha256"]) for job in prepares] == [("two.py", sha(GAMEPLAN))] * 2
    teardowns = HostJob.objects.filter(kind=HostJob.Kind.TEARDOWN).order_by("id")
    assert [(job.run_id, job.params["keep"]) for job in teardowns] == [(first.id, True), (second.id, True)]
    conductor.destroy_sandbox()
    assert HostJob.objects.filter(kind=HostJob.Kind.TEARDOWN).order_by("-id").first().params == {"keep": False}


def test_run_without_upload_is_skipped(one_run, gameplans):
    from core.models.rounds import RoundRun

    _first, second = sorted(_two_teams(one_run, second_upload=False), key=lambda r: r.upload_id is None)
    assert second.upload_id is None
    conductor, agent, clock = make_conductor(gameplans)
    run_round(conductor, one_run["round"])
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
    run_round(conductor, one_run["round"])
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
    run_round(conductor, one_run["round"])
    run = RoundRun.objects.get(pk=one_run["run"].pk)
    assert run.worm_start_at == start + clock_delta(10)


def test_a_failed_run_stops_its_vms_and_scores_no_more(one_run, gameplans):
    """Power control fails during execute(): the attempt is thrown away, its VMs stopped and
    its window closed; what it captured never counts."""
    from core.models.rounds import HostJob, RoundRun, RunEvent
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from orchestrator.rounds import captures
    from orchestrator.rounds.power import PowerError, VMStopped

    power = fake_power()
    conductor, agent, clock = make_conductor(gameplans, power)

    def broken_stop(vm, wait=False):
        if wait:  # the gameplan's stop (the end and a failure stop without waiting)
            raise PowerError("the API refused")
        ProxmoxPower.stop(power, vm)

    power.stop = broken_stop
    run_round(conductor, one_run["round"])
    [first] = attempts(one_run["round"], one_run["team"])
    assert first.state == RoundRun.State.FAILED and "power control" in first.error
    assert first.events.get(kind=RunEvent.Kind.ERROR).data["ours"] is True  # whatever the worm did
    assert first.ends_at == first.finished_at == first.worm_start_at + clock_delta(60)
    assert set(power.api().stopped) == {"sandbox-vm0", "sandbox-vm1"}
    assert first.jobs.filter(kind=HostJob.Kind.TEARDOWN).exists()

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

    # What a guest can do to its VM is never a PowerError (the failure is ours) but VMStopped (the
    # host says who did it). Its QEMU halted (a KVM internal error) is stopped; a start ends that
    # QEMU first. A start whose VM is off again by the end of its task (a worm on its disk powering
    # off as it boots) has let it run; a start task that fails is ours.
    del power.stop  # power control as it is
    api, vm1 = power.api(), first.virtual_machines.get(vm_identifier="sandbox-vm1")
    api.state["sandbox-vm1"] = "internal-error"
    assert power.status(vm1) == "stopped"
    for action in (power.suspend, power.resume):
        with pytest.raises(VMStopped) as stopped:
            action(vm1)
        assert stopped.value.ran is False
    power.start(vm1, wait=True)
    assert api.stopped[-1] == "sandbox-vm1" and power.status(vm1) == "running"
    power.suspend(vm1)
    assert power.status(vm1) == "paused"
    power.stop(vm1, wait=True)
    api.goes_off["sandbox-vm1"] = "guest"
    with pytest.raises(VMStopped) as stopped:
        power.start(vm1, wait=True)
    assert stopped.value.ran is True and api.tasks_read[-1] == "UPID:fake:sandbox-vm1:start:"
    api.task_fails.add("sandbox-vm1")
    with pytest.raises(PowerError, match="the task failed: start failed: QEMU exited with code 1") as failed:
        power.start(vm1, wait=True)
    assert not isinstance(failed.value, VMStopped)

    # The watch, once the worm is armed. A suspend that meets the worm's poweroff (QEMU ends it as
    # it stops the VM) is not done, and the Runner's record of it is not kept. A poweroff on its way
    # as a suspend came may land until the VM is next looked at; a suspended VM found stopped after
    # that was stopped on the host, whatever the records say: the attempt fails.
    from orchestrator.rounds.conductor import AttemptBackend, AttemptFailed

    api.task_fails.clear()
    vm0 = first.virtual_machines.get(vm_identifier="sandbox-vm0")
    for vm in (vm0, vm1):
        power.start(vm, wait=True)
    backend = AttemptBackend(conductor, first)
    backend.armed = True
    api.goes_off["sandbox-vm1"] = "guest"
    backend.power("sandbox-vm1", "suspend")
    backend.event(RunEvent.Kind.VM_SUSPENDED, "sandbox-vm1", how="suspend")  # the Runner's record of it
    assert backend.expected["sandbox-vm1"] == "stopped" and not first.events.filter(kind=RunEvent.Kind.VM_SUSPENDED).exists()
    assert first.events.filter(kind=RunEvent.Kind.SKIPPED, data__message="suspend not done: the worm powered sandbox-vm1 off").exists()
    backend.power("sandbox-vm0", "suspend")
    api.state["sandbox-vm0"], api.cause["sandbox-vm0"] = "stopped", "guest"
    assert backend.status("sandbox-vm0") == "stopped"  # its poweroff was on its way
    backend.power("sandbox-vm0", "start")
    backend.power("sandbox-vm0", "suspend")
    assert backend.status("sandbox-vm0") == "paused"
    api.state["sandbox-vm0"], api.cause["sandbox-vm0"] = "stopped", "guest"
    with pytest.raises(AttemptFailed, match="sandbox-vm0 is stopped, it should be paused: powered off from inside"):
        backend.status("sandbox-vm0")
    assert backend.failed is not None


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
