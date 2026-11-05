"""View as: the admin sees the dashboard as another user does, read only (docs/accounts.md).

The priority is that nobody else can get this, and that it cannot be forged. These tests go
through the dashboard's real middleware, with CSRF checks on, and try to.
"""

from __future__ import annotations

import pytest

from test_dashboard_login import HOST, proxied, site  # noqa: F401 (fixtures)

pytest.importorskip("django")

KEY = "_view_as"
ORIGIN = {"HTTP_ORIGIN": f"https://{HOST}"}
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
SSH_KEY = "ssh-ed25519 " + "A" * 68 + " someone@laptop"


def _client(user):
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    return client


def _get(client, path="/profile/"):
    return client.get(path, **proxied(HOST))


def _post(client, path, data=None, page="/profile/"):
    """A proper form post: the page's own CSRF token and origin."""
    html = _get(client, page).content.decode()
    token = html.split('name="csrfmiddlewaretoken" value="', 1)[1].split('"', 1)[0]
    return client.post(path, {**(data or {}), "csrfmiddlewaretoken": token}, **ORIGIN, **proxied(HOST))


def _start(client, target):
    return _post(client, f"/view-as/{target.uuid}/")


def _leave(client):
    return _post(client, "/view-as/stop/")


def _viewing(client) -> bool:
    return "Back to admin" in _get(client).content.decode()


def _plant(client, entry):
    """What it would take to forge the state: writing into the server-side session directly."""
    session = client.session
    session[KEY] = entry
    session.save()


@pytest.fixture
def people(site):  # noqa: F811
    from core.models.competition import ApplicationSetting
    from core.models.team import Team, TeamMembership
    from core.models.user import User

    competition = ApplicationSetting.get_current_competition()
    admin = User.objects.get(username="admin")
    staff = User.objects.create(username="tutor", is_staff=True)
    student = User.objects.create(username="anna@uni.example", email="anna@uni.example")
    other = User.objects.create(username="ben@uni.example")
    team = Team.objects.create(name="red", competition=competition)
    TeamMembership.objects.create(user=student, team=team)
    return {"admin": admin, "staff": staff, "student": student, "other": other, "team": team}


# ---- Who can start it


def test_the_admin_sees_the_dashboard_as_the_user_does(people):
    admin = _client(people["admin"])
    assert _start(admin, people["student"]).status_code == 302
    page = _get(admin).content.decode()
    assert "Viewing as <strong>anna@uni.example</strong>" in page and "Back to admin" in page
    assert "anna@uni.example" in page and "red" in page  # their account, their team
    assert "Staff tools" not in page  # the admin's own menu is gone


def test_staff_cannot_start_it_on_anyone(people):
    staff = _client(people["staff"])
    for target in ("student", "other", "admin"):
        assert _start(staff, people[target]).status_code == 403
        assert KEY not in staff.session
    assert not _viewing(staff) and "tutor" in _get(staff).content.decode()


def test_a_student_cannot_start_it_on_anyone(people):
    student = _client(people["student"])
    for target in ("other", "staff", "admin"):
        assert _start(student, people[target]).status_code == 403
        assert KEY not in student.session
    assert not _viewing(student)


def test_someone_signed_out_cannot_start_it(people):
    from django.test import Client

    anonymous = Client()
    assert anonymous.post(f"/view-as/{people['student'].uuid}/", **ORIGIN, **proxied(HOST)).status_code == 403
    assert KEY not in anonymous.session


def test_a_staff_flag_is_not_enough_and_a_lost_admin_flag_stops_it(people):
    from core.models.user import User

    User.objects.filter(pk=people["admin"].pk).update(is_superuser=False)  # still staff
    people["admin"].refresh_from_db()
    assert _start(_client(people["admin"]), people["student"]).status_code == 403


def test_it_takes_a_post_with_a_csrf_token(people):
    from django.test import Client

    admin = _client(people["admin"])
    path = f"/view-as/{people['student'].uuid}/"
    assert _get(admin, path).status_code == 405  # a link or an <img> cannot start it
    assert admin.post(path, **ORIGIN, **proxied(HOST)).status_code == 403  # no token
    assert KEY not in admin.session
    forged = Client(enforce_csrf_checks=True)
    forged.force_login(people["admin"])
    response = forged.post(path, {"csrfmiddlewaretoken": "forged"}, **ORIGIN, **proxied(HOST))
    assert response.status_code == 403 and KEY not in forged.session


