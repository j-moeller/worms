"""The team API (dashboard/api.py, docs/team_api.md) through the dashboard's
real URL configuration and middleware, against PostgreSQL with the real
migrations.

Covers: bearer authentication (missing, malformed, unknown, revoked, token in
the URL), team scoping (another team's data and other competitions' rounds
never appear), the scoring results (captured / duplicate / invalid, devbox and
out-of-window submissions excluded), keyset paging (complete, ordered, no
duplicates, also across identical timestamps), the size limits (item limit,
byte budget, at least one item), bad parameters, the statement timeout, and
that no session is created (an anonymous API call must not write to the
database).
"""

from __future__ import annotations

import hashlib
import json

import pytest

pytest.importorskip("django")

DASHBOARD_MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "dashboard.middleware.EnvironmentTransitionMiddleware",
    "dashboard.middleware.UserMetadataMiddleware",
    "dashboard.middleware.ForcePasswordChangeMiddleware",
]

TOKEN_A = "tok-A_" + "a" * 37
TOKEN_B = "tok-B_" + "b" * 37
TOKEN_REVOKED = "tok-R_" + "r" * 37


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def api(db_clean):
    """Two teams, a round with team A's finished run (3 VMs, submissions,
    logs), a round of another competition; the dashboard's URLs and
    middleware."""
    from core.models.competition import Competition, Round
    from core.models.logging import Logging, LoggingAttempt
    from core.models.rounds import RoundRun
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from core.models.team import AccessTokens, Team
    from core.models.user import User
    from core.models.vm import VirtualMachine
    from django.test import Client, override_settings
    from django.utils import timezone

    now = timezone.now()
    comp = Competition.objects.create(name="c", registration_starts_at=now - timezone.timedelta(days=9),
                                      registration_ends_at=now - timezone.timedelta(days=8))
    other = Competition.objects.create(name="other", registration_starts_at=now, registration_ends_at=now)
    user = User.objects.create(username="u")
    a = Team.objects.create(name="team-a", competition=comp)
    b = Team.objects.create(name="team-b", competition=comp)
    AccessTokens.objects.create(name="a", access_token_hash=sha(TOKEN_A), team=a, created_by=user)
    AccessTokens.objects.create(name="b", access_token_hash=sha(TOKEN_B), team=b, created_by=user)
    AccessTokens.objects.create(name="r", access_token_hash=sha(TOKEN_REVOKED), team=a, created_by=user, revoked=True)

    start = now - timezone.timedelta(hours=2)
    r1 = Round.objects.create(seed=1, name="R1", competition=comp, round_start=start - timezone.timedelta(hours=1),
                              round_end=now)
    r_other = Round.objects.create(seed=2, name="X", competition=other, round_start=start, round_end=now)
    run = RoundRun.objects.create(round=r1, team=a, position=0, seed=1, state="finished",
                                  worm_start_at=start, ends_at=start + timezone.timedelta(hours=1))
    RoundRun.objects.create(round=r1, team=b, position=1, seed=1, state="pending")
    vms = [VirtualMachine.objects.create(vm_identifier=f"vm{i}", vm_maintenance_ip=f"10.4.0.{i}", round=r1, run=run,
                                         ctf_flag_hash=sha(f"flag{i}")) for i in range(3)]

    at = lambda s: start + timezone.timedelta(seconds=s)  # noqa: E731
    attempt = lambda team, h, s, **kw: FlagCaptureAttempt.objects.create(  # noqa: E731
        team_uuid=str(team.uuid), vm_flag_hash=h, created_at=at(s), processed=True, **kw)
    captured = attempt(a, sha("flag0"), 10)
    FlagCapture.objects.create(team=a, round=r1, vm=vms[0], flag_capture_attempt=captured)
    attempt(a, sha("flag0"), 20)  # duplicate
    attempt(a, "f" * 64, 30)  # invalid
    for _ in range(5):  # identical timestamps: the keyset must not skip or repeat
        attempt(a, "e" * 64, 40)
    attempt(a, sha("flag1"), 50, devbox=True)  # a devbox submission: not the round's
    attempt(a, sha("flag1"), 4 * 3600)  # long after the run: outside its window
    attempt(b, sha("flag2"), 15)  # another team's
    for i in range(30):
        la = LoggingAttempt.objects.create(team_uuid=str(a.uuid), log_data={"i": i, "pad": "x" * 100}, created_at=at(i))
        Logging.objects.create(team=a, round=r1, logging_attempt=la)
    lb = LoggingAttempt.objects.create(team_uuid=str(b.uuid), log_data={"team": "b"}, created_at=at(5))
    Logging.objects.create(team=b, round=r1, logging_attempt=lb)

    client = Client()

    def get(path, token=TOKEN_A, **extra):
        headers = {"HTTP_HOST": "localhost", "secure": True}
        if token is not None:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        response = client.get(path, **headers, **extra)
        return response, (json.loads(response.content) if response["Content-Type"].startswith("application/json") else None)

    with override_settings(ROOT_URLCONF="dashboard.urls", MIDDLEWARE=DASHBOARD_MIDDLEWARE):
        yield {"get": get, "client": client, "r1": r1, "r_other": r_other, "a": a, "b": b, "run": run}


