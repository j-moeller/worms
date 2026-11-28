"""Staff: the rounds of the competition -- defining them, and following them.

A round is a database record: its window, its seed (the order the teams run in),
its gameplan and its duration per team. The gameplan is picked by file name from
the list the orchestrator keeps; the dashboard never reads a gameplan
(docs/05-live-environment.md). Creating a round builds nothing. At the round's start the
conductor on the orchestrator plans one run per team; the round agent on the
Proxmox host builds the gameplan's sandbox for the first, with OpenTofu (root on
the host), and snapshots it; every run starts from that snapshot. An attempt the
infrastructure failed is run again (docs/05-live-environment.md). This page shows how far
that got: the attempts, their host jobs and errors.

A round can change whenever no `worms round start` runs it (the session's mark,
Round.session_running): fully before its runs are planned, then all but its seed (the
teams' order). A change after teams ran is the next session's to carry out: it runs again,
asked, every team whose attempt no longer matches the round, and keeps the earlier attempts
as superseded (orchestrator/rounds/conductor.py, stale and replan). It is deleted, with its
attempts and their records, whenever no session runs it (round_delete). Where a round is, its annotated timeline, is read
from its runs (round_progress.py); every staff page warns of a round whose gameplan is
missing or not valid (round_progress.warnings, through render_with_base_info).
"""

from __future__ import annotations

from collections import Counter

from core import clock, events, schedule
from core.locking import serialized_resource
from core.models.competition import Competition, Round
from core.models.rounds import GameplanFile, HostJob, RoundRun, round_gameplan
from core.models.scoring import FlagCapture
from core.models.team import Team
from dashboard.forms import RoundForm, overlapping_round
from dashboard.views import round_progress
from dashboard.views.decorators import refuse, staff_required
from dashboard.views.util import render_with_base_info
from django.contrib import messages
from django.db import transaction
from django.core.files.storage import default_storage
from django.db.models import Q
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import get_object_or_404, redirect
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST

RUNNING = "`worms round start` runs this round now: it can change once that session has ended."
CHANGED = (" Teams have run: the next `worms round start` runs again, asked, every team whose attempt no longer "
           "matches it (their earlier attempts kept, superseded, no longer counted).")  # fmt: skip
NOTE_LIMIT = 500



def _status(round_obj, now) -> str:
    if round_obj.round_start is None or round_obj.round_end is None:
        return "undated"
    if now < round_obj.round_start:
        return "scheduled"
    if now < round_obj.round_end:
        return "active"
    return "finished"


def _estimate(round_obj, teams):
    return schedule.estimate(round_obj.round_start, round_obj.round_end, round_obj.team_duration, teams)


def _check_window(form, competition, start, end, exclude=None):
    """Rounds of one competition do not overlap (Live runs are sequential)."""
    if start is None or end is None:
        return
    clash = overlapping_round(competition, start, end, exclude=exclude)
    if clash is not None:
        form.add_error("round_start", f"Overlaps {clash.name} ({clash.round_start:%Y-%m-%d %H:%M} – {clash.round_end:%Y-%m-%d %H:%M} UTC).")


def _check_fit(form, start, end, team_duration, teams):
    estimate = schedule.estimate(start, end, team_duration, teams)
    if not estimate.fits:
        form.add_error(
            "round_end",
            f"{teams} teams need {schedule.format_duration(estimate.needed)} "
            f"({schedule.format_duration(estimate.slot)} each, one after another); "
            f"this round is {schedule.format_duration(estimate.available)} "
            f"(room for {estimate.max_teams}). Make it longer or the duration per team shorter.",
        )


