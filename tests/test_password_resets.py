"""The record of password resets (core/accounts.py, reset_password; table password_resets).

Through the dashboard's real URLs and middleware, as its own database role with its real grants
(conftest.as_role), so the writes succeed or fail as they would in production.
test_database.py checks that the role cannot change or remove a row.
"""

from __future__ import annotations

import uuid

import pytest

from conftest import REPO, as_role
from test_dashboard_login import site  # noqa: F401 (fixture)
from test_staff_admin import get, passwords, post, staff_site  # noqa: F401 (fixture)

pytest.importorskip("django")


def _rows():
    from core.models.user import PasswordReset

    return list(PasswordReset.objects.order_by("id"))


def _reset(client, target, data=None, page="/profile/"):
    return post(client, f"/staff/users/{target.uuid}/password/", data, page=page)


def _student(name="stu@uni.example"):
    from core import accounts

    return accounts.create_account(name)


def _staff(name="tutor"):
    from core.models.user import User

    with as_role("postgres"):  # staff accounts are made on the console
        return User.objects.create(username=name, is_staff=True)


def _admin():
    from core.models.user import User

    return User.objects.get(username="admin")


def test_a_reset_by_the_admin_is_recorded(staff_site):  # noqa: F811
    from django.utils import timezone

    student = _student().user
    assert _rows() == [], "creating an account is not a reset"

    [password] = passwords(_reset(staff_site, student))

    [row] = _rows()
    admin = _admin()
    assert (row.actor_uuid, row.actor_username, row.actor_role) == (admin.uuid, "admin", "admin")
    assert (row.target_uuid, row.target_username, row.target_role) == (student.uuid, "stu@uni.example", "student")
    assert abs((timezone.now() - row.created_at).total_seconds()) < 60

    student.refresh_from_db()
    assert student.check_password(password) and student.must_change_password
    stored = " ".join(str(getattr(row, f.name)) for f in row._meta.fields)
    assert password not in stored and student.password not in stored

    # Each reset has its own row.
    _reset(staff_site, student)
    assert len(_rows()) == 2


def test_a_reset_by_staff_is_recorded(staff_site, site):  # noqa: F811
    student, tutor = _student().user, _staff()
    site.force_login(tutor)

    assert passwords(_reset(site, student))

    [row] = _rows()
    assert (row.actor_uuid, row.actor_username, row.actor_role) == (tutor.uuid, "tutor", "staff")
    assert (row.target_uuid, row.target_role) == (student.uuid, "student")


def test_the_admin_resetting_a_staff_account_is_recorded(staff_site):  # noqa: F811
    tutor = _staff()

    assert passwords(_reset(staff_site, tutor))

    [row] = _rows()
    assert (row.actor_role, row.target_username, row.target_role) == ("admin", "tutor", "staff")


def test_a_refused_reset_changes_nothing_and_records_nothing(staff_site, site):  # noqa: F811
    from core.models.user import User

    created, classmate, tutor, tutor2 = _student(), _student("ben@uni.example"), _staff(), _staff("tutor2")
    admin = _admin()
    before = {user.pk: user.password for user in User.objects.all()}

    # The admin itself, through these pages.
    assert _reset(staff_site, admin).status_code == 404
    # Staff: not other staff, not themselves, not the admin.
    site.force_login(tutor)
    for target in (tutor2, tutor, admin):
        assert _reset(site, target).status_code == 404
    # A student, past the first-login page, so the request reaches the staff check.
    User.objects.filter(pk=created.user.pk).update(must_change_password=False)
    site.force_login(created.user)
    for target in (classmate.user, created.user, tutor, admin):
        refused = _reset(site, target)
        assert (refused.status_code, refused.url) == (302, "/")  # staff_required: to the home page
    # Signed out.
    site.logout()
    assert _reset(site, classmate.user, page="/login/").status_code == 302

    assert _rows() == []
    assert {user.pk: user.password for user in User.objects.all()} == before


def test_a_request_cannot_name_another_actor(staff_site, site):  # noqa: F811
    student, tutor = _student().user, _staff()
    site.force_login(tutor)

    _reset(site, student, {
        "by": "admin",
        "actor_uuid": str(uuid.uuid4()),
        "actor_username": "someone-else",
        "actor_role": "admin",
        "target_username": "someone-else",
    })  # fmt: skip

    [row] = _rows()
    assert (row.actor_uuid, row.actor_username, row.actor_role) == (tutor.uuid, "tutor", "staff")
    assert row.target_username == student.username


def test_no_reset_without_its_record(staff_site):  # noqa: F811
    """The row cannot be written, as during a deploy before the grant is applied: the password
    stays as it was. The real refusal from the database, not a stand-in."""
    from django.db.utils import ProgrammingError

    created = _student()
    with as_role("postgres"):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("REVOKE INSERT ON password_resets FROM django_dashboard")
    try:
        with pytest.raises(ProgrammingError, match="permission denied"):
            _reset(staff_site, created.user)
    finally:
        with as_role("postgres"):
            with connection.cursor() as cursor:
                cursor.execute("GRANT INSERT ON password_resets TO django_dashboard")

    created.user.refresh_from_db()
    assert created.user.check_password(created.password)
    assert _rows() == []


def test_a_reset_while_viewing_as_someone_is_refused(staff_site):  # noqa: F811
    """View as is read only: no row can be written in the viewed account's name."""
    student, tutor = _student().user, _staff()
    assert post(staff_site, f"/view-as/{tutor.uuid}/").status_code == 302
    before = student.password

    assert _reset(staff_site, student).status_code == 403

    student.refresh_from_db()
    assert student.password == before
    assert _rows() == []


def test_a_row_outlives_the_accounts(staff_site):  # noqa: F811
    from core.models.user import User

    student = _student().user
    student_uuid = student.uuid
    _reset(staff_site, student)

    with as_role("postgres"):
        User.objects.filter(pk=student.pk).delete()

    [row] = _rows()
    assert (row.target_uuid, row.target_username) == (student_uuid, "stu@uni.example")


def test_the_django_admin_has_no_way_to_set_a_password(staff_site):  # noqa: F811
    """A reset there would bypass the record. The admin is signed in, so a 404 means no such page."""
    student = _student().user
    assert get(staff_site, f"/admin/core/user/{student.pk}/password/").status_code == 404
    assert get(staff_site, f"/admin/core/user/{student.pk}/change/").status_code == 404


def test_no_other_code_sets_a_password():
    """A new place that sets someone's password must also write a row, or say why it does not.
    Needs no database."""
    dashboard = REPO / "projects" / "dashboard"
    found = {}
    for path in dashboard.rglob("*.py"):
        uses = path.read_text().count(".set_password(")
        if uses:
            found[str(path.relative_to(dashboard))] = uses
    assert found == {
        "core/accounts.py": 2,  # a new account; reset_password, which records it
        "core/management/commands/reset_admin_password.py": 1,  # the admin's, on the console
    }
