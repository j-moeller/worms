"""Team API: a team's own scoring and logging of a round, as JSON.

    GET /api/v1/rounds/                               the team's rounds (and its run in each)
    GET /api/v1/rounds/<round>/scoring/?limit=&cursor=  every flag the team's sandbox submitted
                                                      during its run, with the result
    GET /api/v1/rounds/<round>/logging/?limit=&cursor=  every log attributed to the team in that round

    Authorization: Bearer <access token>

The token is one of the team's access tokens (ACCESS page; stored only as its
SHA-256, revocable). It selects the team: nothing of another team is ever
returned, and a round outside the team's competitions is a 404.

Availability (docs/team_api.md) -- a client can ask for a lot of data, so:
- keyset pagination: each page is one bounded range scan over an index
  (flag_attempts_team_time, logging_team_round), however deep the client
  pages; the page ends with an opaque `next_cursor`;
- a page holds at most `limit` items (scoring <= 500, logging <= 200) and at
  most MAX_PAGE_BYTES of item data -- a response is never much above 1 MiB;
- every query runs under a statement timeout (STATEMENT_TIMEOUT_MS);
- these paths write nothing: no session (the session middlewares skip /api/),
  no CSRF, GET only;
- nginx rate-limits /api/ per client address and per token before Django
  (ansible role nginx_tls_django, nginx_team_api_*).

Log records are what the team's worm sent (JSON, size-capped by the logging
server); they are returned as stored.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from functools import wraps

from core.models.competition import Round
from core.models.logging import Logging
from core.models.rounds import RoundRun
from core.models.scoring import FlagCapture, FlagCaptureAttempt
from core.models.team import AccessTokens
from django.db import OperationalError, connection, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from orchestrator.rounds.timeline import classify_submission, run_window

API_PREFIX = "/api/"

# (default, maximum) items per page.
PAGE_SIZES = {"scoring": (100, 500), "logging": (50, 200)}
MAX_PAGE_BYTES = 1024 * 1024
MAX_ROUNDS = 500
STATEMENT_TIMEOUT_MS = 3000

# secrets.token_urlsafe(32) (views/access.py) is 43 characters of [A-Za-z0-9_-].
TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
CURSOR = re.compile(r"^[A-Za-z0-9_-]{1,64}={0,2}$")
EPOCH = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)
MICROSECOND = timedelta(microseconds=1)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _json(data, status=200) -> JsonResponse:
    response = JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "no-store"
    return response


def _error(error: ApiError) -> JsonResponse:
    response = _json({"error": {"code": error.code, "message": error.message}}, status=error.status)
    if error.status == 401:
        response["WWW-Authenticate"] = 'Bearer realm="worms", error="invalid_token"'
    if error.status == 405:
        response["Allow"] = "GET"
    return response


def _authenticate(request):
    scheme, _, token = request.META.get("HTTP_AUTHORIZATION", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not TOKEN.match(token):
        raise ApiError(401, "unauthorized", "Send an access token: Authorization: Bearer <token> (ACCESS page).")
    access = (
        AccessTokens.objects.select_related("team")
        .filter(access_token_hash=hashlib.sha256(token.encode()).hexdigest(), revoked=False)
        .first()
    )
    if access is None:
        raise ApiError(401, "unauthorized", "Unknown or revoked access token.")
    return access.team


def api_view(handler):
    """GET only, bearer authentication, a statement timeout, JSON errors.

    csrf_exempt: nothing here changes state (GET only, enforced below), so a
    POST gets the API's own JSON 405 instead of Django's HTML CSRF page."""

    @csrf_exempt
    @wraps(handler)
    def view(request, *args, **kwargs):
        try:
            if request.method != "GET":
                raise ApiError(405, "method_not_allowed", "Only GET is supported.")
            if "token" in request.GET or "access_token" in request.GET:
                raise ApiError(400, "token_in_url", "Send the token in the Authorization header, never in the URL.")
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
                team = _authenticate(request)
                return _json(handler(request, team, *args, **kwargs))
        except ApiError as error:
            return _error(error)
        except OperationalError:
            # statement_timeout (or the database being unreachable).
            return _error(ApiError(503, "unavailable", "The request took too long; retry later or ask for a smaller page."))

    return view


# --- helpers -----------------------------------------------------------------


def _team_info(team):
    return {"uuid": str(team.uuid), "name": team.name}


def _competitions(team):
    return [cid for cid in (team.competition_id, team.development_competition_id) if cid]


def _round(team, round_id) -> Round:
    rnd = Round.objects.filter(id=round_id, competition_id__in=_competitions(team)).first()
    if rnd is None:
        raise ApiError(404, "not_found", "No such round for your team.")
    return rnd


def _iso(ts):
    return ts.isoformat() if ts else None


def _round_info(rnd, team):
    return {
        "id": rnd.id,
        "name": rnd.name,
        "competition": "development" if rnd.competition_id == team.development_competition_id else "main",
        "start": _iso(rnd.round_start),
        "end": _iso(rnd.round_end),
    }


def _run_info(run):
    if run is None:
        return None
    return {"state": run.state, "position": run.position, "worm_start_at": _iso(run.worm_start_at), "ends_at": _iso(run.ends_at)}


def _limit(request, kind) -> int:
    default, maximum = PAGE_SIZES[kind]
    raw = request.GET.get("limit")
    if raw is None:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= maximum:
        raise ApiError(400, "bad_limit", f"limit must be 1..{maximum}.")
    return int(raw)


# Opaque cursors: "<kind>:<field>:..." in base64url, so a client cannot mix
# them up between endpoints and the format can change later.
def _encode_cursor(*parts) -> str:
    return base64.urlsafe_b64encode(":".join(str(p) for p in parts).encode()).decode()


def _decode_cursor(request, kind, fields) -> list[int] | None:
    raw = request.GET.get("cursor")
    if raw is None or raw == "":
        return None
    try:
        if not CURSOR.match(raw):
            raise ValueError
        parts = base64.urlsafe_b64decode(raw.encode()).decode().split(":")
        if parts[0] != kind or len(parts) != fields + 1 or not all(p.isdigit() and len(p) <= 20 for p in parts[1:]):
            raise ValueError
        return [int(p) for p in parts[1:]]
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_cursor", "Invalid cursor: pass next_cursor from the previous page unchanged.") from None


def _micros(ts) -> int:
    return (ts - EPOCH) // MICROSECOND


def _page(rows, limit, render):
    """Render up to `limit` rows, stopping before MAX_PAGE_BYTES of item data
    (but always at least one). -> (items, last rendered row, more?)."""
    items, size, last = [], 0, None
    for row in rows[:limit]:
        item = render(row)
        cost = len(json.dumps(item, ensure_ascii=False, default=str).encode())
        if items and size + cost > MAX_PAGE_BYTES:
            return items, last, True
        items.append(item)
        size += cost
        last = row
    return items, last, len(rows) > limit


def _page_info(name, rnd, limit, items, next_cursor):
    return {
        "limit": limit,
        "count": len(items),
        "next_cursor": next_cursor,
        "next": f"{reverse(name, args=[rnd.id])}?limit={limit}&cursor={next_cursor}" if next_cursor else None,
    }


# --- endpoints -----------------------------------------------------------------


@api_view
def rounds(request, team):
    qs = Round.objects.filter(competition_id__in=_competitions(team)).order_by("round_start", "id")[:MAX_ROUNDS]
    rounds_ = list(qs)
    runs = {run.round_id: run for run in RoundRun.objects.filter(team=team, round__in=rounds_)}
    return {
        "team": _team_info(team),
        "rounds": [
            {
                **_round_info(rnd, team),
                "run": _run_info(runs.get(rnd.id)),
                "scoring": reverse("api_round_scoring", args=[rnd.id]),
                "logging": reverse("api_round_logging", args=[rnd.id]),
            }
            for rnd in rounds_
        ],
    }


@api_view
def round_scoring(request, team, round_id):
    """Every flag the team's sandbox submitted during its run in the round,
    oldest first, with the result: captured (scored), duplicate (that VM was
    already captured), not_counted, invalid, pending (not processed yet)."""
    rnd = _round(team, round_id)
    limit = _limit(request, "scoring")
    cursor = _decode_cursor(request, "s", 2)
    run = RoundRun.objects.filter(round=rnd, team=team).first()
    captures = FlagCapture.objects.filter(team=team, round=rnd)
    summary = {
        "captured": captures.count(),
        "vms": run.virtual_machines.count() if run else 0,
    }

    items, next_cursor = [], None
    start, end = run_window(run) if run else (None, None)
    if start is not None:
        qs = FlagCaptureAttempt.objects.filter(
            team_uuid=str(team.uuid), devbox=False, created_at__gte=start, created_at__lte=end
        )
        if cursor:
            after = EPOCH + cursor[0] * MICROSECOND
            qs = qs.filter(Q(created_at__gt=after) | Q(created_at=after, id__gt=cursor[1]))
        rows = list(qs.order_by("created_at", "id")[: limit + 1])
        shown = rows[:limit]
        by_attempt = {
            c.flag_capture_attempt_id: c
            for c in FlagCapture.objects.filter(flag_capture_attempt__in=shown).select_related("vm")
        }
        vms = {
            vm.ctf_flag_hash: vm
            for vm in run.virtual_machines.filter(ctf_flag_hash__in={a.vm_flag_hash for a in shown})
        }
        captured_vm_ids = {str(u) for u in captures.values_list("vm__uuid", flat=True)}

        def render(attempt):
            result, vm = classify_submission(attempt, by_attempt.get(attempt.id), vms.get(attempt.vm_flag_hash), captured_vm_ids)
            return {
                "id": attempt.id,
                "submitted_at": _iso(attempt.created_at),
                "flag_hash": attempt.vm_flag_hash,
                "result": result,
                "vm": vm.vm_identifier if vm is not None else None,
            }

        items, last, more = _page(rows, limit, render)
        if more and last is not None:
            next_cursor = _encode_cursor("s", _micros(last.created_at), last.id)

    return {
        "team": _team_info(team),
        "round": _round_info(rnd, team),
        "run": _run_info(run),
        "summary": summary,
        "items": items,
        "page": _page_info("api_round_scoring", rnd, limit, items, next_cursor),
    }


@api_view
def round_logging(request, team, round_id):
    """Every log attributed to the team in the round (sent by its sandbox
    during its run), oldest first, with the record as the worm sent it."""
    rnd = _round(team, round_id)
    limit = _limit(request, "logging")
    cursor = _decode_cursor(request, "l", 1)
    run = RoundRun.objects.filter(round=rnd, team=team).first()

    qs = Logging.objects.filter(team=team, round=rnd)
    if cursor:
        qs = qs.filter(id__gt=cursor[0])
    rows = list(qs.select_related("logging_attempt").order_by("id")[: limit + 1])

    def render(entry):
        attempt = entry.logging_attempt
        return {
            "id": entry.id,
            "uuid": str(attempt.uuid),
            "received_at": _iso(attempt.created_at),
            "log": attempt.log_data,
        }

    items, last, more = _page(rows, limit, render)
    next_cursor = _encode_cursor("l", last.id) if more and last is not None else None
    return {
        "team": _team_info(team),
        "round": _round_info(rnd, team),
        "run": _run_info(run),
        "items": items,
        "page": _page_info("api_round_logging", rnd, limit, items, next_cursor),
    }
