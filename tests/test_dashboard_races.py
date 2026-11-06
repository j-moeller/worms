"""Concurrent uploads and credential mutations through the real dashboard role."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import threading
import zipfile

import pytest

from conftest import as_role
from test_dashboard_login import site  # noqa: F401
from test_enrollment_concurrency import _clients, _race
from test_staff_admin import csrf, post


def student_clients():
    from core.models.competition import ApplicationSetting
    from core.models.team import Team, TeamMembership
    from core.models.user import User

    comp = ApplicationSetting.get_current_competition()
    clients = _clients()
    teams, users = [], []
    for i, client in enumerate(clients):
        team = Team.objects.create(name=f"race-team-{i}", competition=comp)
        user = User.objects.create(username=f"race-student-{i}")
        TeamMembership.objects.create(team=team, user=user)
        client.force_login(user)
        csrf(client, "/profile/")
        teams.append(team)
        users.append(user)
    return clients, teams, users


def test_simultaneous_identical_uploads_both_succeed(site, app_roles, django_env):  # noqa: F811
    from core.models.worms import MalwareArtifact, Upload
    from django.conf import settings
    from pathlib import Path
    from django.db import connection

    clients, teams, users = student_clients()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("main.sh", "print('concurrency fixture')\n")
    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    key = f"malware-artifacts/{digest[:2]}/{digest[2:4]}/{digest}.zip"
    target = Path(settings.MEDIA_ROOT) / key
    target.parent.mkdir(parents=True)
    target.write_bytes(data)  # a previous upload's file, without a DB row
    def upload(i):
        try:
            blob = io.BytesIO(data)
            blob.name = "worm.zip"
            return post(clients[i], "/upload/", {"artifact": blob, "environment": "live"})
        finally:
            connection.close()

    with as_role("django_dashboard"):
        responses = _race(django_env, "users", [u.pk for u in users], [lambda: upload(0), lambda: upload(1)])
    assert [r.status_code for r in responses] == [302, 302]
    assert MalwareArtifact.objects.count() == 1
    assert set(Upload.objects.values_list("team_id", flat=True)) == {t.pk for t in teams}
    assert target.read_bytes() == data


def test_one_ssh_identity_cannot_be_registered_by_two_teams_at_once(site, app_roles, django_env):  # noqa: F811
    from core.models.devbox import DevboxSSHKey

    clients, teams, _ = student_clients()
    blob = "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eA=="
    with as_role("django_dashboard"):
        responses = _race(django_env, "teams", [t.pk for t in teams], [
            lambda: post(clients[0], "/devbox/ssh-keys/add/", {"public_key": f"ssh-ed25519 {blob} first"}),
            lambda: post(clients[1], "/devbox/ssh-keys/add/", {"public_key": f"ssh-ed25519 {blob} second"}),
        ])
    assert [r.status_code for r in responses] == [302, 302]
    assert DevboxSSHKey.objects.count() == 1


def test_removed_member_cannot_finish_registering_an_ssh_key(site, app_roles, monkeypatch):  # noqa: F811
    from core.models.devbox import DevboxSSHKey
    from dashboard.membership import remove_member
    from dashboard.views import devbox
    from django.db import connection

    clients, teams, users = student_clients()
    entered, release = threading.Event(), threading.Event()
    original = devbox._valid_ssh_key

    def validate(value):
        entered.set()
        assert release.wait(10)
        return original(value)

    monkeypatch.setattr(devbox, "_valid_ssh_key", validate)

    def add():
        try:
            return post(clients[0], "/devbox/ssh-keys/add/", {
                "public_key": "ssh-ed25519 eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eA== laptop",
            })
        finally:
            connection.close()

    with as_role("django_dashboard"), ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(add)
        try:
            assert entered.wait(10)
            remove_member(teams[0], users[0])
        finally:
            release.set()
        assert pending.result(timeout=15).status_code == 302
    assert not DevboxSSHKey.objects.filter(team=teams[0]).exists()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_reupload_repairs_a_missing_or_interrupted_file(site, app_roles, damage):  # noqa: F811
    from core.models.worms import MalwareArtifact, Upload
    from django.conf import settings
    from pathlib import Path
    from test_uploads import _zip

    clients, _, _ = student_clients()
    data = _zip({"main.sh": "print('repair')\n"})

    def upload():
        blob = io.BytesIO(data)
        blob.name = "worm.zip"
        return post(clients[0], "/upload/", {"artifact": blob, "environment": "live"})

    with as_role("django_dashboard"):
        assert upload().status_code == 302
        artifact = MalwareArtifact.objects.get()
        target = Path(settings.MEDIA_ROOT) / artifact.storage_key
        if damage == "missing":
            target.unlink()
        else:
            target.write_bytes(data[:10])
        assert upload().status_code == 302
        assert Upload.objects.count() == 2 and MalwareArtifact.objects.count() == 1
    assert target.read_bytes() == data


def test_interrupted_upload_is_not_published_and_can_be_retried(site, app_roles, monkeypatch):  # noqa: F811
    from core.models.worms import MalwareArtifact, Upload
    from django.conf import settings
    from django.core.files.storage import default_storage
    from pathlib import Path
    from test_uploads import _zip

    clients, _, _ = student_clients()
    data = _zip({"main.sh": "print('disk failure')\n"})
    original = default_storage.save

    def broken_save(name, content, **kwargs):
        partial = io.BytesIO(data[:10])
        original(name, partial, **kwargs)
        raise OSError("simulated disk full")

    def upload():
        blob = io.BytesIO(data)
        blob.name = "worm.zip"
        return post(clients[0], "/upload/", {"artifact": blob, "environment": "live"})

    monkeypatch.setattr(default_storage, "save", broken_save)
    with as_role("django_dashboard"):
        assert upload().status_code == 302
        assert not MalwareArtifact.objects.exists() and not Upload.objects.exists()
        assert not list(Path(settings.MEDIA_ROOT).rglob("*.zip"))
        assert not list(Path(settings.MEDIA_ROOT).rglob("*.pending-*"))
        monkeypatch.setattr(default_storage, "save", original)
        assert upload().status_code == 302
    target = Path(settings.MEDIA_ROOT) / MalwareArtifact.objects.get().storage_key
    assert target.read_bytes() == data


def test_an_alternate_base64_spelling_cannot_share_another_teams_key(site, app_roles):  # noqa: F811
    import base64
    from core.models.devbox import DevboxSSHKey

    clients, teams, users = student_clients()
    canonical = base64.b64encode(b"x" * 40).decode()
    alternative = canonical[:-3] + "B=="
    assert base64.b64decode(canonical) == base64.b64decode(alternative)
    DevboxSSHKey.objects.create(team=teams[1], created_by=users[1], public_key=f"ssh-ed25519 {canonical} other")
    with as_role("django_dashboard"):
        assert post(clients[0], "/devbox/ssh-keys/add/", {"public_key": f"ssh-ed25519 {alternative} mine"}).status_code == 302
    assert not DevboxSSHKey.objects.filter(team=teams[0]).exists()


def test_key_registration_cannot_exceed_the_agents_limit(site, app_roles):  # noqa: F811
    import base64
    from core.models.devbox import DevboxSSHKey

    clients, teams, users = student_clients()
    for i in range(64):
        blob = base64.b64encode(bytes([i]) * 40).decode()
        DevboxSSHKey.objects.create(team=teams[0], created_by=users[0], public_key=f"ssh-ed25519 {blob}")
    extra = base64.b64encode(b"z" * 40).decode()
    with as_role("django_dashboard"):
        assert post(clients[0], "/devbox/ssh-keys/add/", {"public_key": f"ssh-ed25519 {extra} extra"}).status_code == 302
    assert DevboxSSHKey.objects.filter(team=teams[0]).count() == 64


def test_a_short_key_cannot_poison_the_reconfigure_job(site, app_roles):  # noqa: F811
    from core.models.devbox import DevboxSSHKey

    clients, teams, _ = student_clients()
    with as_role("django_dashboard"):
        assert post(clients[0], "/devbox/ssh-keys/add/", {"public_key": "ssh-ed25519 Zg== laptop"}).status_code == 302
    assert not DevboxSSHKey.objects.filter(team=teams[0]).exists()