@staff_required
def rounds(request: HttpRequest):
    comp = request.competition
    if comp is None:
        return refuse(request, "No current competition.", 500)
    now = clock.now()
    teams = Team.objects.filter(competition=comp).count()
    form = RoundForm(competition=comp)
    if request.method == "POST":
        form = RoundForm(request.POST, competition=comp)
        if form.is_valid():
            # Read before checking: an error on a field removes it from cleaned_data.
            data = dict(form.cleaned_data)
            start, end, duration = data["round_start"], data["round_end"], form.team_duration_seconds()
            with transaction.atomic():
                # One round at a time per competition: two staff saving at once cannot
                # both pass the overlap check.
                Competition.objects.select_for_update().get(pk=comp.pk)
                teams = Team.objects.filter(competition=comp).count()
                _check_window(form, comp, start, end)
                _check_fit(form, start, end, duration, teams)
                if not form.errors:
                    created = Round.objects.create(
                        competition=comp, name=data["name"], seed=form.chosen_seed(),
                        round_start=start, round_end=end, gameplan=data["gameplan"], team_duration=duration,
                        note=data.get("note", ""),
                    )  # fmt: skip
                    events.competition("round_created", request=request, round_id=created.pk, detail={"name": created.name})
                    note = "" if created.gameplan else " It has no gameplan yet: choose one on its page. Without one at its start it waits, alone."
                    messages.success(request, f"Round {created.name} created. Its runs are planned when it starts.{note}")
                    return redirect("staff_round", round_uuid=created.uuid)

    current = round_progress.under_way()

    def listing(competition):
        rows = []
        current_versions = dict(GameplanFile.objects.values_list("filename", "version_id"))
        for r in Round.objects.filter(competition=competition).order_by("round_start"):
            runs = list(RoundRun.objects.filter(round=r).only("state", "gameplan_sha256", "upload_id"))
            # As round_gameplan: the version pinned once an attempt started, else the file's now.
            pinned = next((run.gameplan_sha256 for run in runs if run.gameplan_sha256 and run.state != RoundRun.State.SUPERSEDED), "")
            rows.append({
                "round": r,
                "version": pinned or current_versions.get(r.gameplan) or "",
                "status": _status(r, now),
                "phase": round_progress.phase(r, runs, now, current),
                "states": sorted(Counter(run.state for run in runs).items()),
                "captures": FlagCapture.objects.filter(round=r, vm__run__state=RoundRun.State.FINISHED).count(),
                "estimate": _estimate(r, teams),
            })  # fmt: skip
        return rows

    rows = listing(comp)
    # The timeline only of a round being run right now (a session's, or an attempt under way).
    running = round_progress.active()
    return render_with_base_info(request, "staff/rounds.html", {
        "form": form,
        "live_rounds": rows,
        "progress": round_progress.progress(running, current=current) if running is not None else None,
        "teams": teams,
        "timing": schedule.timing_defaults(),
    })  # fmt: skip


def _ran(runs) -> bool:
    """Whether a team's attempt has run (or was skipped): a change of the round then reaches it
    only through the next session's question (CHANGED)."""
    return any(run.state != RoundRun.State.PENDING for run in runs)


@staff_required
def round_detail(request: HttpRequest, round_uuid):
    comp = request.competition
    round_obj = get_object_or_404(Round, uuid=round_uuid, competition=comp)
    now = clock.now()
    runs = list(RoundRun.objects.filter(round=round_obj).select_related("team").order_by("position", "attempt"))
    # Changed whenever no `worms round start` runs it; once its runs are planned, all but its seed.
    running = round_obj.session_running(now)
    planned = bool(runs)
    form = None
    if not running:
        initial = RoundForm.initial_from(round_obj)
        form = RoundForm(initial=initial, competition=comp, instance=round_obj, planned=planned)
        if request.method == "POST":
            form = RoundForm(request.POST, competition=comp, instance=round_obj, planned=planned)
            if form.is_valid():
                data = dict(form.cleaned_data)
                start, end = data["round_start"], data["round_end"]
                duration = form.team_duration_seconds()
                with transaction.atomic():
                    Competition.objects.select_for_update().get(pk=round_obj.competition_id)
                    with serialized_resource("round-plan", round_obj.pk):
                        # Against `worms round start` beginning meanwhile (it plans the runs under this lock).
                        round_obj = get_object_or_404(Round, pk=round_obj.pk, competition=comp)
                        if round_obj.session_running(clock.now()):
                            messages.error(request, RUNNING)
                            return redirect("staff_round", round_uuid=round_obj.uuid)
                        if not planned and RoundRun.objects.filter(round=round_obj).exists():
                            messages.error(request, "Its runs were planned meanwhile; reload before editing it.")
                            return redirect("staff_round", round_uuid=round_obj.uuid)
                        _check_window(form, round_obj.competition, start, end, exclude=round_obj)
                        _check_fit(form, start, end, duration, Team.objects.filter(competition=round_obj.competition).count())
                        if not form.errors:
                            round_obj.name, round_obj.round_start, round_obj.round_end = data["name"], start, end
                            round_obj.gameplan, round_obj.team_duration = data["gameplan"], duration
                            round_obj.note = data.get("note", "")
                            # The orchestrator checks it again against the new values.
                            round_obj.gameplan_status = ""
                            fields = ["name", "round_start", "round_end", "gameplan", "team_duration", "note", "gameplan_status"]
                            if not planned:
                                round_obj.seed = form.chosen_seed()
                                fields.append("seed")
                            round_obj.save(update_fields=fields)
                            events.competition("round_updated", request=request, round_id=round_obj.pk, detail={"name": round_obj.name})
                            messages.success(request, f"Saved {round_obj.name}." + (CHANGED if _ran(runs) else ""))
                            return redirect("staff_round", round_uuid=round_obj.uuid)
    elif request.method == "POST":
        messages.error(request, RUNNING)
        return redirect("staff_round", round_uuid=round_obj.uuid)

    teams = Team.objects.filter(competition=round_obj.competition).count()
    # Why each attempt failed (RoundRun.failure, `worms round start` writes it), the newest first:
    # the step, its phase, the cause, what of its output says it, and its logs on the host.
    failures = sorted((run for run in runs if run.failure), key=lambda run: run.finished_at or run.created_at, reverse=True)
    # The round's own steps, tied to no attempt (its sandbox's build and snapshot, its end): their
    # params name the round (orchestrator/rounds/conductor.py build_sandbox, destroy_sandbox).
    own = Q(run__isnull=True, params__round=round_obj.pk)
    step_failures = list(HostJob.objects.filter(own, state=HostJob.State.FAILED).order_by("-id")[:10])
    # The rounds before and after it, in the order of their starts (the rounds page's).
    order = list(Round.objects.filter(competition=round_obj.competition).order_by("round_start", "id").values("pk", "uuid", "name"))
    at = next(i for i, r in enumerate(order) if r["pk"] == round_obj.pk)
    return render_with_base_info(request, "staff/round.html", {
        "round": round_obj,
        "status": _status(round_obj, now),
        "runs": runs,
        "failures": failures,
        "step_failures": step_failures,
        "form": form,
        "running": running,
        "attempts": len(runs),
        "planned": planned,
        "ran": _ran(runs),
        "timing": schedule.timing_defaults(),
        "estimate": _estimate(round_obj, teams),
        "version": round_gameplan(round_obj),
        "progress": round_progress.progress(round_obj),
        "previous": order[at - 1] if at > 0 else None,
        "following": order[at + 1] if at + 1 < len(order) else None,
    })  # fmt: skip