def walk(get, path, limit):
    """Follow next_cursor to the end; returns all items and the pages."""
    items, pages, url = [], 0, f"{path}?limit={limit}"
    while url:
        response, body = get(url)
        assert response.status_code == 200, body
        assert body["page"]["count"] == len(body["items"]) <= limit
        items += body["items"]
        pages += 1
        url = body["page"]["next"]
        assert pages < 100
    return items, pages


def test_authentication(api):
    get = api["get"]
    for token in (None, "short", "has spaces in it but long enough", TOKEN_A + "x", TOKEN_REVOKED):
        response, body = get("/api/v1/rounds/", token=token)
        assert response.status_code == 401, token
        assert response["WWW-Authenticate"].startswith("Bearer")
        assert body["error"]["code"] == "unauthorized"
    response = api["client"].get("/api/v1/rounds/", HTTP_HOST="localhost", secure=True,
                                 HTTP_AUTHORIZATION=f"Basic {TOKEN_A}")
    assert response.status_code == 401
    response, body = get(f"/api/v1/rounds/?token={TOKEN_A}")
    assert response.status_code == 400 and body["error"]["code"] == "token_in_url"
    response = api["client"].post("/api/v1/rounds/", HTTP_HOST="localhost", secure=True,
                                  HTTP_AUTHORIZATION=f"Bearer {TOKEN_A}")
    assert response.status_code == 405 and response["Allow"] == "GET"


def test_no_session_and_no_caching(api):
    from django.contrib.sessions.models import Session

    for token in (TOKEN_A, None, "garbage-garbage-garbage"):
        response, _ = api["get"]("/api/v1/rounds/", token=token)
        assert "sessionid" not in response.cookies
    assert Session.objects.count() == 0
    response, _ = api["get"]("/api/v1/rounds/")
    assert response["Cache-Control"] == "no-store"


def test_rounds_are_the_teams_own(api):
    response, body = api["get"]("/api/v1/rounds/")
    assert response.status_code == 200
    assert body["team"]["name"] == "team-a"
    assert [r["name"] for r in body["rounds"]] == ["R1"]  # not the other competition's
    r1 = body["rounds"][0]
    assert r1["run"]["state"] == "finished" and r1["competition"] == "main"
    assert r1["scoring"] == f"/api/v1/rounds/{api['r1'].id}/scoring/"


def test_other_competitions_round_is_not_found(api):
    for rid in (api["r_other"].id, 999999):
        for kind in ("scoring", "logging"):
            response, body = api["get"](f"/api/v1/rounds/{rid}/{kind}/")
            assert response.status_code == 404 and body["error"]["code"] == "not_found"


