"""Turns what the sandbox APIs stored into results.

flag_capture_attempts -> flag_captures: a submitted flag counts when its hash
is a sandbox VM's released secret, the attempt's team (recorded directly by
the scoring API: `team_uuid`) is the team whose run that VM is part of, and it
was submitted while that run was live (from the worm's start to the end plus a
grace period). Each VM counts once per team. The capture is also a `captured`
event in the run's timeline (the timeline view marks the VM).

logging_attempts -> logging: attributed to the attempt's team and the round of
the run that was live for that team at the time.

Every attempt is marked processed, counted or not. Run continuously by
`manage.py process_attempts` (orchestrator).
"""

from __future__ import annotations

from datetime import timedelta

from core.models.logging import Logging, LoggingAttempt
from core.models.rounds import RoundRun, RunEvent
from core.models.scoring import FlagCapture, FlagCaptureAttempt
from core.models.team import Team
from core.models.vm import VirtualMachine
from django.db import transaction
from django.utils import timezone


def _teams_by_uuid(uuids):
    """{uuid string -> team id} for the given attempt team_uuids."""
    valid = {u for u in uuids if u}
    if not valid:
        return {}
    return {str(uuid): team_id for uuid, team_id in Team.objects.filter(uuid__in=valid).values_list("uuid", "id")}


def _live(run, ts, grace):
    if run is None or run.worm_start_at is None or run.ends_at is None:
        return False
    return run.worm_start_at <= ts <= run.ends_at + grace


def process_flags(grace_seconds=120, batch=500):
    grace = timedelta(seconds=grace_seconds)
    attempts = list(FlagCaptureAttempt.objects.filter(processed=False).order_by("id")[:batch])
    if not attempts:
        return 0
    teams = _teams_by_uuid(a.team_uuid for a in attempts)
    vms = {
        vm.ctf_flag_hash: vm
        for vm in VirtualMachine.objects.filter(
            ctf_flag_hash__in={a.vm_flag_hash for a in attempts}
        ).select_related("run")
    }
    captured = 0
    now = timezone.now()
    with transaction.atomic():
        for attempt in attempts:
            vm = vms.get(attempt.vm_flag_hash)
            team_id = teams.get(attempt.team_uuid)
            if (
                vm is not None
                and team_id is not None
                and vm.run is not None
                and vm.run.team_id == team_id
                and _live(vm.run, attempt.created_at, grace)
                and not FlagCapture.objects.filter(team_id=team_id, vm=vm).exists()
            ):
                FlagCapture.objects.create(team_id=team_id, round=vm.round, vm=vm, flag_capture_attempt=attempt)
                RunEvent.objects.create(
                    run=vm.run, ts=attempt.created_at, kind=RunEvent.Kind.CAPTURED, vm=vm,
                    data={"team": team_id, "attempt": attempt.id},
                )  # fmt: skip
                captured += 1
            attempt.processed, attempt.processed_at = True, now
            attempt.save(update_fields=["processed", "processed_at"])
    return captured


def process_logs(grace_seconds=120, batch=500):
    grace = timedelta(seconds=grace_seconds)
    attempts = list(LoggingAttempt.objects.filter(processed=False).order_by("id")[:batch])
    if not attempts:
        return 0
    teams = _teams_by_uuid(a.team_uuid for a in attempts)
    runs = {}
    for run in RoundRun.objects.filter(team_id__in=set(teams.values()), worm_start_at__isnull=False):
        runs.setdefault(run.team_id, []).append(run)
    stored = 0
    now = timezone.now()
    with transaction.atomic():
        for attempt in attempts:
            team_id = teams.get(attempt.team_uuid)
            run = next((r for r in runs.get(team_id, []) if _live(r, attempt.created_at, grace)), None)
            if run is not None:
                Logging.objects.create(team_id=team_id, round=run.round, logging_attempt=attempt)
                stored += 1
            attempt.processed, attempt.processed_at = True, now
            attempt.save(update_fields=["processed", "processed_at"])
    return stored
