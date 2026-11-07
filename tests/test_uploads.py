"""Worm uploads end to end on the dashboard side: where the file lands (the
one directory the round agent reads, docs/rounds.md) and what is accepted.

The deploy used to have nothing to fetch: MEDIA_ROOT was unset, so Django
wrote into the service's read-only source tree (and a redeploy's rsync would
have deleted the files), while the agent read /opt/dashboard/media.
"""

from __future__ import annotations

import io
import re
import sys
import zipfile

import pytest
import yaml

from conftest import REPO, as_role
from test_dashboard_login import HOST, login, proxied, site, view  # noqa: F401 (fixture)

pytest.importorskip("django")

sys.path.insert(0, str(REPO / "scripts"))
from round_agent import validation  # noqa: E402

GROUP_VARS = REPO / "ansible" / "group_vars"
DJANGO_APP = "ansible/roles/applications/django_app"
ROUND_AGENT = "ansible/roles/host/round_agent"


def _zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def team_site(site, app_roles):  # noqa: F811
    """The admin in a team, signed in; requests run as the dashboard's role."""
    from core.models.competition import ApplicationSetting
    from core.models.team import Team, TeamMembership
    from core.models.user import User

    team = Team.objects.create(name="red", competition=ApplicationSetting.get_current_competition())
    TeamMembership.objects.create(user=User.objects.get(username="admin"), team=team)
    assert login(site, HOST, f"https://{HOST}").status_code == 302
    view(site, "sandbox")  # these uploads are Live entries; the dashboard opens in Dev
    with as_role("django_dashboard"):
        yield site


def upload(client, data, name="worm.zip"):
    page = client.get("/upload/", **proxied(HOST))
    token = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content).group(1).decode()
    blob = io.BytesIO(data)
    blob.name = name
    client.post("/upload/", {"csrfmiddlewaretoken": token, "artifact": blob, "environment": "live"},
                HTTP_ORIGIN=f"https://{HOST}", **proxied(HOST))  # fmt: skip
    return re.sub(r"\s+", " ", client.get("/upload/", **proxied(HOST)).content.decode())


def test_an_upload_lands_where_the_agent_reads_it(team_site):
    import hashlib

    from django.conf import settings

    from core.models.worms import MalwareArtifact

    data = _zip({"main.sh": "echo worm\n"})
    assert "Uploaded worm.zip" in upload(team_site, data)
    artifact = MalwareArtifact.objects.get()
    digest = hashlib.sha256(data).hexdigest()
    assert artifact.content_hash == digest
    # The key the agent accepts, and the file under MEDIA_ROOT byte for byte.
    assert validation.storage_key(artifact.storage_key) == f"malware-artifacts/{digest[:2]}/{digest[2:4]}/{digest}.zip"
    from pathlib import Path

    assert (Path(settings.MEDIA_ROOT) / artifact.storage_key).read_bytes() == data


def test_a_team_downloads_its_own_uploads_and_nothing_else(team_site):
    import hashlib

    from core.models.team import Team
    from core.models.user import User
    from core.models.worms import MalwareArtifact, Upload

    data = _zip({"main.sh": "echo worm\n"})
    digest = hashlib.sha256(data).hexdigest()
    href = f"/upload/{digest}/download/"
    assert f'href="{href}"' in upload(team_site, data)
    response = team_site.get(href, **proxied(HOST))
    assert response.status_code == 200 and b"".join(response.streaming_content) == data
    assert response["Content-Type"] == "application/zip" and response["Content-Disposition"].startswith("attachment")

    # Another team's upload is not reachable by its hash, nor is anything that is not one.
    blue = Team.objects.create(name="blue", competition=Team.objects.get(name="red").competition)
    other = "e" * 64
    artifact = MalwareArtifact.objects.create(content_hash=other, storage_key=f"malware-artifacts/ee/ee/{other}.zip")
    Upload.objects.create(artifact=artifact, user=User.objects.get(username="admin"), team=blue)
    for path in (f"/upload/{other}/download/", "/upload/x/download/", f"/upload/{digest.upper()}/download/"):
        assert team_site.get(path, **proxied(HOST)).status_code == 404, path


