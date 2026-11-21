"""Round orchestration: one run per team and round (an attempt, tried again if the
infrastructure fails), the jobs the Proxmox host executes for it, and its timeline. A round
names its gameplan (gameplans/, docs/05-live-environment.md) by file; each run pins the version
it runs by SHA-256. Every version the orchestrator has seen is recorded, its bytes included, in
gameplan_versions: immutable, and its content readable by the orchestrator and the admin only,
never the dashboard. See docs/05-live-environment.md.

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
    # the host runs only that version) and its duration per team (seconds from the worm's start).
    gameplan = models.TextField(blank=True, default="")
    gameplan_sha256 = models.TextField(blank=True, default="")
    # The recorded version of that SHA-256 (set with it when the run is pinned; a database
    # constraint keeps the two equal). Null before the pin, and for a run pinned before
    # versions were recorded whose file is gone: its version is known by hash only.
    gameplan_version = models.ForeignKey(
        "GameplanVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="runs",
        db_column="gameplan_version",
    )  # fmt: skip
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
            models.CheckConstraint(
                condition=models.Q(gameplan_version__isnull=True) | models.Q(gameplan_version=models.F("gameplan_sha256")),
                name="run_gameplan_version_is_its_sha256",
            ),
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


class GameplanVersion(models.Model):
    """One version of a gameplan file, by its SHA-256: the exact bytes the orchestrator found and
    what its author wrote for teams (Gameplan.description), recorded the first time the
    orchestrator sees it (orchestrator/rounds/gameplans.py, refresh).

    Immutable, as the database enforces (migration 0019):
      - the SHA-256 is the content's (a CHECK computes it), and `size` its length;
      - no role may UPDATE or DELETE a row, and a trigger refuses both to the owner too;
      - `first_seen_at` and `recorded_by` (the inserting account) are the database's;
      - `worms db reset` keeps the table.
    `content` is readable by the admin only: the dashboard and the orchestrator (which writes it)
    may read every other column (setup_db_permissions, postgresql_restricted_columns). Use
    `GameplanVersion.objects`, which leaves `content` out unless asked for it (`.content_of(sha256)`,
    the admin's: `worms` on the host)."""

    sha256 = models.CharField(max_length=64, primary_key=True)
    filename = models.TextField()  # the name it was first seen under
    content = models.BinaryField()
    size = models.PositiveIntegerField()
    description = models.TextField(blank=True, default="")  # "": none, or the file does not describe a sandbox
    first_seen_at = models.DateTimeField(db_default=Now())
    recorded_by = models.TextField(db_default="", editable=False)  # DEFAULT current_user (migration 0019)

    class WithoutContent(models.Manager):
        def get_queryset(self):
            return super().get_queryset().defer("content")

    objects = WithoutContent()

    class Meta:
        db_table = "gameplan_versions"
        ordering = ["first_seen_at", "sha256"]

    def __str__(self):
        return f"{self.filename}@{self.sha256[:12]}"

    @classmethod
    def content_of(cls, sha256) -> bytes | None:
        """The version's bytes (the admin only), None if it is not recorded."""
        row = cls.objects.filter(sha256=sha256).values_list("content", flat=True).first()
        return None if row is None else bytes(row)


class GameplanChange(models.Model):
    """What a gameplan file was over time, as the orchestrator's checks saw it: a row each time a
    file appears, becomes another version, or goes (version null). Append-only, as
    gameplan_versions: no role may change or remove a row, and the time and the writing account
    are the database's."""

    filename = models.TextField()
    version = models.ForeignKey(GameplanVersion, null=True, on_delete=models.PROTECT, related_name="changes",
                                db_column="sha256")  # fmt: skip
    seen_at = models.DateTimeField(db_default=Now())
    recorded_by = models.TextField(db_default="", editable=False)  # DEFAULT current_user (migration 0019)

    class Meta:
        db_table = "gameplan_changes"
        ordering = ["seen_at", "id"]
        indexes = [models.Index(fields=["filename", "seen_at"])]

    def __str__(self):
        return f"{self.filename} -> {self.version_id or 'removed'}"


class GameplanFile(models.Model):
    """A gameplan the orchestrator found, by file name: the dashboard offers the valid ones for
    a round. Written by the orchestrator from the round agent's `gameplans` job (the host's
    copy) and its own copy, whose version it records (GameplanVersion); the files stay on those
    two machines."""

    filename = models.TextField(unique=True)
    valid = models.BooleanField(default=False)  # on the host and on the orchestrator, the same file
    checked_at = models.DateTimeField(db_default=Now())
    # The orchestrator's copy as it is now. Null only before its first check after migration 0019.
    version = models.ForeignKey(GameplanVersion, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="files", db_column="sha256")  # fmt: skip

    class Meta:
        db_table = "gameplans"
        ordering = ["filename"]

    def __str__(self):
        return self.filename


def round_gameplan(round_) -> dict:
    """The gameplan version a round runs: {sha256, pinned, recorded, description}. Pinned (every
    attempt runs it) once its first attempt starts; before that, the current version of its file,
    which may still change. `recorded`: the version's bytes are in gameplan_versions (a run pinned
    before versions were recorded, whose file is gone, is known by hash only)."""
    sha = RoundRun.objects.filter(round=round_).exclude(gameplan_sha256="").values_list("gameplan_sha256", flat=True).first()
    pinned = bool(sha)
    if not pinned:
        sha = GameplanFile.objects.filter(filename=round_.gameplan).values_list("version_id", flat=True).first() if round_.gameplan else None
    version = GameplanVersion.objects.filter(sha256=sha).values("description").first() if sha else None
    return {
        "sha256": sha or "",
        "pinned": pinned,
        "recorded": version is not None,
        "description": (version or {}).get("description", ""),
    }


class GameplanSimulation(models.Model):
    """A gameplan's attempt played against a pretend clock, as the round player's document: what
    staff see on the dashboard's Simulations page (orchestrator/rounds/simulations.py). Made by the
    orchestrator on the first request for a version of the file (sha256), a duration per team (0:
    the gameplan's own) and a version of the simulation, then kept. It holds what a run's timeline
    holds (the sandbox's VMs, networks and firewall rules; power, flags, the worm, files by number,
    waits), never a path, file content, log line or problem of the gameplan."""

    filename = models.TextField()
    sha256 = models.TextField()
    duration = models.PositiveIntegerField()
    version = models.PositiveIntegerField()
    document = models.JSONField()
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "gameplan_simulations"
        constraints = [
            models.UniqueConstraint(fields=["filename", "sha256", "duration", "version"], name="unique_gameplan_simulation"),
        ]

    def __str__(self):
        return f"GameplanSimulation({self.filename}, {self.duration})"


class HostJob(models.Model):
    """A privileged operation requested by the orchestrator and executed by
    the round agent on the Proxmox host (ansible/roles/host/round_agent).

    The orchestrator inserts (kind, params); the agent claims the job, runs it
    and writes state/result. The agent validates `params` itself: rows here
    are untrusted input for it.
    """

    class Kind(models.TextChoices):
        GAMEPLANS = "gameplans"  # the host's gameplan files: name, SHA-256, valid
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
        # A round with devboxes_off: every devbox VM and gateway shut down before its first
        # attempt, started again after its last run. Not named devbox_*: the agent takes no
        # devbox_* job while the devboxes are held (round_agent/agent.py, claim).
        HOLD_DEVBOXES = "hold_devboxes"
        RELEASE_DEVBOXES = "release_devboxes"

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
