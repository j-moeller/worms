"""The redesigned dashboard front end: the templates carry no inline
JavaScript or CSS (everything is in static/), the files they reference exist,
the state-changing endpoints are POST-only, and the core actions work WITHOUT
JavaScript (a plain multipart upload, a themed cookie from a form post).
"""

from __future__ import annotations

import io
import re
import zipfile

import pytest

from conftest import REPO
from test_dashboard_login import HOST, login, proxied, site  # noqa: F401 (fixtures)
from test_dashboard_pages import team_upload  # noqa: F401 (fixture)

pytest.importorskip("django")

TEMPLATES = REPO / "projects" / "dashboard" / "dashboard" / "templates"
STATIC = REPO / "projects" / "dashboard" / "dashboard" / "static"

SCRIPT_OPEN = re.compile(r"<script\b([^>]*)>")
HANDLER = re.compile(r"\son[a-z]+\s*=")
STATIC_REF = re.compile(r"{%\s*static\s*'([^']+)'\s*%}")


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def test_templates_have_no_inline_javascript_or_css():
    """No <style>, no inline event handlers, no style="" attributes, and every
    <script> is an external src (the JS lives in static/)."""
    for path in _templates():
        text = path.read_text()
        where = path.relative_to(TEMPLATES)
        assert "<style" not in text, f"{where}: inline <style>"
        assert 'style="' not in text, f"{where}: inline style attribute"
        assert not HANDLER.search(text), f"{where}: inline on* handler"
        for attrs in SCRIPT_OPEN.findall(text):
            assert "src=" in attrs, f"{where}: inline <script> (no src)"


def test_referenced_static_files_exist():
    refs = set()
    for path in _templates():
        refs.update(STATIC_REF.findall(path.read_text()))
    # The theme stylesheets are referenced dynamically (per request.theme).
    for key in ("lab", "lab-dark"):
        refs.add(f"dashboard/css/themes/{key}.css")
    assert "dashboard/css/base.css" in refs and "dashboard/js/enhance.js" in refs
    for ref in refs:
        assert (STATIC / ref).exists(), f"missing static file: {ref}"


def test_state_changing_endpoints_are_post_only(site):  # noqa: F811
    # A GET (a link, a crawler, an <img>) must not log out or change settings.
    assert site.get("/logout/", **proxied(HOST)).status_code == 405
    assert site.get("/theme/", **proxied(HOST)).status_code == 405


def _csrf(client, path):
    page = client.get(path, **proxied(HOST))
    return re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content).group(1).decode()


def test_theme_is_set_by_a_form_post_cookie(site):  # noqa: F811
    token = _csrf(site, "/login/")
    origin = {"HTTP_ORIGIN": f"https://{HOST}"}
    response = site.post(
        "/theme/", {"csrfmiddlewaretoken": token, "theme": "lab-dark", "next": "/login/"},
        **proxied(HOST), **origin,
    )  # fmt: skip
    assert response.status_code == 302
    assert response.cookies["worms_theme"].value == "lab-dark"
    # An unknown design is ignored (no cookie set).
    bad = site.post(
        "/theme/", {"csrfmiddlewaretoken": token, "theme": "nope", "next": "/login/"},
        **proxied(HOST), **origin,
    )  # fmt: skip
    assert "worms_theme" not in bad.cookies


def _zip_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("main.py", "print('worm')\n")
    return buffer.getvalue()


def test_upload_works_without_javascript(site, team_upload):  # noqa: F811
    """A plain multipart form stores the artifact and redirects with a message
    (no fetch/JS involved)."""
    from core.models.worms import Upload

    before = Upload.objects.count()
    token = _csrf(site, "/upload/")
    data = _zip_bytes()
    upload = io.BytesIO(data); upload.name = "worm.zip"
    response = site.post(
        "/upload/",
        {"csrfmiddlewaretoken": token, "artifact": upload},
        HTTP_ORIGIN=f"https://{HOST}", **proxied(HOST),
    )  # fmt: skip
    assert response.status_code == 302, response.content[:300]
    assert Upload.objects.count() == before + 1


def test_upload_rejects_a_non_zip_without_javascript(site, team_upload):  # noqa: F811
    from core.models.worms import Upload

    before = Upload.objects.count()
    token = _csrf(site, "/upload/")
    blob = io.BytesIO(b"not a zip"); blob.name = "evil.txt"
    response = site.post(
        "/upload/",
        {"csrfmiddlewaretoken": token, "artifact": blob},
        HTTP_ORIGIN=f"https://{HOST}", **proxied(HOST),
    )  # fmt: skip
    assert response.status_code == 302
    assert Upload.objects.count() == before  # nothing stored


