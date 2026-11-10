"""Round orchestration: one run per team and round (an attempt, tried again if the
infrastructure fails), the jobs the Proxmox host executes for it, and its timeline. The
round's gameplan (gameplans/, docs/05-live-environment.md) is named by its file only: its content is
never in the database. See docs/05-live-environment.md.

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
    """One attempt at evaluating a team's worm in a round, in the round's sandbox. An attempt
    the infrastructure failed is FAILED and a new one (attempt + 1) takes its place, up to
    the conductor's max_retries (orchestrator/rounds/conductor.py). Only a FINISHED attempt's
    captures and logs count, and only then are they shown to the team."""

    class State(models.TextChoices):
        PENDING = "pending"  # waiting for its turn
        PREPARING = "preparing"  # host: the sandbox built or rolled back, the run's secrets and flags
        CHECKING = "checking"  # host: sanity checks
        RUNNING = "running"  # the gameplan's execute(): the worm is deployed in it
        ENDING = "ending"  # VMs stopped, monitor drains
        TEARING_DOWN = "tearing_down"  # host: destroy the sandbox, or keep it for the round's next run
        FINISHED = "finished"
        FAILED = "failed"  # see `error`; thrown away, and tried again while attempts are left
        SKIPPED = "skipped"  # no upload before the deadline

    uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="runs")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="runs")
    # The team's latest upload before the round's deadline (round_start); every attempt runs it.
    upload = models.ForeignKey(Upload, on_delete=models.SET_NULL, null=True, blank=True, related_name="runs")
    position = models.PositiveIntegerField()  # order within the round
    attempt = models.PositiveIntegerField(default=0)  # 0 = the first; a retry is the next number
    seed = models.BigIntegerField()

    state = models.TextField(choices=State.choices, default=State.PENDING)
    error = models.TextField(blank=True, default="")

    # The gameplan this attempt runs (its file name and the SHA-256 the orchestrator validated:
    # the node runs only that version) and its duration per team (seconds from the worm's start).
    gameplan = models.TextField(blank=True, default="")
    gameplan_sha256 = models.TextField(blank=True, default="")
    duration = models.PositiveIntegerField(default=3600)
    # The sandbox as built (from the host's prepare job), for the replay: VMs, networks and
    # the gateway (src/gameplan/build.py, topology), and the VM the worm started on.
    topology = models.JSONField(default=dict)
    entrypoint = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(db_default=Now())
    state_changed_at = models.DateTimeField(null=True, blank=True)
    worm_start_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "round_runs"
        constraints = [
            models.UniqueConstraint(fields=["round", "team", "attempt"], name="unique_run_attempt_per_round_team"),
            models.UniqueConstraint(fields=["round", "position", "attempt"], name="unique_run_position_attempt"),
        ]
        indexes = [models.Index(fields=["state"])]

    def __str__(self):
        return f"RoundRun({self.round.name}, {self.team.name}, attempt {self.attempt}, {self.state})"

    # Not over yet: the round's results wait for these.
    OPEN_STATES = (State.PENDING, State.PREPARING, State.CHECKING, State.RUNNING, State.ENDING, State.TEARING_DOWN)

    @classmethod
    def for_team(cls, round_, team):
        """The attempt that stands for a team in a round: its finished one, else its latest."""
        runs = cls.objects.filter(round=round_, team=team)
        return runs.filter(state=cls.State.FINISHED).order_by("-attempt").first() or runs.order_by("-attempt").first()


class GameplanFile(models.Model):
    """A gameplan the orchestrator found, by file name only: the dashboard offers the valid
    ones for a round. Written by the orchestrator from the round agent's `gameplans` job (the
    node's copy) and its own copy; their content stays on those two machines."""

    filename = models.TextField(unique=True)
    valid = models.BooleanField(default=False)  # on the node and on the orchestrator, the same file
    checked_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "gameplans"
        ordering = ["filename"]

    def __str__(self):
        return self.filename


class HostJob(models.Model):
    """A privileged operation requested by the orchestrator and executed by
    the round agent on the Proxmox host (ansible/roles/host/round_agent).

    The orchestrator inserts (kind, params); the agent claims the job, runs it
    and writes state/result. The agent validates `params` itself: rows here
    are untrusted input for it.
    """

    class Kind(models.TextChoices):
        GAMEPLANS = "gameplans"  # the node's gameplan files: name, SHA-256, valid
        PREPARE = "prepare"  # the round's sandbox (built or rolled back), the run's secrets, files, flags
        CHECK = "check"  # sanity checks (firewalls, VMs, services)
        DEPLOY = "deploy"  # worm artifact to the start VM + start timer
        REMOVE_FLAGS = "remove_flags"  # the gameplan's execute(): flags out, checked
        PLACE_FLAGS = "place_flags"  # new flags in, hashes back
        WRITE_FILE = "write_file"  # a file the gameplan declared, by its number
        READ_FILE = "read_file"  # whether a file on a VM changed: its size and SHA-256, never its content
        TEARDOWN = "teardown"  # destroy the sandbox, or keep it powered off for the round's next run
        # Devbox (docs/04-dev-environment.md): provision/rebuild a team's devbox VMs and
        # (re)write their tokens; run is null (a devbox job is team-scoped,
        # not round-scoped). The team uuid and version are in params.
        DEVBOX_PROVISION = "devbox_provision"
        DEVBOX_REBUILD = "devbox_rebuild"
        # Re-push every team's SSH keys to the devbox VMs and the gateway (and
        # the source->team map) without rebuilding any VM: run when a team adds
        # or removes a key, or leaves. Cheap and idempotent.
        DEVBOX_RECONFIGURE = "devbox_reconfigure"
        # Put a team's dev upload on its devbox VM A and arm the fuse: `deploy` for
        # the devbox (orchestrator/rounds/devbox_runs.py).
        DEVBOX_DEPLOY = "devbox_deploy"

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
    """The timeline of a run: state changes, VMs stopped/started/suspended/resumed, flags
    placed (secret_released) and removed, the worm deployed/started, VMs captured. Together
    with network_flows (by time) this is what the timeline view replays. `skipped` (a step the
    worm made impossible) and `error` are for staff only."""

    class Kind(models.TextChoices):
        STATE = "state"
        VM_STOPPED = "vm_stopped"
        VM_STARTED = "vm_started"
        VM_SUSPENDED = "vm_suspended"
        VM_RESUMED = "vm_resumed"
        SECRET_RELEASED = "secret_released"
        FLAG_REMOVED = "flag_removed"
        WORM_DEPLOYED = "worm_deployed"
        WORM_STARTED = "worm_started"
        CAPTURED = "captured"
        SKIPPED = "skipped"
        ERROR = "error"

    run = models.ForeignKey(RoundRun, on_delete=models.CASCADE, related_name="events")
    ts = models.DateTimeField(db_default=Now())
    kind = models.TextField(choices=Kind.choices)
    vm = models.ForeignKey("core.VirtualMachine", on_delete=models.SET_NULL, null=True, blank=True, related_name="events")
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "run_events"
        indexes = [models.Index(fields=["run", "ts"], name="run_events_timeline")]
