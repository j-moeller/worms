"""Concurrent staff configuration must preserve the non-overlapping round schedule.
(The development environment these tests also covered is gone with dev rounds.)"""

from conftest import as_role
from test_dashboard_login import site  # noqa: F401
from test_enrollment_concurrency import _clients, _race
from test_staff_admin import post, round_data


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
            lambda: post(clients[0], f"/staff/rounds/{rounds[0].uuid}/", {**data, "name": "edited first"}),
            lambda: post(clients[1], f"/staff/rounds/{rounds[1].uuid}/", {**data, "name": "edited second"}),
        ])
    assert sorted(r.status_code for r in responses) == [200, 302]
