"""Enrollment races against PostgreSQL and the real dashboard grants.

Hold a parent row while two requests reach it, then release it together.
This makes the last-place race reproducible without sleeps inside the code
under test: checking occupancy must happen after acquiring the team lock.
"""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from conftest import as_role
from test_dashboard_login import site  # noqa: F401
from test_staff_admin import csrf, post


@pytest.mark.parametrize("emails", [
    ("same@example.test", "same@example.test"),
    ("Case@example.test", "case@example.test"),
])
def test_simultaneous_account_creation_is_a_duplicate_not_a_server_error(db_clean, app_roles, monkeypatch, emails):
    from core import accounts
    from core.models.user import User
    from django.db import connection, transaction

    barrier = threading.Barrier(2)
    original = accounts.generate_password

    def password(*args, **kwargs):
        barrier.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(accounts, "generate_password", password)

    def create(email):
        try:
            with transaction.atomic():
                result = accounts.create_account(email)
                assert User.objects.filter(username__iexact=email).count() == 1
                return result
        finally:
            connection.close()

    with as_role("django_dashboard"), ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(create, emails))
    assert sum(item is not None for item in created) == 1
    assert User.objects.filter(username__iexact=emails[0]).count() == 1


def test_csv_reports_an_overlong_username_and_keeps_the_valid_row(db_clean, app_roles):
    from core import accounts
    from core.models.user import User

    email = "student@" + ".".join(["a" * 63, "b" * 63, "c" * 63]) + ".test"
    assert 150 < len(email) < 254  # valid email; longer than the username column
    parsed = accounts.parse_csv(f"email\n{email}\nvalid@example.test\n")
    assert [row.email for row in parsed.rows] == ["valid@example.test"]
    assert len(parsed.problems) == 1 and "150" in parsed.problems[0]
    with as_role("django_dashboard"):
        created, problems = accounts.import_rows(parsed.rows)
    assert not problems
    assert [item.user.username for item in created] == ["valid@example.test"]
    assert User.objects.filter(username="valid@example.test").exists()


def _clients():
    from core.models.user import User
    from django.test import Client

    admin = User.objects.get(username="admin")
    clients = []
    for _ in range(2):
        client = Client(enforce_csrf_checks=True)
        client.force_login(admin)
        csrf(client, "/profile/")
        clients.append(client)
    return clients


def _race(cluster, table, row_id, requests):
    """Both requests must be blocked on the parent row before release."""
    from django.db import connection

    holder = cluster.connect()
    holder.autocommit = False
    with holder.cursor() as cur:
        cur.execute(f'SELECT id FROM "{table}" WHERE id = %s FOR UPDATE', [row_id])
    observer = cluster.connect()

    def run(request):
        try:
            return request()
        finally:
            connection.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, request) for request in requests]
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    with observer.cursor() as cur:
                        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                    "AND usename = 'django_dashboard' AND wait_event_type = 'Lock'")
                        waiting = cur.fetchone()[0]
                    if waiting >= 2:
                        break
                    if any(f.done() for f in futures):
                        break
                    time.sleep(0.02)
                assert waiting >= 2, "both requests must reach the row lock before release"
            finally:
                holder.commit()  # release even if a request has failed
            responses = [f.result(timeout=20) for f in futures]
    finally:
        holder.close()
        observer.close()
    return responses


@pytest.mark.parametrize("action", ["add", "move", "create", "import", "student-and-staff"])
def test_staff_requests_never_overfill_the_last_place(site, app_roles, django_env, action):  # noqa: F811
    from core.models.competition import ApplicationSetting
    from core.models.team import Team
    from core.models.user import User

    comp = ApplicationSetting.get_current_competition()
    team = Team.objects.create(name="last place", competition=comp, max_members=1)
    users = [User.objects.create(username=f"student{i}@example.test") for i in range(2)]
    clients = _clients()
    if action == "student-and-staff":
        from datetime import timedelta
        from core import clock
        comp.registration_starts_at = clock.now() - timedelta(days=1)
        comp.registration_ends_at = clock.now() + timedelta(days=1)
        comp.save(update_fields=["registration_starts_at", "registration_ends_at"])
        clients[0].force_login(users[0])
        csrf(clients[0], "/teams/")

    def request(i):
        client, user = clients[i], users[i]
        if action == "student-and-staff":
            if i == 0:
                return post(client, f"/join_team/{team.uuid}")
            return post(client, f"/staff/teams/{team.uuid}/members/", {"user": user.pk})
        if action == "add":
            return post(client, f"/staff/teams/{team.uuid}/members/", {"user": user.pk})
        if action == "move":
            return post(client, f"/staff/users/{user.uuid}/team/", {"team": team.pk})
        if action == "create":
            return post(client, "/staff/users/", {"email": f"created{i}@example.test", "team": team.pk})
        return post(client, "/staff/users/import/", {
            "csv_text": f"email,team\nimported{i}@example.test,{team.name}\n",
        })

    with as_role("django_dashboard"):
        responses = _race(django_env, "teams", team.pk, [lambda: request(0), lambda: request(1)])
    assert all(response.status_code in (200, 302) for response in responses)
    assert team.users.count() == 1