@pytest.mark.parametrize("who", ["admin", "inactive", "unknown"])
def test_who_cannot_be_viewed(people, who):
    from core.models.user import User

    admin = _client(people["admin"])
    if who == "inactive":
        User.objects.filter(pk=people["student"].pk).update(is_active=False)
    target = {"admin": str(people["admin"].uuid), "inactive": str(people["student"].uuid), "unknown": ZERO_UUID}[who]
    assert _post(admin, f"/view-as/{target}/").status_code == 404
    assert KEY not in admin.session


# ---- Forging it


def test_a_request_cannot_name_a_user_to_become(people):
    staff = _client(people["staff"])
    uuid = people["student"].uuid
    for attempt in (f"/profile/?view_as={uuid}", f"/profile/?{KEY}={uuid}", f"/profile/?user={uuid}"):
        assert "Back to admin" not in _get(staff, attempt).content.decode()
    response = staff.get("/profile/", **{"HTTP_X_VIEW_AS": str(uuid)}, **proxied(HOST))
    assert "Back to admin" not in response.content.decode()
    staff.cookies[KEY] = str(people["student"].pk)
    assert not _viewing(staff)
    assert KEY not in staff.session


@pytest.mark.parametrize("who", ["staff", "student"])
def test_state_planted_in_someone_elses_session_is_ignored_and_removed(people, who):
    """Even a session written directly: `by` naming the admin does not help."""
    client = _client(people[who])
    _plant(client, {"by": people["admin"].pk, "user": people["other"].pk})
    assert not _viewing(client)
    assert people[who].username in _get(client).content.decode()
    assert KEY not in client.session


def test_state_planted_for_themselves_is_ignored_too(people):
    client = _client(people["staff"])
    _plant(client, {"by": people["staff"].pk, "user": people["student"].pk})
    assert not _viewing(client) and KEY not in client.session


@pytest.mark.parametrize("entry", ["anna", 5, ["a"], {}, {"by": None}, {"user": 1}, {"by": "x", "user": "y"}])
def test_malformed_state_is_dropped(people, entry):
    admin = _client(people["admin"])
    _plant(admin, entry)
    assert not _viewing(admin) and _get(admin).status_code == 200
    assert KEY not in admin.session


def test_state_for_another_admin_is_dropped(people):
    admin = _client(people["admin"])
    _plant(admin, {"by": people["admin"].pk + 999, "user": people["student"].pk})
    assert not _viewing(admin) and KEY not in admin.session


def test_true_is_not_a_user_id(people):
    admin = _client(people["admin"])
    _plant(admin, {"by": people["admin"].pk, "user": True})
    assert not _viewing(admin) and KEY not in admin.session


# ---- It ends as soon as it stops being allowed


@pytest.mark.parametrize(
    ("whom", "change"),
    [
        ("admin", {"is_superuser": False}),
        ("admin", {"is_active": False}),
        ("student", {"is_active": False}),
        ("student", {"is_superuser": True}),
    ],
)
def test_it_ends_at_once_when_the_accounts_change(people, whom, change):
    from core.models.user import User

    admin = _client(people["admin"])
    _start(admin, people["student"])
    assert _viewing(admin)
    User.objects.filter(pk=people[whom].pk).update(**change)
    assert not _viewing(admin)


def test_a_new_admin_password_ends_it(people):
    admin = _client(people["admin"])
    _start(admin, people["student"])
    people["admin"].set_password("a-new-admin-password")
    people["admin"].save()
    response = _get(admin)
    assert response.status_code == 302 and "/login/" in response["Location"]


def test_signing_out_ends_it(people):
    admin = _client(people["admin"])
    _start(admin, people["student"])
    assert _post(admin, "/logout/").status_code == 302
    assert KEY not in admin.session
    admin.force_login(people["admin"])
    assert not _viewing(admin)


def test_leaving_gives_the_admin_back(people):
    admin = _client(people["admin"])
    _start(admin, people["student"])
    response = _leave(admin)
    assert (response.status_code, response["Location"]) == (302, f"/staff/users/{people['student'].uuid}/")
    assert not _viewing(admin) and KEY not in admin.session
    assert _get(admin, "/staff/").status_code == 200