def test_scoring_results_and_scope(api):
    items, _ = walk(api["get"], f"/api/v1/rounds/{api['r1'].id}/scoring/", 500)
    results = [(i["result"], i["vm"]) for i in items]
    assert results[:3] == [("captured", "vm0"), ("duplicate", "vm0"), ("invalid", None)]
    assert results[3:] == [("invalid", None)] * 5
    assert len(items) == 8  # no devbox, out-of-window or other-team submissions
    response, body = api["get"](f"/api/v1/rounds/{api['r1'].id}/scoring/")
    assert body["summary"] == {"captured": 1, "vms": 3}
    # Team B sees only its own (none: its run never started).
    response, body = api["get"](f"/api/v1/rounds/{api['r1'].id}/scoring/", token=TOKEN_B)
    assert body["items"] == [] and body["run"]["state"] == "pending" and body["summary"]["captured"] == 0


def test_scoring_paging_is_complete_across_identical_timestamps(api):
    everything, _ = walk(api["get"], f"/api/v1/rounds/{api['r1'].id}/scoring/", 500)
    paged, pages = walk(api["get"], f"/api/v1/rounds/{api['r1'].id}/scoring/", 2)
    assert pages == 4
    assert [i["id"] for i in paged] == [i["id"] for i in everything]
    assert len({i["id"] for i in paged}) == len(paged)


def test_logging_items_paging_and_scope(api):
    items, pages = walk(api["get"], f"/api/v1/rounds/{api['r1'].id}/logging/", 7)
    assert pages == 5 and len(items) == 30
    assert [i["log"]["i"] for i in items] == list(range(30))
    assert all("team" not in i["log"] for i in items)  # never team B's
    response, body = api["get"](f"/api/v1/rounds/{api['r1'].id}/logging/", token=TOKEN_B)
    assert [i["log"] for i in body["items"]] == [{"team": "b"}]


def test_page_byte_budget(api, monkeypatch):
    from dashboard import api as api_module

    monkeypatch.setattr(api_module, "MAX_PAGE_BYTES", 600)  # ~4 log items
    response, body = api["get"](f"/api/v1/rounds/{api['r1'].id}/logging/?limit=200")
    assert 1 <= body["page"]["count"] < 10 and body["page"]["next_cursor"]
    items, _ = walk(api["get"], f"/api/v1/rounds/{api['r1'].id}/logging/", 200)
    assert len(items) == 30
    # A single item over the budget is still returned (one per page).
    monkeypatch.setattr(api_module, "MAX_PAGE_BYTES", 10)
    response, body = api["get"](f"/api/v1/rounds/{api['r1'].id}/logging/?limit=200")
    assert body["page"]["count"] == 1


def test_bad_parameters(api):
    base = f"/api/v1/rounds/{api['r1'].id}"
    for query in ("limit=0", "limit=501", "limit=abc", "limit=-1"):
        response, body = api["get"](f"{base}/scoring/?{query}")
        assert response.status_code == 400 and body["error"]["code"] == "bad_limit", query
    response, body = api["get"](f"{base}/logging/?limit=201")
    assert response.status_code == 400
    for cursor in ("!!!", "bm9wZQ==", "x" * 80):
        response, body = api["get"](f"{base}/scoring/?cursor={cursor}")
        assert response.status_code == 400 and body["error"]["code"] == "bad_cursor", cursor
    # A logging cursor is not a scoring cursor.
    _, page = api["get"](f"{base}/logging/?limit=1")
    response, body = api["get"](f"{base}/scoring/?cursor={page['page']['next_cursor']}")
    assert response.status_code == 400


def test_statement_timeout(api, monkeypatch):
    """Every API query runs under a statement timeout; a slow one is a 503."""
    from dashboard import api as api_module
    from django.db import connection

    original = api_module._authenticate

    def slow(request):
        with connection.cursor() as cur:
            cur.execute("SELECT pg_sleep(2)")
        return original(request)

    monkeypatch.setattr(api_module, "STATEMENT_TIMEOUT_MS", 100)
    monkeypatch.setattr(api_module, "_authenticate", slow)
    response, body = api["get"]("/api/v1/rounds/")
    assert response.status_code == 503 and body["error"]["code"] == "unavailable"
    # The timeout was local to that request's transaction.
    with connection.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        assert cur.fetchone()[0] == "0"
