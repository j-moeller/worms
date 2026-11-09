"""Staff administration on the dashboard (dashboard/views/staff*.py): the
competition, student accounts, teams and rounds -- through the dashboard's
real URLs and middleware, and with the dashboard's own database role and its
real grants (conftest.app_roles / as_role), so a write the role may not make
fails here as it would in production.

Nothing here builds infrastructure: a round is a record the conductor plans
from, devboxes are provisioned on the console.
"""

from __future__ import annotations

import html
import re
from datetime import timedelta

import pytest

from conftest import REPO, as_role
from test_dashboard_login import HOST, login, proxied, site  # noqa: F401 (fixture)

pytest.importorskip("django")

ORIGIN = {"HTTP_ORIGIN": f"https://{HOST}"}


@pytest.fixture
def staff_site(site, app_roles):  # noqa: F811
    """Signed in as the admin; every request below runs as django_dashboard."""
    assert login(site, HOST, f"https://{HOST}").status_code == 302
    with as_role("django_dashboard"):
        yield site


def csrf(client, path="/profile/"):
    page = client.get(path, **proxied(HOST))
    match = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content)
    return match.group(1).decode()


def post(client, path, data=None, files=None, page="/profile/"):
    payload = {**(data or {}), "csrfmiddlewaretoken": csrf(client, page)}
    if files:
        payload.update(files)
    return client.post(path, payload, **ORIGIN, **proxied(HOST))


def get(client, path):
    return client.get(path, **proxied(HOST))


def text(response):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", response.content.decode())))


def passwords(response):
    return re.findall(r'<td class="secret">([^<]+)</td>', response.content.decode())


def competition():
    from core.models.competition import ApplicationSetting

    return ApplicationSetting.get_current_competition()


# --- access ----------------------------------------------------------------------

STAFF_PAGES = ["/staff/", "/staff/users/", "/staff/teams/", "/staff/rounds/"]


def test_staff_pages_are_for_staff_only(site, app_roles):  # noqa: F811
    from core.models.user import User

    for path in STAFF_PAGES:
        response = get(site, path)
        assert response.status_code == 302 and response["Location"].startswith("/login/"), path
    User.objects.create(username="student")
    site.force_login(User.objects.get(username="student"))
    with as_role("django_dashboard"):
        for path in STAFF_PAGES:
            response = get(site, path)
            assert response.status_code == 302 and response["Location"] == "/", path
        # And no state-changing staff endpoint does anything for them.
        assert post(site, "/staff/teams/", {"name": "sneaky", "max_members": 2}).status_code == 302
    from core.models.team import Team

    assert not Team.objects.filter(name="sneaky").exists()


def test_every_staff_page_renders(staff_site):
    for path in STAFF_PAGES:
        assert get(staff_site, path).status_code == 200, path
    # The old staff round list redirects to the new one.
    assert get(staff_site, "/rounds/")["Location"] == "/staff/rounds/"


# --- the competition -------------------------------------------------------------


def test_registration_window(staff_site):
    from core.models.team import Team

    comp = competition()
    Team.objects.create(name="early", competition=comp)
    bad = post(staff_site, "/staff/", {"name": comp.name, "registration_starts_at": "2030-01-01T10:00"})
    assert bad.status_code == 200 and "Set both registration dates" in text(bad)
    ok = post(staff_site, "/staff/", {
        "name": "WORMS 2030", "registration_starts_at": "2030-01-01T10:00", "registration_ends_at": "2030-01-08T10:00",
    })  # fmt: skip
    assert ok.status_code == 302
    comp.refresh_from_db()
    assert comp.name == "WORMS 2030" and comp.registration_ends_at.day == 8
    # Dev rounds are gone, and so is the page that made their competition.
    assert "Development competition" not in text(get(staff_site, "/staff/"))
    assert post(staff_site, "/staff/competition/development/", page="/staff/").status_code == 404


# --- accounts --------------------------------------------------------------------


