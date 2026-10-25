"""Dashboard pages that read the round/upload models, and `manage.py init`
(behind nginx, the dashboard's real middleware; see test_dashboard_login.py).
"""

from __future__ import annotations

import re

import pytest

from test_dashboard_login import HOST, login, proxied, site  # noqa: F401 (fixture)

pytest.importorskip("django")


@pytest.fixture
def team_upload(site):  # noqa: F811
    """The admin (staff) in a team of the current competition, with one upload."""
    from core.models.competition import ApplicationSetting
    from core.models.team import Team, TeamMembership
    from core.models.user import User
    from core.models.worms import MalwareArtifact, Upload

    competition = ApplicationSetting.get_current_competition()
    admin = User.objects.get(username="admin")
    team = Team.objects.create(name="red", competition=competition)
    TeamMembership.objects.create(user=admin, team=team)
    artifact = MalwareArtifact.objects.create(content_hash="ab" * 32, storage_key="malware-artifacts/x.zip")
    upload = Upload.objects.create(artifact=artifact, user=admin)
    assert login(site, HOST, f"https://{HOST}").status_code == 302
    return team, upload


def test_uploads_page_lists_the_teams_uploads(site, team_upload):  # noqa: F811
    response = site.get("/upload/", **proxied(HOST))
    assert response.status_code == 200
    page = response.content.decode()
    assert "ab" * 32 in page and "admin" in page
    # Not used in any round yet.
    assert 'class="referenced"' not in page
    assert not re.search(r'class="upload-round"', page)


def test_todos_page_counts_vms_and_deployments(site, team_upload):  # noqa: F811
    from datetime import timedelta

    from core import clock
    from core.models.competition import ApplicationSetting, Round

    now = clock.now()
    Round.objects.create(competition=ApplicationSetting.get_current_competition(), name="Round 1", seed=1,
                         round_start=now + timedelta(days=1), round_end=now + timedelta(days=2))  # fmt: skip
    response = site.get("/todos/", **proxied(HOST))
    assert response.status_code == 200
    page = response.content.decode()
    assert "Round 1" in page
    counts = re.findall(r"<span >(\d+)</span>", page)
    assert counts == ["0", "0"], counts  # no VMs, no worm deployments


def test_init_is_idempotent_and_never_sets_a_password(db_clean, monkeypatch):
    """`manage.py init` (run after every migration): the admin and the
    default competition, created once; the admin cannot log in until a
    password is set deliberately."""
    from core.models.competition import ApplicationSetting, Competition
    from core.models.user import User
    from django.core.management import call_command

    monkeypatch.delenv("DJANGO_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("DJANGO_SUPERUSER_PASSWORD", raising=False)
    for _ in range(2):
        call_command("init", competition="WORMS 2026", stdout=open("/dev/null", "w"), stderr=open("/dev/null", "w"))
    admin = User.objects.get(username="admin")
    assert admin.is_staff and admin.is_superuser and admin.is_active
    assert not admin.has_usable_password()
    assert list(Competition.objects.values_list("name", flat=True)) == ["WORMS 2026"]
    assert ApplicationSetting.get_current_competition().name == "WORMS 2026"
    assert User.objects.count() == 1


def test_init_keeps_an_existing_password(db_clean):
    from core.models.user import User
    from django.core.management import call_command

    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    admin.set_password("chosen-by-the-admin")
    admin.save()
    call_command("init", stdout=open("/dev/null", "w"))
    assert User.objects.get(username="admin").check_password("chosen-by-the-admin")


def run_with(team, upload, name, start):
    from datetime import timedelta

    from core.models.competition import Round
    from core.models.rounds import RoundRun

    round = Round.objects.create(competition=team.competition, name=name, seed=1, round_start=start,
                                 round_end=start + timedelta(hours=1))  # fmt: skip
    return RoundRun.objects.create(round=round, team=team, upload=upload, position=0, seed=1)


def test_uploads_page_shows_the_rounds_an_upload_ran_in(site, team_upload):  # noqa: F811
    from datetime import timedelta

    from core import clock

    team, upload = team_upload
    now = clock.now()
    run_with(team, upload, "Round 1", now - timedelta(days=2))
    run_with(team, upload, "Round 2", now - timedelta(days=1))
    page = site.get("/upload/", **proxied(HOST)).content.decode()
    assert 'class="referenced"' in page
    assert re.findall(r'class="upload-round">\s*(.*?)\s*<', page) == ["Round 2", "Round 1"]


def test_todos_counts_the_runs_that_deploy_an_upload(site, team_upload):  # noqa: F811
    from datetime import timedelta

    from core import clock

    team, upload = team_upload
    run_with(team, upload, "Round 1", clock.now() + timedelta(days=1))
    counts = re.findall(r"<span >(\d+)</span>", site.get("/todos/", **proxied(HOST)).content.decode())
    assert counts == ["0", "1"], counts


def test_anonymous_requests_create_no_session(site):  # noqa: F811
    """An anonymous, cookie-less request must not write a django_session row:
    the dev/prod toggle is only for logged-in users, and creating a session per
    request would fill the shared database disk (docs/security-assessment.md, D1).
    """
    from django.contrib.sessions.models import Session

    assert Session.objects.count() == 0
    assert site.get("/login/", **proxied(HOST)).status_code == 200
    assert Session.objects.count() == 0
    # A real login still gets a session (and the dev/prod flag).
    assert login(site, HOST, f"https://{HOST}").status_code == 302
    assert Session.objects.count() == 1
