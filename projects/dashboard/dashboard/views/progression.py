"""Round progression: the PROGRESSION page (a team's rounds, replayed) and the
progression data behind it and behind the staff TOPOLOGY player
(orchestrator/rounds/progression.py, docs/progression.md).

Team (signed in; a round of the competition of the active environment, Live
or Dev):

    /progression/                                   the page (?round=<id>)
    /progression/rounds/                            the rounds (the player's tabs)
    /progression/rounds/<round>/                    the progression document
    /progression/rounds/<round>/flows/              its connections
    /progression/rounds/<round>/logs/<ref>/         one log record
    /progression/rounds/<round>/download/           the zip (?logs=0|1&network=0|1)

Staff (any run; the TOPOLOGY page):

    /rounds/<run>/progression/, .../flows/, .../logs/<ref>/, .../download/

The token API serves the same documents (dashboard/api.py). What a team sees
is decided by the builder (audience "team"), never by the player.
"""

from __future__ import annotations

from datetime import timedelta

from core import clock
from core.models.competition import Round
from core.models.rounds import RoundRun
from core.models.scoring import FlagCapture, FlagCaptureAttempt
from core.models.logging import LoggingAttempt
from core.models.team import Team
from dashboard.views.decorators import user_in_team_required
from dashboard.views.topology import latest_snapshot, live_address_index, with_team_names
from dashboard.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.cache import never_cache

from orchestrator.rounds import progression

ZERO_UUID = "00000000-0000-0000-0000-000000000000"


class BadFlag(ValueError):
    pass


def flag(request: HttpRequest, name: str, default=True) -> bool:
    """A 0/1 query parameter (the zip's `logs` and `network`)."""
    raw = request.GET.get(name)
    if raw is None or raw == "":
        return default
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    raise BadFlag(f"{name} must be 0 or 1")


def with_links(doc: dict, flows_url: str | None, log_url: str | None) -> dict:
    """Point the document at where its flows and log records are fetched
    (a log URL has `{ref}` where the record's ref goes)."""
    doc["network"]["url"] = flows_url if doc["network"]["available"] else None
    doc["logs"]["url"] = log_url
    return doc


def flows_payload(doc: dict) -> dict:
    return {
        "count": len(doc["timeline"].get("flows", [])),
        "truncated": doc["network"]["truncated"],
        "available": doc["network"]["available"],
        "reason": doc["network"]["reason"],
        "flows": doc["timeline"].get("flows", []),
    }