def test_environment_toggle_reflects_and_switches_the_session(site, team_upload):  # noqa: F811
    """The environment switch: one sliding toggle that names both environments,
    marks the current one and, when posted, flips the whole page (brand,
    data-env, and the value the no-JS button would submit next). The animation
    and in-place swap are JavaScript-only; the form post is the baseline."""
    page = site.get("/", **proxied(HOST)).content.decode()
    assert "envtoggle" in page and "data-env-toggle" in page
    assert ">Live<" in page and ">Dev<" in page  # both labels live in the track
    # Live is the default; the chrome says so, and the no-JS button submits dev.
    assert 'data-env="sandbox"' in page
    assert 'class="brand__env">LIVE<' in page
    assert re.search(r'class="envtoggle"[^>]*aria-pressed="false"', page)
    assert re.search(r'name="environment"\s+value="dev"', page)

    # Switching is a plain form post (works without JavaScript).
    token = _csrf(site, "/")
    switched = site.post(
        "/environment/", {"csrfmiddlewaretoken": token, "environment": "dev"},
        HTTP_ORIGIN=f"https://{HOST}", **proxied(HOST),
    )  # fmt: skip
    assert switched.status_code == 302
    page = site.get("/", **proxied(HOST)).content.decode()
    assert 'data-env="dev"' in page
    assert 'class="brand__env">DEV<' in page
    assert "WORMS//DEV" in page  # the <title>
    assert re.search(r'class="envtoggle"[^>]*aria-pressed="true"', page)
    assert re.search(r'name="environment"\s+value="sandbox"', page)  # now submits back


def test_staff_tools_are_hidden_from_non_staff(site):  # noqa: F811
    """Todos / Rounds / Topology redirect non-staff to the index, so the
    top-right staff menu is rendered only for staff: views a user can't reach
    are not displayed."""
    from core.models.competition import ApplicationSetting
    from core.models.team import Team, TeamMembership
    from core.models.user import User

    competition = ApplicationSetting.get_current_competition()
    member = User.objects.create(username="player", is_staff=False)
    team = Team.objects.create(name="blue", competition=competition)
    TeamMembership.objects.create(user=member, team=team)

    site.force_login(member)
    page = site.get("/", **proxied(HOST)).content.decode()
    assert "staff-menu" not in page
    for href in ('href="/todos/"', 'href="/rounds/"', 'href="/topology/"'):
        assert href not in page, href
    # And the views themselves are closed to them (hiding matches real access).
    for path in ("/todos/", "/rounds/", "/topology/"):
        assert site.get(path, **proxied(HOST)).status_code == 302, path

    # Staff do see the menu and its links.
    site.force_login(User.objects.get(username="admin"))
    staff_page = site.get("/", **proxied(HOST)).content.decode()
    assert "staff-menu" in staff_page
    for href in ('href="/todos/"', 'href="/rounds/"', 'href="/topology/"'):
        assert href in staff_page, href


def test_brand_is_home_and_leaderboard_access_are_not_nav_links(site, team_upload):  # noqa: F811
    """The brand links home (leaderboard/devbox), Access moved into the profile,
    and the team name is the profile link — none of them are separate nav items."""
    page = site.get("/", **proxied(HOST)).content.decode()
    assert 'class="brand" href="/"' in page            # brand is the home link
    assert ">Leaderboard</a>" not in page              # no separate leaderboard nav link
    assert ">Access</a>" not in page                   # Access folded into the profile
    assert re.search(r'class="site-session__user"[^>]*href="/profile/"', page)


def test_access_tokens_moved_into_the_profile(site, team_upload):  # noqa: F811
    """The standalone Access page is gone; the team's tokens, the create form
    and the API docs now live on the profile page."""
    assert site.get("/access/", **proxied(HOST)).status_code == 404
    page = site.get("/profile/", **proxied(HOST)).content.decode()
    assert "Access tokens" in page
    assert 'action="/access-tokens/create/"' in page   # the create form
    assert "/api/v1/rounds/" in page                   # the API docs moved too


def test_devbox_reset_without_a_devbox_is_an_error(site, team_upload):  # noqa: F811
    """Resetting a devbox you do not have reports an error instead of confirming
    an action that did not happen (and never bumps the version)."""
    team, _ = team_upload
    before = team.devbox_version
    token = _csrf(site, "/")
    response = site.post(
        "/devbox/reset/", {"csrfmiddlewaretoken": token},
        HTTP_ORIGIN=f"https://{HOST}", **proxied(HOST),
    )  # fmt: skip
    assert response.status_code == 302
    team.refresh_from_db()
    assert team.devbox_version == before  # nothing was reset
    assert "no devbox to reset" in site.get("/", **proxied(HOST)).content.decode()


