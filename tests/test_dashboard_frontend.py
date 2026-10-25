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
    for key in ("underground", "lab", "tech"):
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
        "/theme/", {"csrfmiddlewaretoken": token, "theme": "tech", "next": "/login/"},
        **proxied(HOST), **origin,
    )  # fmt: skip
    assert response.status_code == 302
    assert response.cookies["worms_theme"].value == "tech"
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
