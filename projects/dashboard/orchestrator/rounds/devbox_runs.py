"""Uploads made in the dev view: run the upload on VM A of the team's devbox, as the devbox is
(docs/04-dev-environment.md). Not reset first (since 2026-11-24): what earlier uploads left on
the VMs stays, and the dev score and logs go on (a capture counts once per devbox version). The
team resets its devbox by hand for fresh VMs, new tokens and a new score.

The devbox's counterpart of the conductor, and separate from it: no round, no sandbox, no
other team, and the upload never enters a round (orchestrator/rounds/planner.py).

    PENDING -> DEPLOYING -> ARMED -> RUNNING -> FINISHED
    (any of them) -> FAILED, or REPLACED by a newer upload or a manual reset

(RESETTING: a run from before 2026-11-24 whose reset was under way; it goes on to DEPLOYING.)

The dashboard only stores the upload (Upload.devbox); this picks it up. Nothing here runs a
command: it asks the round agent through host jobs, as the conductor does. A pass is
idempotent.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from core.models.devbox import Devbox, DevboxRun
from core.models.rounds import HostJob, RoundRun
from core.models.worms import Upload
from django.db import DatabaseError, transaction
from django.utils import timezone

from orchestrator.rounds.conductor import config, parse_armed_at
from orchestrator.rounds.devbox import team_slug

log = logging.getLogger("orchestrator")

S = DevboxRun.State
ACTIVE = [S.PENDING, S.RESETTING, S.DEPLOYING, S.ARMED, S.RUNNING]
ENTRY_ROLE = "a"  # where the worm starts, as the start VM of a sandbox
_WAITING: dict[int, str] = {}  # what each pending run was last said to wait for (_start)
# A round's run, from its first host job to its last.
ROUND_BUSY = [
    RoundRun.State.PREPARING, RoundRun.State.CHECKING,
    RoundRun.State.RUNNING, RoundRun.State.ENDING, RoundRun.State.TEARING_DOWN,
]  # fmt: skip


def advance(now=None) -> int:
    """One pass over every team with an upload to handle. Returns how many teams it looked at."""
    now = now or timezone.now()
    new = Upload.objects.filter(devbox=True, team__isnull=False, devbox_run__isnull=True).order_by("id")
    for upload in new:
        DevboxRun.objects.get_or_create(upload=upload, defaults={"team_id": upload.team_id})
    team_ids = set(DevboxRun.objects.filter(state__in=ACTIVE).values_list("team_id", flat=True))
    for team_id in team_ids:
        try:
            _advance_team(team_id, now)
        except Exception:  # one team's problem must not stop the others
            log.exception("devbox run of team %s failed to advance", team_id)
    return len(team_ids)


def _advance_team(team_id, now):
    runs = list(
        DevboxRun.objects.filter(team_id=team_id, state__in=ACTIVE)
        .select_related("team", "upload__artifact", "rebuild_job", "deploy_job")
        .order_by("-id")
    )
    for older in runs[1:]:  # only the team's latest upload runs
        _set(older, S.REPLACED)
    _step(runs[0], now, config())


def _step(run, now, cfg):
    if run.state == S.PENDING:
        _start(run, cfg)
    elif run.state == S.RESETTING:  # a run from before 2026-11-24, its reset under way
        if _finished(run, run.rebuild_job, now, cfg):
            _start_deploy(run, cfg)
    elif run.devbox_version != run.team.devbox_version:
        # Someone reset the devbox by hand: the VMs this ran on are gone.
        _set(run, S.REPLACED)
    elif run.state == S.DEPLOYING:
        if _finished(run, run.deploy_job, now, cfg):
            _armed(run, now, cfg)
    elif run.state == S.ARMED:
        if now >= run.worm_start_at:
            _set(run, S.RUNNING)
    elif run.state == S.RUNNING:
        if now >= run.ends_at:
            _set(run, S.FINISHED)


def deploy_waits_for(team=None) -> str:
    """Why a team's upload cannot go onto its devbox now ("" if it can): what it waits for, in
    words, for the processor's log, `worms trace` and the dashboard. A round's attempt under way
    (its host steps come first; during a session the agent is stopped anyway), or a reset of the
    team's devbox that is not done yet (the team's own: the upload goes onto the VMs it leaves)."""
    busy = RoundRun.objects.filter(state__in=ROUND_BUSY).select_related("round", "team").first()
    if busy is not None:
        return f"a round's attempt is under way (round {busy.round.name!r}, team {busy.team.name}: {busy.state})"
    if team is not None and HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_REBUILD, params__team_uuid=str(team.uuid),
                                                   state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING]).exists():  # fmt: skip
        return "the reset of its devbox that is under way"
    return ""


def _start(run, cfg):
    """A new upload onto the devbox as it is: no reset first (since 2026-11-24)."""
    waits = deploy_waits_for(run.team)
    if waits:
        # Said once per reason, not every pass: the processor's log tells what an upload waits for.
        if _WAITING.get(run.pk) != waits:
            _WAITING[run.pk] = waits
            log.info("devbox run of upload %s waits: %s", run.upload_id, waits)
        return
    _WAITING.pop(run.pk, None)
    if not Devbox.objects.filter(team_id=run.team_id).exists():
        _fail(run, "the team has no devbox")
        return
    run.team.refresh_from_db(fields=["devbox_version"])
    # The devbox it runs on: a reset after this (a new version) replaces it (_step).
    run.devbox_version = run.team.devbox_version
    run.save(update_fields=["devbox_version"])
    _start_deploy(run, cfg)


def _start_deploy(run, cfg):
    artifact = run.upload.artifact
    slug = team_slug(run.team)
    with transaction.atomic():
        run.deploy_job = HostJob.objects.create(
            kind=HostJob.Kind.DEVBOX_DEPLOY,
            params={
                "team_uuid": str(run.team.uuid),
                "team_slug": slug,
                "start_vm": f"devbox-{slug}-{ENTRY_ROLE}",
                "artifact": {"storage_key": artifact.storage_key, "content_hash": artifact.content_hash},
                "worm_delay": cfg["devbox_worm_start_delay"],
                "run_duration": cfg["devbox_run_duration"],
            },
        )
        _set(run, S.DEPLOYING, "deploy_job")


def _armed(run, now, cfg):
    # The fuse was lit when the agent armed it (its clock), not when this pass noticed.
    armed = parse_armed_at(run.deploy_job.result or {}, now, cfg["job_timeout"]) or now
    run.armed_at = armed
    run.worm_start_at = armed + timedelta(seconds=cfg["devbox_worm_start_delay"])
    run.ends_at = run.worm_start_at + timedelta(seconds=cfg["devbox_run_duration"])
    _set(run, S.ARMED, "armed_at", "worm_start_at", "ends_at")


def _finished(run, job, now, cfg) -> bool:
    """True once the job is done; fails the run if it failed or stopped reporting. A job still
    queued waits its turn (the agent runs one job at a time) for up to job_timeout; then nothing
    takes it (the agent stopped, paused, or held up by a job that does not end): it is taken off
    the queue, and the run fails with why. Until 2026-11-26 it waited for ever, the team told
    "queued behind other work"."""
    if job is None:
        _fail(run, "its host job is gone")
        return False
    if job.state == HostJob.State.DONE:
        return True
    if job.state == HostJob.State.FAILED:
        _fail(run, f"{job.kind} job failed: {job.error}")
        return False
    if job.state == HostJob.State.QUEUED:
        if now - job.requested_at > timedelta(seconds=cfg["job_timeout"]):
            why = (f"{job.kind} job not taken by the round agent within {cfg['job_timeout'] // 60} min: it is stopped, "
                   "paused (`worms jobs resume`) or held up by another job (`worms status`)")  # fmt: skip
            # Off the queue first, unless the agent took it meanwhile: run later, it would put an
            # upload the team was told had failed onto its VM. The orchestrator's grant for it
            # (UPDATE on host_jobs' state) comes with `worms apply database`; until then it waits.
            try:
                with transaction.atomic():  # a refused UPDATE leaves an enclosing transaction usable
                    taken_off = HostJob.objects.filter(pk=job.pk, state=HostJob.State.QUEUED).update(
                        state=HostJob.State.FAILED, finished_at=now, error=f"cancelled: {why}"
                    )
            except DatabaseError as error:
                log.warning("devbox run of upload %s: its %s job could not be taken off the queue (%s): `worms apply "
                            "database`", run.upload_id, job.kind, str(error).strip()[:200])  # fmt: skip
                return False
            if taken_off:
                _fail(run, why)
        return False
    reference = job.heartbeat_at or job.started_at
    if reference and now - reference > timedelta(seconds=cfg["job_timeout"]):
        _fail(run, f"{job.kind} job timed out")
    return False


def _fail(run, error):
    run.error = error[:1000]
    log.warning("devbox run of upload %s failed: %s", run.upload_id, run.error)
    _set(run, S.FAILED, "error")


def _set(run, state, *fields):
    log.info("devbox run of upload %s: %s -> %s", run.upload_id, run.state, state)
    run.state = state
    run.updated_at = timezone.now()
    run.save(update_fields=["state", "updated_at", *fields])