@pytest.mark.parametrize("action", ["add", "move", "student-and-staff"])
def test_staff_requests_never_put_one_student_into_two_teams(site, app_roles, django_env, action):  # noqa: F811
    from core.models.competition import ApplicationSetting
    from core.models.team import Team, TeamMembership
    from core.models.user import User

    comp = ApplicationSetting.get_current_competition()
    teams = [Team.objects.create(name=f"team{i}", competition=comp) for i in range(2)]
    student = User.objects.create(username="student@example.test")
    clients = _clients()
    if action == "student-and-staff":
        from datetime import timedelta
        from core import clock
        comp.registration_starts_at = clock.now() - timedelta(days=1)
        comp.registration_ends_at = clock.now() + timedelta(days=1)
        comp.save(update_fields=["registration_starts_at", "registration_ends_at"])
        clients[0].force_login(student)
        csrf(clients[0], "/teams/")

    def request(i):
        if action == "student-and-staff" and i == 0:
            return post(clients[i], f"/join_team/{teams[i].uuid}")
        if action in ("add", "student-and-staff"):
            return post(clients[i], f"/staff/teams/{teams[i].uuid}/members/", {"user": student.pk})
        return post(clients[i], f"/staff/users/{student.uuid}/team/", {"team": teams[i].pk})

    with as_role("django_dashboard"):
        responses = _race(django_env, "users", student.pk, [lambda: request(0), lambda: request(1)])
    assert all(response.status_code == 302 for response in responses)
    assert TeamMembership.objects.filter(user=student, team__competition=comp).count() == 1


@pytest.mark.parametrize("action", ["create", "rename"])
@pytest.mark.parametrize("names", [("Double submit", "Double submit"), ("Case Team", "case team")])
def test_simultaneous_team_creation_reports_a_duplicate(site, app_roles, monkeypatch, names, action):  # noqa: F811
    from core.models.team import Team
    from dashboard.forms import TeamForm
    from django.db import connection

    barrier = threading.Barrier(2)
    original = TeamForm.clean_name

    def clean_name(form):
        name = original(form)
        barrier.wait(timeout=10)
        return name

    monkeypatch.setattr(TeamForm, "clean_name", clean_name)
    clients = _clients()
    from core.models.competition import ApplicationSetting
    comp = ApplicationSetting.get_current_competition()
    teams = [Team.objects.create(name=f"old{i}", competition=comp) for i in range(2)] if action == "rename" else []

    def create(i):
        try:
            path = f"/staff/teams/{teams[i].uuid}/" if teams else "/staff/teams/"
            return post(clients[i], path, {"name": names[i], "max_members": 2})
        finally:
            connection.close()

    with as_role("django_dashboard"), ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(create, range(2)))
    assert sorted(r.status_code for r in responses) == [200, 302]
    assert "already exists" in next(r.content.decode() for r in responses if r.status_code == 200)
    assert Team.objects.filter(name__iexact=names[0]).count() == 1


def test_csv_with_an_oversized_cell_is_a_validation_error(db_clean):
    from core.accounts import CsvError, parse_csv

    with pytest.raises(CsvError):
        parse_csv("email,first name\nstudent@example.test," + "x" * 150_000)


def test_pasted_csv_has_the_same_size_limit_as_an_uploaded_csv(db_clean):
    from dashboard.forms import UserImportForm

    form = UserImportForm({"csv_text": "email\n" + "x" * UserImportForm.MAX_BYTES})
    assert not form.is_valid()
    assert "too large" in str(form.errors)


def test_resize_and_join_keep_capacity_consistent(site, app_roles, django_env):  # noqa: F811
    from core.models.competition import ApplicationSetting
    from core.models.team import Team, TeamMembership
    from core.models.user import User

    team = Team.objects.create(name="resize", competition=ApplicationSetting.get_current_competition(), max_members=2)
    first, second = [User.objects.create(username=f"resize{i}") for i in range(2)]
    TeamMembership.objects.create(team=team, user=first)
    clients = _clients()
    with as_role("django_dashboard"):
        responses = _race(django_env, "teams", team.pk, [
            lambda: post(clients[0], f"/staff/teams/{team.uuid}/", {"name": team.name, "max_members": 1}),
            lambda: post(clients[1], f"/staff/teams/{team.uuid}/members/", {"user": second.pk}),
        ])
    assert all(r.status_code in (200, 302) for r in responses)
    team.refresh_from_db()
    assert team.users.count() <= team.max_members


def test_key_reconfiguration_runs_only_after_membership_commits(site, app_roles, monkeypatch):  # noqa: F811
    from core.models.competition import ApplicationSetting
    from core.models.devbox import Devbox, DevboxSSHKey
    from core.models.team import Team, TeamMembership
    from core.models.user import User
    from dashboard import orchestrator_client
    from dashboard.membership import remove_member
    from django.db import transaction

    team = Team.objects.create(name="keys", competition=ApplicationSetting.get_current_competition())
    user = User.objects.create(username="key-owner")
    TeamMembership.objects.create(team=team, user=user)
    Devbox.objects.create(team=team, index=1)
    DevboxSSHKey.objects.create(team=team, created_by=user, name="key", public_key="ssh-ed25519 AAAA local")
    calls = []
    monkeypatch.setattr(orchestrator_client, "post", lambda path: calls.append(path))
    with as_role("django_dashboard"):
        with transaction.atomic():
            assert remove_member(team, user) == 1
            assert calls == []
            transaction.set_rollback(True)
        assert team.users.filter(pk=user.pk).exists()
        assert DevboxSSHKey.objects.filter(team=team, created_by=user).exists()
        assert calls == []
        with transaction.atomic():
            assert remove_member(team, user) == 1
            assert calls == []
        assert calls == [f"/devbox/{team.uuid}/reconfigure/"]