def _post(site, path, data):
    data = {**data, "csrfmiddlewaretoken": _csrf(site, "/")}
    return site.post(path, data, HTTP_ORIGIN=f"https://{HOST}", **proxied(HOST))


def _flash(site):
    return site.get("/", **proxied(HOST)).content.decode()


# A valid key blob to build test keys from (type + base64 + comment).
_BLOB = "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eA=="


def test_devbox_ssh_key_add_remove_and_reconfigure(site, team_upload, monkeypatch):  # noqa: F811
    """Adding or removing a key stores/removes it AND asks the orchestrator to
    re-push keys (no VM rebuild). A key that could be a template, or that
    belongs to another team, is refused."""
    from core.models.devbox import Devbox, DevboxSSHKey
    from core.models.team import Team
    from dashboard import orchestrator_client

    team, _ = team_upload
    Devbox.objects.create(team=team, index=1)
    calls = []
    monkeypatch.setattr(orchestrator_client, "post", lambda path, timeout=15: calls.append(path) or {"reconfigure_enqueued": True})

    # Add a good key -> stored, and a reconfigure is requested.
    assert _post(site, "/devbox/ssh-keys/add/", {"name": "laptop", "public_key": f"ssh-ed25519 {_BLOB} me@laptop"}).status_code == 302
    assert list(DevboxSSHKey.objects.filter(team=team).values_list("public_key", flat=True)) == [f"ssh-ed25519 {_BLOB} me@laptop"]
    assert calls == [f"/devbox/{team.uuid}/reconfigure/"]

    # A template/shell comment is refused (SSTI); nothing stored, no call.
    calls.clear()
    assert _post(site, "/devbox/ssh-keys/add/", {"name": "evil", "public_key": f"ssh-ed25519 {_BLOB} x={{{{7*7}}}}"}).status_code == 302
    assert DevboxSSHKey.objects.filter(team=team).count() == 1 and calls == []
    assert "not allowed" in _flash(site) or "not a single-line" in _flash(site)

    # Another team's key cannot be registered (would hijack confinement).
    other = Team.objects.create(name="blue", competition=team.competition)
    from core.models.user import User

    DevboxSSHKey.objects.create(team=other, name="k", public_key=f"ssh-ed25519 {_BLOB} blue", created_by=User.objects.create(username="b"))
    calls.clear()
    _post(site, "/devbox/ssh-keys/add/", {"name": "dup", "public_key": f"ssh-ed25519 {_BLOB} mine"})
    assert DevboxSSHKey.objects.filter(team=team).count() == 1 and calls == []
    assert "already registered" in _flash(site)

    # Remove the key -> gone, and a reconfigure is requested.
    calls.clear()
    key = DevboxSSHKey.objects.get(team=team)
    assert _post(site, f"/devbox/ssh-keys/{key.id}/delete/", {}).status_code == 302
    assert DevboxSSHKey.objects.filter(team=team).count() == 0
    assert calls == [f"/devbox/{team.uuid}/reconfigure/"]


def test_leaving_a_team_removes_the_members_keys(site, team_upload, monkeypatch):  # noqa: F811
    """A member who leaves no longer has devbox access: their keys are removed
    and the orchestrator re-pushes the remaining ones."""
    from core.models.competition import ApplicationSetting
    from core.models.devbox import Devbox, DevboxSSHKey
    from core.models.user import User
    from dashboard import orchestrator_client
    from django.utils import timezone

    team, _ = team_upload
    Devbox.objects.create(team=team, index=1)
    admin = User.objects.get(username="admin")
    DevboxSSHKey.objects.create(team=team, name="mine", public_key=f"ssh-ed25519 {_BLOB} admin", created_by=admin)
    # Open registration so leaving is allowed.
    comp = ApplicationSetting.get_current_competition()
    comp.registration_starts_at = timezone.now() - timezone.timedelta(days=1)
    comp.registration_ends_at = timezone.now() + timezone.timedelta(days=1)
    comp.save()
    calls = []
    monkeypatch.setattr(orchestrator_client, "post", lambda path, timeout=15: calls.append(path) or {})

    assert _post(site, f"/leave_team/{team.uuid}", {}).status_code == 302
    assert DevboxSSHKey.objects.filter(team=team, created_by=admin).count() == 0
    assert calls == [f"/devbox/{team.uuid}/reconfigure/"]
