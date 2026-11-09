"""Planning and staff edits must agree on the immutable snapshot of a round."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading

from conftest import as_role
from test_dashboard_login import site  # noqa: F401
from test_enrollment_concurrency import _clients, _race
from test_staff_admin import post, round_data


def setup_round():
    from core import clock
    from core.models.competition import ApplicationSetting, Round
    from core.models.team import Team

    comp = ApplicationSetting.get_current_competition()
    Team.objects.create(name="planner team", competition=comp)
    return Round.objects.create(name="planning race", competition=comp, seed=17,
        round_start=clock.now()+timedelta(days=1), round_end=clock.now()+timedelta(days=1,hours=6))


def test_simultaneous_planners_return_the_same_runs(site, app_roles, django_env):  # noqa: F811
    from core.models.rounds import RoundRun
    from orchestrator.rounds.planner import plan_round

    round = setup_round()
    with as_role("django_orchestrator"):
        responses = _race(django_env, "rounds", round.pk,
            [lambda: plan_round(round), lambda: plan_round(round)], role="django_orchestrator")
    assert [[r.pk for r in runs] for runs in responses] == [[RoundRun.objects.get().pk]] * 2


def test_staff_cannot_change_parameters_after_planning_started_during_form_validation(site, app_roles, monkeypatch):  # noqa: F811
    from dashboard.forms import RoundForm
    from django.db import connection
    from orchestrator.rounds.planner import plan_round

    round = setup_round()
    clients = _clients()
    entered, release = threading.Event(), threading.Event()
    original = RoundForm.clean

    def clean(form):
        data = original(form)
        entered.set()
        assert release.wait(10)
        return data

    monkeypatch.setattr(RoundForm, "clean", clean)

    def edit():
        try:
            return post(clients[0], f"/staff/rounds/{round.uuid}/", round_data(name=round.name, seed=999))
        finally:
            connection.close()

    with as_role("django_dashboard"), ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(edit)
        try:
            assert entered.wait(10)
            with as_role("django_orchestrator"):
                assert len(plan_round(round)) == 1
        finally:
            release.set()
        assert pending.result(timeout=15).status_code == 302
    round.refresh_from_db()
    assert round.seed == 17  # its run was already planned from this seed