def test_the_same_bytes_again_keep_their_key(team_site):
    """A file already on disk (its records removed since) is reused, never
    renamed by the storage (a renamed key fails the agent's validation)."""
    from core.models.worms import MalwareArtifact, Upload

    data = _zip({"main.sh": "echo 1\n"})
    upload(team_site, data)
    key = MalwareArtifact.objects.get().storage_key
    with as_role("postgres"):
        Upload.objects.all().delete()
        MalwareArtifact.objects.all().delete()
    upload(team_site, data)
    assert MalwareArtifact.objects.get().storage_key == key


@pytest.mark.parametrize("data, message", [
    (b"not a zip", "That is not a .zip archive"),
    (_zip({"worm/main.sh": "x"}), "no main.sh at its top level"),
    (_zip({"README.md": "x"}), "no main.sh at its top level"),
])  # fmt: skip
def test_what_cannot_run_is_refused(team_site, data, message):
    from core.models.worms import Upload

    assert message in upload(team_site, data)
    assert not Upload.objects.exists()


def test_a_team_cannot_store_more_than_its_quota(team_site, monkeypatch):
    """One team cannot fill the dashboard's disk (docs/security-assessment.md, D11):
    what its uploads keep stored is capped, and the same bytes again cost nothing."""
    from django.conf import settings

    from core.models.worms import Upload

    first = _zip({"main.sh": "echo 1\n" + "#" * 4000})
    monkeypatch.setattr(settings, "UPLOAD_TEAM_QUOTA", len(first) + 100)
    assert "Uploaded worm.zip" in upload(team_site, first)
    assert "Uploaded worm.zip" in upload(team_site, first)  # already stored: within the quota
    second = _zip({"main.sh": "echo 2\n" + "#" * 4000})
    assert "the most stored per team" in upload(team_site, second)
    assert Upload.objects.count() == 2


def test_a_team_may_upload_only_so_often(team_site, monkeypatch):
    from django.conf import settings

    from core.models.worms import Upload

    monkeypatch.setattr(settings, "UPLOAD_TEAM_PER_HOUR", 2)
    for n in range(2):
        assert "Uploaded worm.zip" in upload(team_site, _zip({"main.sh": f"echo {n}\n"}))
    assert "uploads in the last hour" in upload(team_site, _zip({"main.sh": "echo 3\n"}))
    assert Upload.objects.count() == 2


# --- one directory for the dashboard and the agent (Ansible) ------------------------


def test_the_dashboard_and_the_agent_share_one_media_root():
    all_vars = yaml.safe_load((GROUP_VARS / "all.yml").read_text())
    dashboard = yaml.safe_load((GROUP_VARS / "dashboard.yml").read_text())
    orchestrator = yaml.safe_load((GROUP_VARS / "orchestrator.yml").read_text())
    agent = yaml.safe_load((REPO / ROUND_AGENT / "defaults" / "main.yml").read_text())
    root = all_vars["dashboard_media_root"]
    assert root == "/opt/dashboard/media"
    assert dashboard["django_media_root"] == "{{ dashboard_media_root }}"
    assert agent["round_agent_dashboard_media_root"] == "{{ dashboard_media_root }}"
    assert not orchestrator.get("django_media_root")
    # Outside the source tree the role rsyncs with --delete.
    src = yaml.safe_load((REPO / DJANGO_APP / "defaults" / "main.yml").read_text())["django_source_dir"]
    assert not root.startswith(src.replace("{{ django_app_name }}", "dashboard") + "/")


DJANGO_ENV = dict(
    django_settings_module="config.settings.dashboard", django_secret_key="k", django_allowed_hosts="h",
    django_db_host="10.11.0.16", django_db_user="django_dashboard", django_db_password="p", postgres_db="mydb",
    django_variant="dashboard",
)  # fmt: skip


def test_the_service_may_write_only_its_media_root(render, tmp_path):
    env = render(DJANGO_APP, "django.env.j2", django_media_root="/opt/dashboard/media", **DJANGO_ENV)
    assert "DJANGO_MEDIA_ROOT=/opt/dashboard/media" in env.splitlines()
    unit = render(DJANGO_APP, "django.service.j2", django_media_root="/opt/dashboard/media", **DJANGO_ENV)
    assert "ProtectSystem=strict" in unit and "ReadWritePaths=/opt/dashboard/media" in unit.splitlines()
    # The orchestrator stores no files: no media root, nothing writable.
    env = render(DJANGO_APP, "django.env.j2", **DJANGO_ENV)
    unit = render(DJANGO_APP, "django.service.j2", **DJANGO_ENV)
    assert "DJANGO_MEDIA_ROOT" not in env and "ReadWritePaths" not in unit
