"""Uploads made in the dev view: reset the team's devbox, then run the upload on VM A
(docs/devbox.md).

The devbox's counterpart of the conductor, and separate from it: no round, no sandbox, no
other team, and the upload never enters a round (orchestrator/rounds/planner.py).

    PENDING -> RESETTING -> DEPLOYING -> ARMED -> RUNNING -> FINISHED
    (any of them) -> FAILED, or REPLACED by a newer upload or a manual reset

The dashboard only stores the upload (Upload.devbox); this picks it up. Nothing here runs a
command: it asks the round agent through host jobs, as the conductor does. A pass is
idempotent, and the orchestrator's attempt loop runs one after applying finished devbox
jobs, so a rebuilt VM's token is known before the worm can submit it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from core.models.devbox import DevboxRun
from core.models.rounds import HostJob, RoundRun
from core.models.worms import Upload
from django.db import transaction
from django.utils import timezone

from orchestrator.rounds.conductor import config, parse_armed_at
from orchestrator.rounds.devbox import request_rebuild, team_slug

log = logging.getLogger("orchestrator")

S = DevboxRun.State
ACTIVE = [S.PENDING, S.RESETTING, S.DEPLOYING, S.ARMED, S.RUNNING]
ENTRY_ROLE = "a"  # where the worm starts, as the start VM of a sandbox
# A round's run, from its first host job to its last.
ROUND_BUSY = [
    RoundRun.State.PREPARING, RoundRun.State.CHECKING, RoundRun.State.DEPLOYING, RoundRun.State.ARMED,
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
        _start_reset(run)
    elif run.state == S.RESETTING:
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


def _start_reset(run):
    # The agent runs one job at a time and a rebuild takes many minutes. Started now, it would
    # hold up a running round's timed jobs, so a round's team would pay for another's test. The
    # agent also takes a round's jobs first (round_agent/agent.py, claim), but it cannot stop a
    # rebuild it has begun.
    if RoundRun.objects.filter(state__in=ROUND_BUSY).exists():
        return
    with transaction.atomic():
        job, enqueued = request_rebuild(run.team)
        if job is None:
            _fail(run, "the team has no devbox")
        elif enqueued:
            run.rebuild_job = job
            run.devbox_version = job.params["devbox_version"]
            _set(run, S.RESETTING, "rebuild_job", "devbox_version")
        # Otherwise a rebuild is in progress (a replaced upload's, or a manual reset): wait for
        # it. This one needs fresh VMs of its own, because that rebuild may have been followed
        # by anything.


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
    queued has not timed out: the agent runs one job at a time, so it may simply be waiting."""
    if job is None:
        _fail(run, "its host job is gone")
        return False
    if job.state == HostJob.State.DONE:
        return True
    if job.state == HostJob.State.FAILED:
        _fail(run, f"{job.kind} job failed: {job.error}")
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