def test_starting_and_leaving_change_the_session_key(people):
    admin = _client(people["admin"])
    _get(admin)
    before = admin.session.session_key
    _start(admin, people["student"])
    during = admin.session.session_key
    _leave(admin)
    assert len({before, during, admin.session.session_key}) == 3


def test_leaving_when_not_viewing_does_nothing(people):
    student = _client(people["student"])
    assert _leave(student).status_code == 302
    assert "anna@uni.example" in _get(student).content.decode()


# ---- Read only


def _every_write_route():
    """Every route of the dashboard, top level: a page added later is covered by this."""
    from dashboard import urls
    from django.urls import URLPattern, reverse

    values = {"UUIDConverter": ZERO_UUID, "IntConverter": 1, "StringConverter": "x", "PathConverter": "x"}
    routes = []
    for pattern in urls.urlpatterns:
        if isinstance(pattern, URLPattern) and pattern.name:
            kwargs = {k: values[type(c).__name__] for k, c in pattern.pattern.converters.items()}
            routes.append((pattern.name, reverse(pattern.name, kwargs=kwargs)))
    return routes


def test_every_write_is_refused_except_the_three_allowed(people):
    admin = _client(people["admin"])
    _start(admin, people["student"])
    html = _get(admin).content.decode()
    token = html.split('name="csrfmiddlewaretoken" value="', 1)[1].split('"', 1)[0]
    routes = _every_write_route()
    assert len(routes) > 30
    for name, path in routes:
        if name in ("view_as_stop", "logout", "set_environment") or path.startswith("/api/"):
            continue
        response = admin.post(path, {"csrfmiddlewaretoken": token}, **ORIGIN, **proxied(HOST))
        assert response.status_code == 403, name
        assert b"read only" in response.content, name
    assert _viewing(admin)


@pytest.mark.parametrize("path", ["/admin/logout/", "/admin/login/", "/admin/password_change/"])
def test_djangos_own_admin_pages_are_refused_too(people, path):
    """Django's /admin/logout/ has the same short name as our /logout/: it must not count."""
    admin = _client(people["admin"])
    _start(admin, people["student"])
    response = _post(admin, path)
    assert response.status_code == 403 and b"read only" in response.content
    assert _viewing(admin)


def test_staff_cannot_change_accounts_through_djangos_admin(people):
    from core.models.user import User

    staff = _client(people["staff"])
    target = people["admin"]
    assert _get(staff, "/admin/core/user/").status_code in (403, 404)
    assert _get(staff, f"/admin/core/user/{target.pk}/change/").status_code in (403, 404)
    token = staff.cookies["csrftoken"].value if "csrftoken" in staff.cookies else _get(staff) and staff.cookies["csrftoken"].value
    response = staff.post(f"/admin/core/user/{target.pk}/change/", {"csrfmiddlewaretoken": token, "is_superuser": "on", "is_staff": "on"}, **ORIGIN, **proxied(HOST))
    assert response.status_code in (403, 404)
    assert User.objects.get(pk=people["staff"].pk).is_superuser is False


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_other_writing_methods_are_refused_too(people, method):
    admin = _client(people["admin"])
    _start(admin, people["student"])
    token = admin.cookies["csrftoken"].value
    response = getattr(admin, method)("/profile/", **{"HTTP_X_CSRFTOKEN": token}, **ORIGIN, **proxied(HOST))
    assert response.status_code == 403


def test_nothing_is_done_in_the_users_name(people, monkeypatch):
    from core.models.devbox import Devbox, DevboxSSHKey
    from core.models.team import AccessTokens
    from dashboard import orchestrator_client

    team = people["team"]
    Devbox.objects.create(team=team, index=1)
    version = team.devbox_version
    calls = []
    monkeypatch.setattr(orchestrator_client, "post", lambda path, timeout=15: calls.append(path) or {})
    password = people["student"].password
    admin = _client(people["admin"])
    _start(admin, people["student"])

    assert _post(admin, "/devbox/ssh-keys/add/", {"name": "x", "public_key": SSH_KEY}).status_code == 403
    assert _post(admin, "/devbox/reset/").status_code == 403
    assert _post(admin, "/access-tokens/create/", {"name": "stolen"}).status_code == 403
    assert _post(admin, "/change-password/", {"new_password1": "a-much-better-pw-42", "new_password2": "a-much-better-pw-42"}).status_code == 403

    team.refresh_from_db()
    people["student"].refresh_from_db()
    assert not DevboxSSHKey.objects.filter(team=team).exists() and not AccessTokens.objects.filter(team=team).exists()
    assert team.devbox_version == version and calls == []
    assert people["student"].password == password


