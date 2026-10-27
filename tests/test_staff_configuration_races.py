"""Concurrent staff configuration must preserve one development environment
and the non-overlapping round schedule."""
from concurrent.futures import ThreadPoolExecutor
import threading

from conftest import as_role
from test_dashboard_login import site  # noqa: F401
from test_enrollment_concurrency import _clients, _race
from test_staff_admin import post, round_data


def test_two_development_setup_requests_create_one_consistent_environment(site, app_roles, django_env):  # noqa: F811
    from core.models.competition import ApplicationSetting, Competition
    from core.models.team import Team

    comp = ApplicationSetting.get_current_competition()
    Team.objects.create(name="existing", competition=comp)
    clients = _clients()
    with as_role("django_dashboard"):
        responses = _race(django_env, "competitions", comp.pk, [
            lambda: post(clients[0], "/staff/competition/development/"),
            lambda: post(clients[1], "/staff/competition/development/"),
        ])
    assert [r.status_code for r in responses] == [302, 302]
    comp.refresh_from_db()
    assert Competition.objects.filter(is_dev_competition=True).count() == 1
    assert list(Team.objects.values_list("development_competition_id", flat=True)) == [comp.development_competition_id]


def test_team_creation_uses_the_development_environment_created_during_validation(site, app_roles, monkeypatch):  # noqa: F811
    from core.models.competition import ApplicationSetting
    from core.models.team import Team
    from dashboard.forms import TeamForm
    from django.db import connection

    clients = _clients()
    entered, release = threading.Event(), threading.Event()
    original = TeamForm.clean_name

    def clean_name(form):
        name = original(form)
        entered.set()
        assert release.wait(10)
        return name

    monkeypatch.setattr(TeamForm, "clean_name", clean_name)

    def create():
        try:
            return post(clients[0], "/staff/teams/", {"name": "new team", "max_members": 2})
        finally:
            connection.close()

    with as_role("django_dashboard"), ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(create)
        try:
            assert entered.wait(10)
            assert post(clients[1], "/staff/competition/development/").status_code == 302
        finally:
            release.set()
        assert pending.result(timeout=15).status_code == 302
    comp = ApplicationSetting.get_current_competition()
    assert comp.development_competition_id is not None
    assert Team.objects.get(name="new team").development_competition_id == comp.development_competition_id


def test_two_creators_cannot_schedule_overlapping_rounds(site, app_roles, django_env):  # noqa: F811
    from core.models.competition import ApplicationSetting, Round

    comp = ApplicationSetting.get_current_competition()
    clients = _clients()
    with as_role("django_dashboard"):
        responses = _race(django_env, "competitions", comp.pk, [
            lambda: post(clients[0], "/staff/rounds/", round_data(name="first")),
            lambda: post(clients[1], "/staff/rounds/", round_data(name="second")),
        ])
    assert sorted(r.status_code for r in responses) == [200, 302]
    assert Round.objects.count() == 1


def test_two_editors_cannot_move_rounds_into_the_same_window(site, app_roles, django_env):  # noqa: F811
    from datetime import timedelta
    from core import clock
    from core.models.competition import ApplicationSetting, Round

    comp = ApplicationSetting.get_current_competition()
    now = clock.now()
    rounds = [Round.objects.create(name=f"old{i}", competition=comp, seed=i,
        round_start=now+timedelta(days=i+1), round_end=now+timedelta(days=i+1,hours=6)) for i in range(2)]
    clients = _clients()
    target = now + timedelta(days=4)
    data = round_data(round_start=target.strftime("%Y-%m-%dT%H:%M"),
        round_end=(target+timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M"))
    with as_role("django_dashboard"):
        responses = _race(django_env, "rounds", [r.pk for r in rounds], [
            lambda: post(clients[0], f"/staff/rounds/{rounds[0].pk}/", {**data, "name": "edited first"}),
            lambda: post(clients[1], f"/staff/rounds/{rounds[1].pk}/", {**data, "name": "edited second"}),
        ])
    assert sorted(r.status_code for r in responses) == [200, 302]
