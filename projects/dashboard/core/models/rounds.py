"""Round orchestration: one run per team and round, its game plan, the jobs
the Proxmox host executes for it, and its timeline. See docs/rounds.md.

Written by the orchestrator (orchestrator/rounds/conductor.py) and, for
`host_jobs` only, by the round agent on the Proxmox host (state and results).
Read by the dashboard (timeline, topology).

Times here are real wall-clock times (django.utils.timezone), not core.clock:
they describe what happened in the sandbox.
"""

import uuid

from core.models.competition import Round
from core.models.team import Team
from core.models.worms import Upload
from django.db import models
from django.db.models.functions import Now


class RoundRun(models.Model):
    """The evaluation of one team's worm in one round, in its own sandbox."""

    class State(models.TextChoices):
        PENDING = "pending"  # waiting for its turn
        PREPARING = "preparing"  # host: reset environment, build sandbox
        CHECKING = "checking"  # host: sanity checks
        DEPLOYING = "deploying"  # host: secrets, worm, start timer
        ARMED = "armed"  # worm starts at worm_start_at
        RUNNING = "running"  # game plan executes
        ENDING = "ending"  # VMs stopped, monitor drains
        TEARING_DOWN = "tearing_down"  # host: destroy the sandbox
        FINISHED = "finished"
        FAILED = "failed"  # see `error`; the sandbox is torn down
        SKIPPED = "skipped"  # no upload before the deadline

    uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="runs")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="runs")
    # The team's latest upload before the round's deadline (round_start).
    upload = models.ForeignKey(Upload, on_delete=models.SET_NULL, null=True, blank=True, related_name="runs")
    position = models.PositiveIntegerField()  # order within the round
    seed = models.BigIntegerField()

    state = models.TextField(choices=State.choices, default=State.PENDING)
    error = models.TextField(blank=True, default="")

    # Generator parameters of the sandbox (scripts/generate_sandbox.py).
    sandbox_params = models.JSONField(default=dict)
    # The sandbox as built (from the host's prepare job), saved for the
    # round's visualisation: `topology` is the full generator graph (every
    # layer -- internet, edge/core/distribution/access switches, networks and
    # VMs, with all edges), `sandbox_definition` is the generated OpenTofu
    # configuration (the whole sandbox stack definition for this run).
    topology = models.JSONField(default=dict)
    sandbox_definition = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(db_default=Now())
    state_changed_at = models.DateTimeField(null=True, blank=True)
    worm_start_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "round_runs"
        constraints = [
            models.UniqueConstraint(fields=["round", "team"], name="unique_run_per_round_team"),
            models.UniqueConstraint(fields=["round", "position"], name="unique_run_position"),
        ]
        indexes = [models.Index(fields=["state"])]

    def __str__(self):
        return f"RoundRun({self.round.name}, {self.team.name}, {self.state})"


class GamePlanStep(models.Model):
    """One action of a run's game plan.

    A step is due when `at_seconds` (after the worm's start; null = before
    the start, during deployment) has passed and every step listed in `after`
    (positions) is done. Example: "release VM B's secret after VM A has been
    stopped" is a release_secret step for B with after = [the stop step of A].
    """

    class Action(models.TextChoices):
        RELEASE_SECRET = "release_secret"  # host writes a new secret into the VM
        STOP_VM = "stop_vm"  # power off (hard), via the Proxmox API
        START_VM = "start_vm"

    class State(models.TextChoices):
        PENDING = "pending"
        REQUESTED = "requested"  # waiting for a host job
        DONE = "done"
        FAILED = "failed"

    run = models.ForeignKey(RoundRun, on_delete=models.CASCADE, related_name="steps")
    position = models.PositiveIntegerField()
    action = models.TextField(choices=Action.choices)
    vm = models.ForeignKey("core.VirtualMachine", on_delete=models.CASCADE, related_name="steps")
    at_seconds = models.IntegerField(null=True, blank=True)
    after = models.JSONField(default=list, blank=True)

    state = models.TextField(choices=State.choices, default=State.PENDING)
    host_job = models.ForeignKey("core.HostJob", on_delete=models.SET_NULL, null=True, blank=True, related_name="steps")
    executed_at = models.DateTimeField(null=True, blank=True)
    detail = models.TextField(blank=True, default="")

    class Meta:
        db_table = "game_plan_steps"
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["run", "position"], name="unique_step_position"),
        ]


class HostJob(models.Model):
    """A privileged operation requested by the orchestrator and executed by
    the round agent on the Proxmox host (ansible/roles/host/round_agent).

    The orchestrator inserts (kind, params); the agent claims the job, runs it
    and writes state/result. The agent validates `params` itself: rows here
    are untrusted input for it.
    """

    class Kind(models.TextChoices):
        PREPARE = "prepare"  # reset ephemeral + private internet, build the sandbox
        CHECK = "check"  # sanity checks (firewalls, VMs, services)
        DEPLOY = "deploy"  # worm artifact to the start VM + start timer
        RELEASE_SECRETS = "release_secrets"  # new secrets into VMs, hashes back
        TEARDOWN = "teardown"  # destroy the sandbox
        # Devbox (docs/devbox.md): provision/rebuild a team's devbox VMs and
        # (re)write their tokens; run is null (a devbox job is team-scoped,
        # not round-scoped). The team uuid and version are in params.
        DEVBOX_PROVISION = "devbox_provision"
        DEVBOX_REBUILD = "devbox_rebuild"
        # Re-push every team's SSH keys to the devbox VMs and the gateway (and
        # the source->team map) without rebuilding any VM: run when a team adds
        # or removes a key, or leaves. Cheap and idempotent.
        DEVBOX_RECONFIGURE = "devbox_reconfigure"

    class State(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        DONE = "done"
        FAILED = "failed"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    run = models.ForeignKey(RoundRun, on_delete=models.CASCADE, null=True, blank=True, related_name="jobs")
    kind = models.TextField(choices=Kind.choices)
    params = models.JSONField(default=dict)

    state = models.TextField(choices=State.choices, db_default=State.QUEUED, default=State.QUEUED)
    requested_at = models.DateTimeField(db_default=Now())
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        db_table = "host_jobs"
        indexes = [models.Index(fields=["state", "id"], name="host_jobs_queue")]

    def __str__(self):
        return f"HostJob({self.kind}, {self.state})"


class RunEvent(models.Model):
    """The timeline of a run: state changes, VMs stopped/started, secrets
    released, the worm deployed/started, VMs captured. Together with
    network_flows (by time) this is what the timeline view replays."""

    class Kind(models.TextChoices):
        STATE = "state"
        VM_STOPPED = "vm_stopped"
        VM_STARTED = "vm_started"
        SECRET_RELEASED = "secret_released"
        WORM_DEPLOYED = "worm_deployed"
        WORM_STARTED = "worm_started"
        CAPTURED = "captured"
        ERROR = "error"

    run = models.ForeignKey(RoundRun, on_delete=models.CASCADE, related_name="events")
    ts = models.DateTimeField(db_default=Now())
    kind = models.TextField(choices=Kind.choices)
    vm = models.ForeignKey("core.VirtualMachine", on_delete=models.SET_NULL, null=True, blank=True, related_name="events")
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "run_events"
        indexes = [models.Index(fields=["run", "ts"], name="run_events_timeline")]