def zip_response(doc: dict, logs: bool) -> HttpResponse:
    data = progression.package(doc, progression.log_files(doc) if logs else None)
    response = HttpResponse(data, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{progression.filename(doc)}"'
    response["Cache-Control"] = "no-store"
    return response


def _json(data, status=200) -> JsonResponse:
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


# --- the team's page and data ----------------------------------------------------------


def _environment(request: HttpRequest) -> str:
    return "dev" if request.is_dev_environment else "live"


def _rounds(request: HttpRequest):
    competition = request.active_competition
    if competition is None:
        return []
    return list(Round.objects.filter(competition=competition).order_by("round_start", "id"))


def _team_round(request: HttpRequest, round_id: int) -> Round:
    if request.active_competition is None:
        raise Http404("No such round.")
    return get_object_or_404(Round.objects.select_related("competition"), id=round_id, competition=request.active_competition)


def round_state(rnd: Round, now) -> str:
    if rnd.round_start is None or now < rnd.round_start:
        return "upcoming"
    if rnd.round_end is not None and now >= rnd.round_end:
        return "finished"
    return "active"


def default_round(rounds, now):
    """The running round, else the latest that has started, else the first."""
    active = [r for r in rounds if round_state(r, now) == "active"]
    started = [r for r in rounds if round_state(r, now) == "finished"]
    return (active or started[-1:] or rounds[:1] or [None])[0]


def summary(team: Team, rnd: Round, now, environment: str) -> dict:
    """What the page shows without JavaScript: the captured VMs and the
    counts (cheap queries; no flows, no log records)."""
    if environment == "dev":
        doc = progression.build(team, rnd, now=now, environment="dev")
        zero = rnd.round_start or now
        kinds = [e["kind"] for e in doc["timeline"]["events"]]
        return {
            "run": None,
            "captures": [
                {"vm": n["label"], "at": zero + timedelta(seconds=n["captured_at"])} for n in doc["timeline"]["nodes"]
            ],
            "submissions": kinds.count("flag_submitted"),
            "logs": kinds.count("log_received"),
        }
    run = RoundRun.objects.filter(round=rnd, team=team).first()
    captures = (
        FlagCapture.objects.filter(team=team, round=rnd)
        .select_related("vm", "flag_capture_attempt")
        .order_by("flag_capture_attempt__created_at", "id")
    )
    start, end = progression.window(team, rnd, now, "live", run)
    counts = {"submissions": 0, "logs": 0}
    if start is not None:
        scope = {"team_uuid": str(team.uuid), "devbox": False, "created_at__gte": start, "created_at__lte": end}
        counts = {
            "submissions": FlagCaptureAttempt.objects.filter(**scope).count(),
            "logs": LoggingAttempt.objects.filter(**scope).count(),
        }
    return {
        "run": run,
        "captures": [{"vm": c.vm.vm_identifier, "at": c.flag_capture_attempt.created_at} for c in captures],
        **counts,
    }


@login_required
@user_in_team_required
def progression_page(request: HttpRequest, team: Team):
    now = clock.now()
    rounds = _rounds(request)
    selected = None
    if request.GET.get("round", "").isdigit():
        selected = next((r for r in rounds if r.id == int(request.GET["round"])), None)
    selected = selected or default_round(rounds, now)
    environment = _environment(request)
    return render_with_base_info(
        request,
        "progression.html",
        {
            "rounds": [{"round": r, "state": round_state(r, now)} for r in rounds],
            "selected": selected,
            "selected_state": round_state(selected, now) if selected else None,
            "summary": summary(team, selected, now, environment) if selected else None,
            "environment": environment,
        },
    )


@never_cache
@login_required
@user_in_team_required
def team_rounds(request: HttpRequest, team: Team):
    now = clock.now()
    rounds = _rounds(request)
    runs = {}
    if _environment(request) == "live":
        runs = {run.round_id: run.state for run in RoundRun.objects.filter(team=team, round__in=rounds)}
    default = default_round(rounds, now)
    return _json(
        {
            "environment": _environment(request),
            "team": {"uuid": str(team.uuid), "name": team.name},
            "default": default.id if default else None,
            "rounds": [
                {
                    "id": r.id,
                    "name": r.name,
                    "start": r.round_start.isoformat() if r.round_start else None,
                    "end": r.round_end.isoformat() if r.round_end else None,
                    "state": round_state(r, now),
                    "run": runs.get(r.id),
                }
                for r in rounds
            ],
        }
    )


def _team_document(request, team, round_id, flows=False):
    rnd = _team_round(request, round_id)
    doc = progression.build(
        team, rnd, now=clock.now(), audience="team", environment=_environment(request),
        infra=live_address_index(), flows=flows,
    )  # fmt: skip
    log_url = reverse("progression_log", args=[rnd.id, ZERO_UUID]).replace(ZERO_UUID, "{ref}")
    return rnd, with_links(doc, reverse("progression_flows", args=[rnd.id]), log_url)


@never_cache
@login_required
@user_in_team_required
def team_document(request: HttpRequest, round_id: int, team: Team):
    return _json(_team_document(request, team, round_id)[1])


@never_cache
@login_required
@user_in_team_required
def team_flows(request: HttpRequest, round_id: int, team: Team):
    return _json(flows_payload(_team_document(request, team, round_id, flows=True)[1]))


@never_cache
@login_required
@user_in_team_required
def team_log(request: HttpRequest, round_id: int, ref, team: Team):
    rnd = _team_round(request, round_id)
    record = progression.log_record(team, rnd, ref, now=clock.now(), environment=_environment(request))
    if record is None:
        return _json({"error": "No such log in this round."}, status=404)
    return _json(record)


@never_cache
@login_required
@user_in_team_required
def team_download(request: HttpRequest, round_id: int, team: Team):
    try:
        logs, network = flag(request, "logs"), flag(request, "network")
    except BadFlag as error:
        return _json({"error": str(error)}, status=400)
    _, doc = _team_document(request, team, round_id, flows=network)
    return zip_response(doc, logs)


# --- staff: any run (the TOPOLOGY player) -------------------------------------------


def _staff_run(request, uuid):
    if not request.user.is_staff:
        return None
    return get_object_or_404(RoundRun.objects.select_related("round__competition", "team"), uuid=uuid)


def _staff_document(run, flows=False):
    doc = progression.build(
        run.team, run.round, now=clock.now(), audience="staff", environment="live",
        infra=live_address_index(), flows=flows,
    )  # fmt: skip
    log_url = reverse("run_progression_log", args=[run.uuid, ZERO_UUID]).replace(ZERO_UUID, "{ref}")
    return with_links(doc, reverse("run_progression_flows", args=[run.uuid]), log_url)


FORBIDDEN = {"error": "Forbidden"}


@never_cache
@login_required
def run_document(request: HttpRequest, uuid):
    run = _staff_run(request, uuid)
    if run is None:
        return _json(FORBIDDEN, status=403)
    return _json(_staff_document(run))


@never_cache
@login_required
def run_flows(request: HttpRequest, uuid):
    run = _staff_run(request, uuid)
    if run is None:
        return _json(FORBIDDEN, status=403)
    return _json(flows_payload(_staff_document(run, flows=True)))


@never_cache
@login_required
def run_log(request: HttpRequest, uuid, ref):
    run = _staff_run(request, uuid)
    if run is None:
        return _json(FORBIDDEN, status=403)
    record = progression.log_record(run.team, run.round, ref, now=clock.now(), environment="live")
    if record is None:
        return _json({"error": "No such log in this run."}, status=404)
    return _json(record)


@never_cache
@login_required
def run_download(request: HttpRequest, uuid):
    run = _staff_run(request, uuid)
    if run is None:
        return redirect("index")
    try:
        logs, network = flag(request, "logs"), flag(request, "network")
    except BadFlag as error:
        return _json({"error": str(error)}, status=400)
    doc = _staff_document(run, flows=network)
    # A staff download embeds the infrastructure it was viewed against.
    _, model = latest_snapshot()
    doc["scene"] = with_team_names(model) if model else None
    return zip_response(doc, logs)