def test_create_a_student_shows_the_one_time_password_once(staff_site, site):  # noqa: F811
    from core.models.team import Team
    from core.models.user import User

    team = Team.objects.create(name="red", competition=competition())
    response = post(staff_site, "/staff/users/", {
        "email": "ada@uni.example", "first_name": "Ada", "last_name": "Lovelace", "team": team.pk,
    }, page="/staff/users/")  # fmt: skip
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    [password] = passwords(response)
    user = User.objects.get(username="ada@uni.example")
    assert user.must_change_password and not user.is_staff and user.check_password(password)
    assert team.users.filter(pk=user.pk).exists()
    # Not on any later page.
    assert password not in get(staff_site, f"/staff/users/{user.uuid}/").content.decode()
    # A second account with the same email (any case) is refused.
    again = post(staff_site, "/staff/users/", {"email": "ADA@uni.example"}, page="/staff/users/")
    assert "already exists" in text(again)

    # The student signs in and must set a password -- as the dashboard role,
    # which may only update the password columns.
    staff_site.logout()
    token = csrf(site, "/login/")
    site.post("/login/", {"username": "ada@uni.example", "password": password, "csrfmiddlewaretoken": token},
              **ORIGIN, **proxied(HOST))  # fmt: skip
    assert get(site, "/profile/")["Location"] == "/change-password/"
    new = "A-much-better-passphrase-42"
    response = post(site, "/change-password/", {"new_password1": new, "new_password2": new}, page="/change-password/")
    assert response.status_code == 302 and response["Location"] == "/profile/"
    user.refresh_from_db()
    assert user.check_password(new) and not user.must_change_password


def test_import_from_a_csv(staff_site):
    from core.models.team import Team
    from core.models.user import User

    Team.objects.create(name="Red", competition=competition(), max_members=1)
    User.objects.create(username="old@uni.example")
    csv_text = (
        "Vorname,Nachname,E-Mail,Team\n"
        "Grace,Hopper,grace@uni.example,red\n"
        "Alan,Turing,alan@uni.example,Red\n"      # Red is full by now
        "Old,Timer,old@uni.example,\n"            # exists: unchanged
        "Nobody,,not-an-email,\n"
        "Konrad,Zuse,zuse@uni.example,Blue\n"     # no such team
    )
    response = post(staff_site, "/staff/users/import/", {"csv_text": csv_text}, page="/staff/users/")
    assert response.status_code == 200
    created = dict(zip(re.findall(r'/staff/users/[0-9a-f-]{36}/">([^<]+)</a></th><td class="secret">',
                                  response.content.decode()), passwords(response)))  # fmt: skip
    assert set(created) == {"grace@uni.example", "alan@uni.example", "zuse@uni.example"}
    page = text(response)
    assert "Red is full" in page and "already has an account" in page
    assert "not an email address" in page and "no team named 'Blue'" in page
    assert User.objects.get(username="grace@uni.example").teams.get().name == "Red"
    assert not User.objects.get(username="alan@uni.example").teams.exists()
    assert all(u.must_change_password for u in User.objects.filter(username__in=created))


def test_reset_deactivate_and_who_manages_whom(staff_site, site):  # noqa: F811
    from core import accounts
    from core.models.user import User

    student = accounts.create_account("stu@uni.example").user
    reset = post(staff_site, f"/staff/users/{student.uuid}/password/", page=f"/staff/users/{student.uuid}/")
    [password] = passwords(reset)
    student.refresh_from_db()
    assert student.check_password(password) and student.must_change_password

    assert post(staff_site, f"/staff/users/{student.uuid}/active/", {"active": "0"}).status_code == 302
    student.refresh_from_db()
    assert not student.is_active
    assert post(staff_site, f"/staff/users/{student.uuid}/active/", {"active": "1"}).status_code == 302
    student.refresh_from_db()
    assert student.is_active

    edit = post(staff_site, f"/staff/users/{student.uuid}/", {"first_name": "Stu", "last_name": "Dent", "email": "s@x.example"},
                page=f"/staff/users/{student.uuid}/")  # fmt: skip
    assert edit.status_code == 302
    student.refresh_from_db()
    assert (student.first_name, student.email, student.username) == ("Stu", "s@x.example", "stu@uni.example")

    # Nobody manages the admin here, and a tutor does not manage other staff.
    admin = User.objects.get(username="admin")
    assert post(staff_site, f"/staff/users/{admin.uuid}/password/").status_code == 404
    with as_role("postgres"):  # staff accounts are made on the console
        tutor = User.objects.create(username="tutor", is_staff=True)
        other = User.objects.create(username="tutor2", is_staff=True)
    site.force_login(tutor)
    assert post(site, f"/staff/users/{other.uuid}/password/").status_code == 404
    assert post(site, f"/staff/users/{student.uuid}/active/", {"active": "0"}).status_code == 302


