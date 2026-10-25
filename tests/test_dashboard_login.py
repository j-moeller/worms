"""Logging in to the dashboard behind nginx (the dashboard's real middleware
and URLs, production security settings, PostgreSQL with the migrations).

Regression for "CSRF verification failed" on the login POST: Django's CSRF
check compares the browser's Origin (https://name:port) with https://<Host>.
nginx used to forward `Host $host`, which drops the port, so a dashboard
reached through a forwarded port (SSH tunnel) refused every login. nginx now
forwards `$http_host` (tests/test_ansible.py checks the template).

Also: after the login the user lands on the page that asked for it (?next=,
same site only), and pages render when the competition has no dates (the
default one `manage.py init` creates) instead of failing with 500.
"""

from __future__ import annotations

import importlib
import re

import pytest

pytest.importorskip("django")

HOST = "dashboard.worms.internal"


@pytest.fixture
def site(db_clean, monkeypatch, tmp_path):
    from conftest import REPO
    from core.models.competition import ApplicationSetting, Competition
    from core.models.user import User
    from django.test import Client, override_settings

    # The dashboard's own middleware list, exactly as deployed.
    monkeypatch.setenv("DB_USER", "x")
    monkeypatch.setenv("DB_PASSWORD", "x")
    dashboard_settings = importlib.import_module("config.settings.dashboard")

    competition = Competition.objects.create(name="WORMS 2026")  # no dates, like `init`
    ApplicationSetting.objects.create(key="current", current_competition=competition)
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    admin.set_password("admin-pw")
    admin.save()

    templates = [{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [str(REPO / "projects" / "dashboard" / "dashboard" / "templates")],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": [
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ]},
    }]  # fmt: skip
    with override_settings(
        ROOT_URLCONF="dashboard.urls",
        MIDDLEWARE=dashboard_settings.MIDDLEWARE,
        TEMPLATES=templates,
        ALLOWED_HOSTS=[HOST, "10.10.0.18"],
        CSRF_TRUSTED_ORIGINS=[f"https://{HOST}"],  # what base.py derives from ALLOWED_HOSTS
        MEDIA_ROOT=str(tmp_path / "media"),  # uploads stay out of the checkout
    ):
        yield Client(enforce_csrf_checks=True)


def proxied(host):
    """What nginx forwards: the Host header and the TLS hint."""
    return {"HTTP_HOST": host, "HTTP_X_FORWARDED_PROTO": "https"}


def login(client, host, origin, next_="/topology/", password="admin-pw"):
    page = client.get(f"/login/?next={next_}", **proxied(host))
    assert page.status_code == 200
    token = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content).group(1).decode()
    return client.post(
        f"/login/?next={next_}", {"username": "admin", "password": password, "csrfmiddlewaretoken": token},
        HTTP_ORIGIN=origin, HTTP_REFERER=f"{origin}/login/", **proxied(host),
    )  # fmt: skip


@pytest.mark.parametrize("host", [HOST, f"{HOST}:8443", "10.10.0.18", "10.10.0.18:9443"])
def test_login_works_on_any_port_when_the_host_keeps_it(site, host):
    response = login(site, host, f"https://{host}")
    assert response.status_code == 302, response.content[:300]
    assert response["Location"] == "/topology/"


def test_host_without_the_port_is_what_broke_the_login(site):
    """nginx's old `Host $host` (port dropped) against a browser on :8443."""
    response = login(site, HOST, f"https://{HOST}:8443")
    assert response.status_code == 403
    assert b"CSRF" in response.content


def test_next_never_leaves_the_site(site):
    for target in ("https://evil.example/", "//evil.example/", "http:/evil.example"):
        response = login(site, HOST, f"https://{HOST}", next_=target)
        assert response.status_code == 302 and response["Location"] == "/profile/", target
        site.logout()


def test_pages_render_without_competition_dates(site):
    """The default competition has no registration or round dates; every
    page's navigation asks whether registration is open."""
    assert login(site, HOST, f"https://{HOST}").status_code == 302
    for path in ("/topology/", "/profile/", "/"):
        response = site.get(path, **proxied(HOST))
        assert response.status_code in (200, 302), (path, response.status_code)


def test_registration_period_without_dates_is_closed(db_clean):
    from core.models.competition import Competition

    assert Competition(name="x").is_registration_period() is False
