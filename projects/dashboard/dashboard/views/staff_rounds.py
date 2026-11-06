"""Staff: the rounds of the competition -- defining them, and following them.

A round is a database record: its window, its seed and the parameters every
team's run gets (the sandbox size, the game plan, the timing;
core/schedule.py). Creating one builds nothing. At the round's start the
conductor on the orchestrator plans one run per team. The round agent on the
Proxmox host builds the round's sandbox from the seed for the first run, with
OpenTofu (root on the host), and snapshots it; every run starts from that
snapshot, gets its team's worm, and the last one destroys the sandbox
(docs/rounds.md). This page shows how far that got: the runs, their host jobs
and errors, and the architecture each prepare reported.

A round is edited only before it starts (afterwards only its end can move) and
deleted only before its runs exist.
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
    return schedule.estimate(round_obj.round_start, round_obj.round_end, round_obj.sandbox_params, teams)


def _check_window(form, competition, start, end, exclude=None):
    """Rounds of one competition do not overlap (Live runs are sequential)."""
    if start is None or end is None:
        return
    clash = overlapping_round(competition, start, end, exclude=exclude)
    if clash is not None:
        form.add_error("round_start", f"Overlaps {clash.name} ({clash.round_start:%Y-%m-%d %H:%M} – {clash.round_end:%Y-%m-%d %H:%M} UTC).")


def _check_fit(form, start, end, params, teams):
    estimate = schedule.estimate(start, end, params, teams)
    if not estimate.fits:
        form.add_error(
            "round_end",
            f"{teams} teams need {schedule.format_duration(estimate.needed)} "
            f"({schedule.format_duration(estimate.slot)} each, one after another); "
            f"this round is {schedule.format_duration(estimate.available)} "
            f"(room for {estimate.max_teams}). Make it longer or the runs shorter.",
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
            start, end, params = data["round_start"], data["round_end"], form.sandbox_params()
            with transaction.atomic():
                # One round at a time per competition: two staff saving at once cannot
                # both pass the overlap check.
                Competition.objects.select_for_update().get(pk=comp.pk)
                teams = Team.objects.filter(competition=comp).count()
                _check_window(form, comp, start, end)
                _check_fit(form, start, end, params, teams)
                if not form.errors:
                    created = Round.objects.create(
                        competition=comp, name=data["name"], seed=form.chosen_seed(),
                        round_start=start, round_end=end, sandbox_params=params,
                    )  # fmt: skip
                    events.competition("round_created", request=request, round_id=created.pk, detail={"name": created.name})
                    messages.success(request, f"Round {created.name} created. Its runs are planned when it starts.")
                    return redirect("staff_round", round_id=created.pk)

    def listing(competition):
        rows = []
        for r in Round.objects.filter(competition=competition).order_by("round_start"):
            states = Counter(RoundRun.objects.filter(round=r).values_list("state", flat=True))
            rows.append({
                "round": r,
                "status": _status(r, now),
                "states": sorted(states.items()),
                "captures": FlagCapture.objects.filter(round=r).count(),
                "estimate": _estimate(r, teams),
            })  # fmt: skip
        return rows

    active_run = (
        RoundRun.objects.filter(state__in=ACTIVE_STATES).select_related("round", "team").order_by("round_id", "position").first()
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


@staff_required
def round_detail(request: HttpRequest, round_id):
    comp = request.competition
    round_obj = get_object_or_404(Round, pk=round_id, competition=comp)
    now = clock.now()
    runs = list(
        RoundRun.objects.filter(round=round_obj)
        .select_related("team", "upload__artifact")
        .annotate(vm_count=Count("virtual_machines"))
        .order_by("position")
    )
    captures = Counter(FlagCapture.objects.filter(round=round_obj).values_list("team_id", flat=True))
    for run in runs:
        run.captured = captures.get(run.team_id, 0)
        # The generator graph prepare reported (scripts/generate_sandbox.py).
        nodes = (run.topology or {}).get("nodes") or []
        run.network_count = sum(1 for node in nodes if isinstance(node, dict) and node.get("type") == "network")
    started = round_obj.round_start is not None and round_obj.round_start <= now
    editable = not runs
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
                params = round_obj.sandbox_params if started else form.sandbox_params()
                with transaction.atomic():
                    Competition.objects.select_for_update().get(pk=round_obj.competition_id)
                    with serialized_resource("round-plan", round_obj.pk):
                        # Against the conductor planning the round meanwhile.
                        round_obj = get_object_or_404(Round, pk=round_obj.pk, competition=comp)
                        if RoundRun.objects.filter(round=round_obj).exists():
                            messages.error(request, "The round's runs are planned; it can no longer be changed.")
                            return redirect("staff_round", round_id=round_obj.pk)
                        if not started and round_obj.round_start is not None and round_obj.round_start <= clock.now():
                            messages.error(request, "The round has started; reload before editing its end.")
                            return redirect("staff_round", round_id=round_obj.pk)
                        _check_window(form, round_obj.competition, start, end, exclude=round_obj)
                        _check_fit(form, start, end, params, Team.objects.filter(competition=round_obj.competition).count())
                        if not form.errors:
                            if started:
                                round_obj.round_end = end
                                round_obj.save(update_fields=["round_end"])
                            else:
                                round_obj.name, round_obj.round_start, round_obj.round_end = data["name"], start, end
                                round_obj.seed, round_obj.sandbox_params = form.chosen_seed(), params
                                round_obj.save(update_fields=["name", "seed", "round_start", "round_end", "sandbox_params"])
                            events.competition("round_updated", request=request, round_id=round_obj.pk, detail={"name": round_obj.name})
                            messages.success(request, f"Saved {round_obj.name}.")
                            return redirect("staff_round", round_id=round_obj.pk)
    elif request.method == "POST":
        messages.error(request, "The round's runs are planned; it can no longer be changed.")
        return redirect("staff_round", round_id=round_obj.pk)

    teams = Team.objects.filter(competition=round_obj.competition).count()
    return render_with_base_info(request, "staff/round.html", {
        "round": round_obj,
        "status": _status(round_obj, now),
        "runs": runs,
        "form": form,
        "started": started,
        "params": round_obj.sandbox_params or {},
        "timing": schedule.run_timing(round_obj.sandbox_params),
        "estimate": _estimate(round_obj, teams),
        "jobs": HostJob.objects.filter(run__round=round_obj).select_related("run__team").order_by("-id")[:50],
    })  # fmt: skip


@staff_required
@require_POST
def round_delete(request: HttpRequest, round_id):
    comp = request.competition
    round_obj = get_object_or_404(Round, pk=round_id, competition=comp)
    with serialized_resource("round-plan", round_obj.pk):
        if RoundRun.objects.filter(round=round_obj).exists() or not delete_unreferenced(Round, round_obj.pk):
            messages.error(request, f"{round_obj.name} has runs and is kept.")
            return redirect("staff_round", round_id=round_obj.pk)
    events.competition("round_deleted", request=request, round_id=round_id, detail={"name": round_obj.name})
    messages.success(request, f"Round {round_obj.name} deleted.")
    return redirect("staff_rounds")