def test_the_dashboard_role_cannot_make_staff(staff_site):
    """Even with direct SQL, the dashboard's role creates student accounts
    only (a trigger) and cannot promote one (no is_staff/is_superuser grant)."""
    from django.db import connection, transaction
    from django.db.utils import ProgrammingError

    from core.models.user import User

    with pytest.raises(ProgrammingError):
        with transaction.atomic():
            User.objects.create(username="sneaky-staff", is_staff=True)
    with pytest.raises(ProgrammingError):
        with transaction.atomic():
            User.objects.create(username="sneaky-root", is_superuser=True)
    User.objects.create(username="plain")
    with pytest.raises(ProgrammingError):
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute("UPDATE users SET is_staff = true WHERE username = 'plain'")


# --- teams -----------------------------------------------------------------------


def test_teams_and_members(staff_site, monkeypatch):
    from core.models.devbox import Devbox, DevboxSSHKey
    from core.models.team import Team
    from core.models.user import User
    from dashboard import orchestrator_client

    calls = []
    monkeypatch.setattr(orchestrator_client, "post", lambda path, timeout=15: calls.append(path) or {})
    response = post(staff_site, "/staff/teams/", {"name": "Neural Net", "max_members": 2}, page="/staff/teams/")
    assert response.status_code == 302
    team = Team.objects.get(name="Neural Net")
    # Creating a team builds no infrastructure: the console provisions devboxes.
    assert calls == [] and not Devbox.objects.filter(team=team).exists()
    assert "worms build devbox" in text(get(staff_site, "/staff/teams/"))
    dup = post(staff_site, "/staff/teams/", {"name": "neural net", "max_members": 2}, page="/staff/teams/")
    assert "already exists" in text(dup)

    a, b, c = (User.objects.create(username=n) for n in ("a", "b", "c"))
    for user in (a, b):
        assert post(staff_site, f"/staff/teams/{team.uuid}/members/", {"user": user.pk}).status_code == 302
    post(staff_site, f"/staff/teams/{team.uuid}/members/", {"user": c.pk})
    assert set(team.users.values_list("username", flat=True)) == {"a", "b"}  # full at 2

    resize = post(staff_site, f"/staff/teams/{team.uuid}/", {"name": "Neural Net", "max_members": 1})
    assert "already has 2 members" in text(resize)
    assert post(staff_site, f"/staff/teams/{team.uuid}/", {"name": "Neuro", "max_members": 3}).status_code == 302
    team.refresh_from_db()
    assert (team.name, team.max_members) == ("Neuro", 3)

    # Removing a member removes the devbox keys they registered and re-pushes the rest.
    with as_role("postgres"):  # devboxes are the orchestrator's
        Devbox.objects.create(team=team, index=7)
    DevboxSSHKey.objects.create(team=team, name="k", public_key="ssh-ed25519 AAAA b@x", created_by=b)
    assert post(staff_site, f"/staff/teams/{team.uuid}/members/{b.uuid}/remove/").status_code == 302
    assert not team.users.filter(pk=b.pk).exists() and not DevboxSSHKey.objects.filter(created_by=b).exists()
    assert calls == [f"/devbox/{team.uuid}/reconfigure/"]

    # A user moved between teams on their page.
    other = Team.objects.create(name="Other", competition=team.competition)
    assert post(staff_site, f"/staff/users/{a.uuid}/team/", {"team": other.pk}).status_code == 302
    assert list(a.teams.all()) == [other]


def test_delete_only_teams_without_history(staff_site):
    from core.models.team import Team, TeamMembership
    from core.models.user import User
    from core.models.worms import MalwareArtifact, Upload

    member = User.objects.create(username="m")
    empty = Team.objects.create(name="empty", competition=competition())
    TeamMembership.objects.create(team=empty, user=member)
    assert post(staff_site, f"/staff/teams/{empty.uuid}/delete/").status_code == 302
    assert not Team.objects.filter(pk=empty.pk).exists() and User.objects.filter(pk=member.pk).exists()

    kept = Team.objects.create(name="kept", competition=competition())
    TeamMembership.objects.create(team=kept, user=member)
    Upload.objects.create(user=member, team=kept,
                          artifact=MalwareArtifact.objects.create(content_hash="c" * 64, storage_key="k"))
    response = post(staff_site, f"/staff/teams/{kept.uuid}/delete/")
    assert response.status_code == 302 and Team.objects.filter(pk=kept.pk).exists()
    assert kept.users.filter(pk=member.pk).exists()  # nothing half-deleted