@staff_required
@require_GET
def run_upload(request: HttpRequest, run_uuid):
    """The zip a team's attempt ran (or is to run), byte for byte, for staff: the round page's
    Upload column. By the attempt, so it is the upload the round took for that team (the latest
    before its start), not merely the team's newest."""
    run = get_object_or_404(RoundRun.objects.select_related("upload__artifact", "team"), uuid=run_uuid,
                            round__competition=request.competition)  # fmt: skip
    if run.upload is None:
        raise Http404("This attempt has no upload.")
    try:
        stream = default_storage.open(run.upload.artifact.storage_key, "rb")
    except OSError:
        raise Http404("That upload is no longer stored.") from None
    name = f"worm-{slugify(run.team.name) or f'team-{run.team_id}'}-{run.upload.artifact.content_hash[:12]}.zip"
    response = FileResponse(stream, as_attachment=True, filename=name, content_type="application/zip")
    response["Cache-Control"] = "private, no-store"
    return response


@staff_required
@require_POST
def round_note(request: HttpRequest, round_uuid):
    """Staff's note on a round while a session runs it (the round's form, which has it too, waits
    then): only the note is written (nothing else of the round, which may be changing meanwhile);
    empty removes it. Shown on the staff pages only, never to a team."""
    round_obj = get_object_or_404(Round, uuid=round_uuid, competition=request.competition)
    note = request.POST.get("note", "").strip()[:NOTE_LIMIT]
    if note != round_obj.note:
        Round.objects.filter(pk=round_obj.pk).update(note=note)
        events.competition("round_note", request=request, round_id=round_obj.pk, detail={"name": round_obj.name})
    messages.success(request, f"Note on {round_obj.name} saved." if note else f"Note on {round_obj.name} removed.")
    return redirect("staff_round", round_uuid=round_obj.uuid)


@staff_required
@require_POST
def round_delete(request: HttpRequest, round_uuid):
    """A round deleted whenever no `worms round start` runs it: with its attempts and everything
    under them (VMs, events, captures, logs, host jobs) once staff confirmed that too. One
    transaction: the round's row locked (a session writes its mark there), the mark checked
    again under the lock, then Round.delete_with_records."""
    comp = request.competition
    round_obj = get_object_or_404(Round, uuid=round_uuid, competition=comp)
    if round_obj.session_running(clock.now()):
        messages.error(request, RUNNING)
        return redirect("staff_round", round_uuid=round_obj.uuid)
    with serialized_resource("round-plan", round_obj.pk):  # one transaction, against a session planning it
        locked = Round.objects.select_for_update().filter(pk=round_obj.pk).first()
        if locked is None:
            messages.error(request, f"{round_obj.name} is gone already.")
            return redirect("staff_rounds")
        if locked.session_running(clock.now()):
            messages.error(request, RUNNING)
            return redirect("staff_round", round_uuid=round_obj.uuid)
        attempts = RoundRun.objects.filter(round=locked).count()
        if attempts and request.POST.get("with_attempts") != "yes":
            messages.error(request, f"{round_obj.name} has {attempts} attempt(s): confirm that they go with it.")
            return redirect("staff_round", round_uuid=round_obj.uuid)
        locked.delete_with_records()
    events.competition("round_deleted", request=request, round_id=round_obj.pk, detail={"name": round_obj.name, "attempts": attempts})
    messages.success(request, f"Round {round_obj.name} deleted" + (f", with its {attempts} attempt(s)." if attempts else "."))
    return redirect("staff_rounds")
