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

A round is edited fully before it starts. Afterwards its end, its gameplan and its
duration per team can change until its first attempt starts (the conductor pins the
gameplan then; a round without one waits). It is deleted only before its runs exist.
"""

from __future__ import annotations

from collections import Counter

from core import clock, events, schedule
from core.locking import serialized_resource
from core.models.competition import Competition, Round
from core.models.rounds import HostJob, RoundRun
from core.models.scoring import FlagCapture
from core.models.team import Team
from dashboard.forms import RoundForm, overlapping_round
from dashboard.views.decorators import refuse, staff_required
from dashboard.views.staff import delete_unreferenced
from dashboard.views.util import render_with_base_info
from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.http import HttpRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

ACTIVE_STATES = [s for s in RoundRun.State.values if s not in ("pending", "finished", "failed", "skipped")]


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
                    )  # fmt: skip
                    events.competition("round_created", request=request, round_id=created.pk, detail={"name": created.name})
                    note = "" if created.gameplan else " It waits for a gameplan: choose one before its first attempt."
                    messages.success(request, f"Round {created.name} created. Its runs are planned when it starts.{note}")
                    return redirect("staff_round", round_uuid=created.uuid)

    def listing(competition):
        rows = []
        for r in Round.objects.filter(competition=competition).order_by("round_start"):
            states = Counter(RoundRun.objects.filter(round=r).values_list("state", flat=True))
            rows.append({
                "round": r,
                "status": _status(r, now),
                "states": sorted(states.items()),
                "captures": FlagCapture.objects.filter(round=r, vm__run__state=RoundRun.State.FINISHED).count(),
                "estimate": _estimate(r, teams),
            })  # fmt: skip
        return rows

    active_run = (
        RoundRun.objects.filter(state__in=ACTIVE_STATES).select_related("round", "team")
        .order_by("round_id", "position", "attempt").first()
    )
    return render_with_base_info(request, "staff/rounds.html", {
        "form": form,
        "live_rounds": listing(comp),
        "teams": teams,
        "active_run": active_run,
        "jobs": HostJob.objects.filter(state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING])
        .select_related("run__team", "run__round").order_by("id")[:20],
        "failed_jobs": HostJob.objects.filter(state=HostJob.State.FAILED)
        .select_related("run__team", "run__round").order_by("-id")[:5],
        "timing": schedule.timing_defaults(),
    })  # fmt: skip


def _max_retries():
    from django.conf import settings

    return getattr(settings, "ROUND_CONDUCTOR", {}).get("max_retries", 3)


def _attempt_started(round_obj) -> bool:
    """An attempt of the round has begun (or its gameplan is pinned): it can no longer change."""
    return RoundRun.objects.filter(round=round_obj).exclude(state=RoundRun.State.PENDING, gameplan_sha256="").exists()


@staff_required
def round_detail(request: HttpRequest, round_uuid):
    comp = request.competition
    round_obj = get_object_or_404(Round, uuid=round_uuid, competition=comp)
    now = clock.now()
    runs = list(
        RoundRun.objects.filter(round=round_obj)
        .select_related("team", "upload__artifact")
        .annotate(vm_count=Count("virtual_machines"))
        .order_by("position", "attempt")
    )
    # Per attempt: a failed one's captures are kept for the record but never count.
    captures = Counter(FlagCapture.objects.filter(round=round_obj).values_list("vm__run_id", flat=True))
    for run in runs:
        run.captured = captures.get(run.pk, 0)
        nodes = (run.topology or {}).get("nodes") or []
        run.network_count = sum(1 for node in nodes if isinstance(node, dict) and node.get("type") == "network")
    # Started, or its runs planned (from its seed and window): only its end, gameplan and
    # duration per team can still change.
    started = (round_obj.round_start is not None and round_obj.round_start <= now) or bool(runs)
    editable = not any(run.state != RoundRun.State.PENDING or run.gameplan_sha256 for run in runs)
    form = None
    if editable:
        initial = RoundForm.initial_from(round_obj)
        form = RoundForm(initial=initial, competition=comp, instance=round_obj, started=started)
        if request.method == "POST":
            form = RoundForm(request.POST, competition=comp, instance=round_obj, started=started)
            if form.is_valid():
                data = dict(form.cleaned_data)
                start = data.get("round_start", round_obj.round_start)
                end = data["round_end"]
                duration = form.team_duration_seconds()
                with transaction.atomic():
                    Competition.objects.select_for_update().get(pk=round_obj.competition_id)
                    with serialized_resource("round-plan", round_obj.pk):
                        # Against the conductor planning the round meanwhile.
                        round_obj = get_object_or_404(Round, pk=round_obj.pk, competition=comp)
                        if _attempt_started(round_obj):
                            messages.error(request, "An attempt of the round has started; it can no longer be changed.")
                            return redirect("staff_round", round_uuid=round_obj.uuid)
                        if not started and ((round_obj.round_start is not None and round_obj.round_start <= clock.now())
                                            or RoundRun.objects.filter(round=round_obj).exists()):
                            messages.error(request, "The round has started meanwhile; reload before editing it.")
                            return redirect("staff_round", round_uuid=round_obj.uuid)
                        _check_window(form, round_obj.competition, start, end, exclude=round_obj)
                        _check_fit(form, start, end, duration, Team.objects.filter(competition=round_obj.competition).count())
                        if not form.errors:
                            if started:
                                round_obj.round_end, round_obj.gameplan = end, data["gameplan"]
                                round_obj.team_duration, round_obj.gameplan_status = duration, ""
                                # Runs planned already take both from the round when the conductor
                                # pins its gameplan (the dashboard cannot write runs).
                                round_obj.save(update_fields=["round_end", "gameplan", "team_duration", "gameplan_status"])
                            else:
                                round_obj.name, round_obj.round_start, round_obj.round_end = data["name"], start, end
                                round_obj.seed, round_obj.gameplan = form.chosen_seed(), data["gameplan"]
                                # The orchestrator checks it again against the new values.
                                round_obj.team_duration, round_obj.gameplan_status = duration, ""
                                round_obj.save(update_fields=["name", "seed", "round_start", "round_end", "gameplan",
                                                              "team_duration", "gameplan_status"])  # fmt: skip
                            events.competition("round_updated", request=request, round_id=round_obj.pk, detail={"name": round_obj.name})
                            messages.success(request, f"Saved {round_obj.name}.")
                            return redirect("staff_round", round_uuid=round_obj.uuid)
    elif request.method == "POST":
        messages.error(request, "An attempt of the round has started; it can no longer be changed.")
        return redirect("staff_round", round_uuid=round_obj.uuid)

    teams = Team.objects.filter(competition=round_obj.competition).count()
    return render_with_base_info(request, "staff/round.html", {
        "round": round_obj,
        "status": _status(round_obj, now),
        "runs": runs,
        "form": form,
        "started": started,
        "timing": schedule.timing_defaults(),
        "max_retries": _max_retries(),
        "estimate": _estimate(round_obj, teams),
        "jobs": HostJob.objects.filter(run__round=round_obj).select_related("run__team").order_by("-id")[:50],
    })  # fmt: skip


@staff_required
@require_POST
def round_delete(request: HttpRequest, round_uuid):
    comp = request.competition
    round_obj = get_object_or_404(Round, uuid=round_uuid, competition=comp)
    with serialized_resource("round-plan", round_obj.pk):
        if RoundRun.objects.filter(round=round_obj).exists() or not delete_unreferenced(Round, round_obj.pk):
            messages.error(request, f"{round_obj.name} has runs and is kept.")
            return redirect("staff_round", round_uuid=round_obj.uuid)
    events.competition("round_deleted", request=request, round_id=round_obj.pk, detail={"name": round_obj.name})
    messages.success(request, f"Round {round_obj.name} deleted.")
    return redirect("staff_rounds")