def test_delete_refused_by_the_database_keeps_everything(staff_site):
    """Something the page does not list still refers to the team: the foreign
    key refuses the delete at once and the memberships stay."""
    from core.models.team import AccessTokens, Team, TeamMembership
    from core.models.user import User
    from dashboard.views.staff import delete_unreferenced

    user = User.objects.create(username="t")
    team = Team.objects.create(name="tokens", competition=competition())
    TeamMembership.objects.create(team=team, user=user)
    AccessTokens.objects.create(name="x", access_token_hash="h" * 64, team=team, created_by=user)
    assert delete_unreferenced(Team, team.pk) is False
    assert Team.objects.filter(pk=team.pk).exists()


# --- rounds ----------------------------------------------------------------------


def round_data(**overrides):
    from core import clock

    start = clock.now() + timedelta(days=1)
    data = {
        "name": "R1",
        "round_start": start.strftime("%Y-%m-%dT%H:%M"),
        "round_end": (start + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M"),
        "seed": "", "gameplan": "", "team_duration": 60,
    }  # fmt: skip
    data.update(overrides)
    return data


def valid_gameplan(name="round_one.py", valid=True):
    """A gameplan the orchestrator lists (it writes the table; the dashboard only reads it)."""
    from core.models.rounds import GameplanFile

    with as_role("postgres"):
        GameplanFile.objects.update_or_create(filename=name, defaults={"valid": valid})


def test_create_a_round(staff_site):
    """A round needs no gameplan to be created: it is shown, and waits for one."""
    from core.models.competition import Round
    from core.models.team import Team

    for name in ("a", "b", "c"):
        Team.objects.create(name=name, competition=competition())
    response = post(staff_site, "/staff/rounds/", round_data(), page="/staff/rounds/")
    assert response.status_code == 302, text(response)[:2000]
    created = Round.objects.get(name="R1")
    assert created.competition == competition() and created.seed >= 0
    assert (created.gameplan, created.team_duration) == ("", 3600)
    page = text(get(staff_site, f"/staff/rounds/{created.uuid}/"))
    assert "Nothing is built until it starts" in page and f"seed {created.seed}" in page
    assert "Choose its gameplan before then" in page and "none yet" in page

    # With a gameplan the orchestrator lists as valid; never one it does not.
    valid_gameplan()
    valid_gameplan("broken.py", valid=False)
    later = clock_window(days=2)
    assert post(staff_site, "/staff/rounds/", round_data(name="R2", gameplan="round_one.py", **later),
                page="/staff/rounds/").status_code == 302  # fmt: skip
    assert Round.objects.get(name="R2").gameplan == "round_one.py"
    refused = post(staff_site, "/staff/rounds/", round_data(name="R3", gameplan="broken.py", **clock_window(days=3)),
                   page="/staff/rounds/")  # fmt: skip
    assert refused.status_code == 200 and "Select a valid choice" in text(refused)
    assert not Round.objects.filter(name="R3").exists()


def clock_window(days):
    from core import clock

    start = clock.now() + timedelta(days=days)
    return {"round_start": start.strftime("%Y-%m-%dT%H:%M"), "round_end": (start + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M")}


@pytest.mark.parametrize("overrides, message", [
    ({"round_start": "2000-01-01T10:00", "round_end": "2000-01-01T12:00"}, "Start the round in the future"),
    ({"team_duration": 0}, "greater than or equal to 1"),
    ({"team_duration": 7 * 24 * 60 + 1}, "less than or equal to 10080"),
    ({"gameplan": "../etc/passwd"}, "Select a valid choice"),
])  # fmt: skip
def test_round_validation(staff_site, overrides, message):
    from core.models.competition import Round

    response = post(staff_site, "/staff/rounds/", round_data(**overrides), page="/staff/rounds/")
    assert response.status_code == 200 and message in text(response)
    assert not Round.objects.exists()


def test_a_round_must_fit_every_team(staff_site):
    from core.models.competition import Round
    from core.models.team import Team

    for i in range(4):
        Team.objects.create(name=f"t{i}", competition=competition())
    # 4 teams x (15 s fuse + 60 min + 2 min drain + 15 min build/teardown) = 5 h 9 min > 5 h.
    from datetime import datetime

    start = datetime.strptime(round_data()["round_start"], "%Y-%m-%dT%H:%M")
    short = round_data(round_end=(start + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M"))
    response = post(staff_site, "/staff/rounds/", short, page="/staff/rounds/")
    assert "4 teams need 5 h 9 min" in text(response) and "room for 3" in text(response)
    assert not Round.objects.exists()
    # Shorter runs fit.
    assert post(staff_site, "/staff/rounds/", {**short, "team_duration": 45}, page="/staff/rounds/").status_code == 302


def test_rounds_do_not_overlap_and_there_are_no_dev_rounds(staff_site):
    from core.models.competition import Round

    assert post(staff_site, "/staff/rounds/", round_data(), page="/staff/rounds/").status_code == 302
    clash = post(staff_site, "/staff/rounds/", round_data(name="R2"), page="/staff/rounds/")
    assert "Overlaps R1" in text(clash)
    # The form has no environment any more: a round asked to be "dev" is an ordinary round.
    page = text(get(staff_site, "/staff/rounds/"))
    assert "Dev round" not in page and 'name="environment"' not in page
    assert post(staff_site, "/staff/rounds/", round_data(name="D1", environment="dev", **clock_window(days=3)),
                page="/staff/rounds/").status_code == 302  # fmt: skip
    d1 = Round.objects.get(name="D1")
    assert not d1.competition.is_dev_competition and d1.team_duration == 3600


def test_edit_and_delete_before_the_runs_exist(staff_site):
    """Edited fully before it starts; its gameplan can still be chosen once its runs are
    planned, until the first attempt starts; then it is read-only and kept."""
    from core.models.competition import Round
    from core.models.rounds import RoundRun
    from core.models.team import Team

    post(staff_site, "/staff/rounds/", round_data(), page="/staff/rounds/")
    r1 = Round.objects.get(name="R1")
    edited = post(staff_site, f"/staff/rounds/{r1.uuid}/", round_data(name="R1b", team_duration=50, seed=42),
                  page=f"/staff/rounds/{r1.uuid}/")  # fmt: skip
    assert edited.status_code == 302, text(edited)[:1500]
    r1.refresh_from_db()
    assert (r1.name, r1.seed, r1.team_duration, r1.gameplan) == ("R1b", 42, 3000, "")

    # Planned, nothing started: the gameplan is added later (the conductor copies it to the
    # runs when it pins it).
    team = Team.objects.create(name="t", competition=competition())
    with as_role("postgres"):  # runs are the conductor's
        run = RoundRun.objects.create(round=r1, team=team, position=0, seed=1, duration=3000)
    valid_gameplan()
    added = post(staff_site, f"/staff/rounds/{r1.uuid}/", round_data(name="R1b", team_duration=50, seed=42,
                 gameplan="round_one.py"), page=f"/staff/rounds/{r1.uuid}/")  # fmt: skip
    assert added.status_code == 302, text(added)[:1500]
    r1.refresh_from_db()
    assert r1.gameplan == "round_one.py"

    # Once an attempt has started it is read-only and kept.
    with as_role("postgres"):
        RoundRun.objects.filter(pk=run.pk).update(state=RoundRun.State.PREPARING)
    assert "can no longer change" in text(get(staff_site, f"/staff/rounds/{r1.uuid}/"))
    post(staff_site, f"/staff/rounds/{r1.uuid}/", round_data(name="nope"), page=f"/staff/rounds/{r1.uuid}/")
    r1.refresh_from_db()
    assert r1.name == "R1b"
    post(staff_site, f"/staff/rounds/{r1.uuid}/delete/")
    assert Round.objects.filter(pk=r1.pk).exists()

    post(staff_site, "/staff/rounds/", round_data(name="R9", round_start=round_data()["round_end"],
                                                    round_end="2099-01-01T00:00"), page="/staff/rounds/")  # fmt: skip
    r9 = Round.objects.get(name="R9")
    assert post(staff_site, f"/staff/rounds/{r9.uuid}/delete/").status_code == 302
    assert not Round.objects.filter(pk=r9.pk).exists()


def test_round_parameters_reach_the_runs_and_the_conductor(db_clean):
    from core.models.competition import Competition, Round
    from core.models.team import Team
    from orchestrator.rounds.planner import plan_round

    comp = Competition.objects.create(name="c")
    Team.objects.create(name="t", competition=comp)
    from django.utils import timezone

    round_obj = Round.objects.create(competition=comp, name="R", seed=3, gameplan="round_one.py", team_duration=900,
                                     round_start=timezone.now(), round_end=timezone.now() + timedelta(hours=1))  # fmt: skip
    [run] = plan_round(round_obj)
    assert (run.gameplan, run.duration, run.gameplan_sha256, run.attempt) == ("round_one.py", 900, "", 0)


def test_the_form_bounds_are_the_gameplans(db_clean):
    """The dashboard and the gameplan framework bound a team's duration the same way."""
    import sys

    from core import schedule

    sys.path.insert(0, str(REPO / "src"))
    from gameplan.definition import LIMITS

    default, low, high = schedule.TEAM_DURATION
    assert (low, high) == LIMITS["round_duration"] and low <= default <= high
