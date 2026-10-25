"""Students join and leave teams during registration (dashboard/views/teams.py),
through the real URLs and middleware and as the dashboard's own database role
(conftest.app_roles / as_role) -- the page used to fail to render at all, and
the role had no grant on team_memberships."""

from __future__ import annotations

import re
import threading
from datetime import timedelta

import pytest

from conftest import REPO, as_role
from test_dashboard_login import HOST, proxied, site  # noqa: F401 (fixture)

pytest.importorskip("django")

ORIGIN = {"HTTP_ORIGIN": f"https://{HOST}"}


def test_every_template_compiles(django_env):
    """A template syntax error is a 500 on every request to its page."""
    from django.template import engines

    engine = engines["django"]
    templates = REPO / "projects" / "dashboard" / "dashboard" / "templates"
    for path in sorted(templates.rglob("*.html")):
        engine.from_string(path.read_text())


@pytest.fixture
def registration(site, app_roles):  # noqa: F811
    """Registration open; two teams; a student signed in as the dashboard role."""
    from django.utils import timezone

    from core.models.competition import ApplicationSetting
    from core.models.team import Team
    from core.models.user import User

    comp = ApplicationSetting.get_current_competition()
    comp.registration_starts_at = timezone.now() - timedelta(hours=1)
    comp.registration_ends_at = timezone.now() + timedelta(hours=1)
    comp.save()
    red = Team.objects.create(name="red", competition=comp, max_members=1)
    blue = Team.objects.create(name="blue", competition=comp, max_members=2)
    student = User.objects.create(username="stu")
    site.force_login(student)
    with as_role("django_dashboard"):
        yield site, student, red, blue


def post(client, path):
    page = client.get("/profile/", **proxied(HOST))
    token = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content).group(1).decode()
    return client.post(path, {"csrfmiddlewaretoken": token}, **ORIGIN, **proxied(HOST))


def test_the_page_lists_the_teams_with_their_occupancy(registration):
    site, student, red, blue = registration
    response = site.get("/teams/", **proxied(HOST))
    assert response.status_code == 200
    page = response.content.decode()
    assert f'/join_team/{red.uuid}' in page and f'/join_team/{blue.uuid}' in page
    assert "0 / 1" in page and "0 / 2" in page


def test_join_and_leave(registration):
    site, student, red, blue = registration
    assert post(site, f"/join_team/{blue.uuid}").status_code == 302
    assert list(student.teams.all()) == [blue]
    page = site.get("/teams/", **proxied(HOST)).content.decode()
    assert f"/leave_team/{blue.uuid}" in page and "/join_team/" not in page
    # One team per competition.
    assert post(site, f"/join_team/{red.uuid}").status_code == 400
    assert post(site, f"/leave_team/{blue.uuid}").status_code == 302
    assert not student.teams.exists()


def test_a_full_team_and_another_competitions_team(registration):
    site, student, red, blue = registration
    from core.models.competition import Competition
    from core.models.team import Team, TeamMembership
    from core.models.user import User

    with as_role("postgres"):
        TeamMembership.objects.create(team=red, user=User.objects.create(username="first"))
        elsewhere = Team.objects.create(name="old", competition=Competition.objects.create(name="2025"))
    post(site, f"/join_team/{red.uuid}")
    assert not student.teams.exists()
    assert post(site, f"/join_team/{elsewhere.uuid}").status_code == 404
    assert not student.teams.exists()


def test_concurrent_joins_never_overfill_a_team(registration):
    """Two students hit Join on the last free place at the same moment."""
    from django.db import connection
    from django.test import Client

    from core.models.user import User

    site, student, red, blue = registration
    with as_role("postgres"):
        other = User.objects.create(username="other")
    clients = []
    for user in (student, other):
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        clients.append(client)
    barrier = threading.Barrier(2)
    codes = []

    def join(client):
        try:
            barrier.wait()
            codes.append(post(client, f"/join_team/{red.uuid}").status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=join, args=(c,)) for c in clients]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [302, 302]
    assert red.users.count() == 1