def test_the_views_second_start_is_refused(people):
    admin = _client(people["admin"])
    _start(admin, people["student"])
    assert _start(admin, people["other"]).status_code == 403
    assert admin.session[KEY]["user"] == people["student"].pk


def test_the_user_is_not_signed_in_or_touched(people):
    from core.models.user import User

    student_session = _client(people["student"])
    admin = _client(people["admin"])
    login_before = User.objects.get(pk=people["student"].pk).last_login
    _start(admin, people["student"])
    _get(admin)
    _leave(admin)
    assert User.objects.get(pk=people["student"].pk).last_login == login_before
    assert "anna@uni.example" in _get(student_session).content.decode()  # their own session goes on


def test_the_session_stays_the_admins(people):
    admin = _client(people["admin"])
    _start(admin, people["student"])
    assert admin.session["_auth_user_id"] == str(people["admin"].pk)


def test_the_environment_can_still_be_switched(people):
    admin = _client(people["admin"])
    _start(admin, people["student"])
    assert _post(admin, "/environment/", {"environment": "dev"}).status_code == 302
    assert admin.session["is_dev_environment"] is True and _viewing(admin)


# ---- Seeing what they see


def test_staff_pages_are_closed_when_viewing_a_student(people):
    admin = _client(people["admin"])
    _start(admin, people["student"])
    assert _get(admin, "/staff/").status_code == 302
    assert _get(admin, "/staff/users/").status_code == 302
    assert _get(admin, "/topology/").status_code != 200


def test_a_one_time_password_does_not_get_in_the_way(people):
    from core.models.user import User

    User.objects.filter(pk=people["student"].pk).update(must_change_password=True)
    admin = _client(people["admin"])
    _start(admin, people["student"])
    assert _get(admin).status_code == 200


def test_a_staff_user_can_be_viewed_but_not_used(people):
    admin = _client(people["admin"])
    _start(admin, people["staff"])
    assert _get(admin, "/staff/").status_code == 200
    page = _get(admin, "/staff/users/").content.decode()
    assert "View as" not in page.replace("Viewing as", "")  # the view-as buttons are the admin's
    assert _post(admin, "/staff/users/", {"email": "new@uni.example"}).status_code == 403


def test_the_team_api_goes_by_token_and_not_by_the_view(people):
    admin = _client(people["admin"])
    _start(admin, people["student"])
    assert _get(admin, "/api/v1/rounds/").status_code == 401


# ---- The buttons


def test_only_the_admin_is_offered_the_buttons(people):
    admin = _client(people["admin"])
    listing = _get(admin, "/staff/users/").content.decode()
    assert f'action="/view-as/{people["student"].uuid}/"' in listing
    assert f'action="/view-as/{people["staff"].uuid}/"' in listing
    assert f'action="/view-as/{people["admin"].uuid}/"' not in listing
    detail = _get(admin, f"/staff/users/{people['student'].uuid}/").content.decode()
    assert "View as this user" in detail

    staff = _client(people["staff"])
    for path in ("/staff/users/", f"/staff/users/{people['student'].uuid}/"):
        assert "/view-as/" not in _get(staff, path).content.decode()


# ---- The trail


def test_starting_and_leaving_are_logged(people, monkeypatch):
    from dashboard import middleware

    lines = []
    monkeypatch.setattr(middleware.view_as_log, "warning", lambda message, *args: lines.append(message % args))
    admin = _client(people["admin"])
    _start(admin, people["student"])
    _leave(admin)
    assert lines == [
        "view-as started: admin='admin' viewed='anna@uni.example'",
        "view-as stopped: admin='admin' viewed='anna@uni.example'",
    ]
