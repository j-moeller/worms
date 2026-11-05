"""The orchestrator's REST API (orchestrator/api.py): the dashboard's devbox
reset, the one call the dashboard makes (bearer token, scope "dashboard")."""

from __future__ import annotations

import pytest

pytest.importorskip("django")

TOKEN = "dashboard-test-token"


@pytest.fixture
def api(db_clean):
    from django.test import Client, override_settings

    with override_settings(ROOT_URLCONF="orchestrator.urls", ORCHESTRATOR_TOKENS={"dashboard": TOKEN},
                           ALLOWED_HOSTS=["orchestrator.worms.internal"]):  # fmt: skip
        yield Client(HTTP_HOST="orchestrator.worms.internal")


@pytest.fixture
def team(db_clean):
    from core.models.competition import Competition
    from core.models.devbox import Devbox, DevboxSSHKey
    from core.models.team import Team
    from core.models.user import User

    competition = Competition.objects.create(name="WORMS 2026")
    team = Team.objects.create(name="red", competition=competition)
    Devbox.objects.create(team=team, index=1)
    # A second provisioned team: a rebuild/reconfigure job must carry the whole
    # competition's teams, each with its own keys.
    other = Team.objects.create(name="blue", competition=competition)
    Devbox.objects.create(team=other, index=2)
    user = User.objects.create(username="u")
    import base64

    blob = base64.b64encode(b"x" * 40).decode()
    DevboxSSHKey.objects.create(team=team, name="k", public_key=f"ssh-ed25519 {blob} red@laptop", created_by=user)
    return team


def reset(api, team, token=TOKEN):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    return api.post(f"/devbox/{team.uuid}/reset/", **headers)


def reconfigure(api, team, token=TOKEN):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    return api.post(f"/devbox/{team.uuid}/reconfigure/", **headers)


def test_devbox_reset_bumps_the_version_and_enqueues_a_rebuild(api, team):
    from core.models.rounds import HostJob

    response = reset(api, team)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["devbox_version"] == 2 and body["rebuild_enqueued"] is True
    job = HostJob.objects.get()
    assert job.kind == HostJob.Kind.DEVBOX_REBUILD
    # The job carries the full teams spec (with keys), not just this team, so
    # the agent can regenerate the stack and re-push every team's keys.
    assert job.params["team_uuid"] == str(team.uuid) and job.params["devbox_version"] == 2
    teams = job.params["teams"]
    assert {t["slug"] for t in teams} == {f"t{team.id}", f"t{team.id + 1}"}
    red = next(t for t in teams if t["slug"] == f"t{team.id}")
    assert red["authorized_keys"] and "red@laptop" in red["authorized_keys"][0]


def test_rebuild_params_pass_the_agent_validator(api, team):
    """What the orchestrator enqueues for a reset must be accepted by the round
    agent's validator (the two used to disagree: no `teams` -> every rebuild
    failed and no keys were installed)."""
    import sys

    from conftest import REPO
    from core.models.rounds import HostJob

    sys.path.insert(0, str(REPO / "scripts"))
    from round_agent import validation

    reset(api, team)
    job = HostJob.objects.get()
    clean = validation.validate("devbox_rebuild", job.params)
    assert clean["team_uuid"] == str(team.uuid) and len(clean["teams"]) == 2


def test_devbox_reconfigure_enqueues_a_reconfigure_job(api, team):
    from core.models.rounds import HostJob

    response = reconfigure(api, team)
    assert response.status_code == 200, response.content
    assert response.json()["reconfigure_enqueued"] is True
    job = HostJob.objects.get()
    assert job.kind == HostJob.Kind.DEVBOX_RECONFIGURE
    # No version bump, no rebuild; carries every team's keys.
    team.refresh_from_db()
    assert team.devbox_version == 1
    assert {t["slug"] for t in job.params["teams"]} == {f"t{team.id}", f"t{team.id + 1}"}

    import sys

    from conftest import REPO

    sys.path.insert(0, str(REPO / "scripts"))
    from round_agent import validation

    assert validation.validate("devbox_reconfigure", job.params)["teams"]

    # Coalesced: the same keys again, while they are queued, does not pile up.
    assert reconfigure(api, team).json()["reconfigure_enqueued"] is False
    assert HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_RECONFIGURE).count() == 1


def test_devbox_reconfigure_never_loses_a_key_added_behind_a_job(api, team):
    """A job carries the keys as they were when it was created. Coalescing with
    it regardless told nine users their key was being installed, while the only
    job held two of them. Found by the live end-to-end run."""
    from core.models.devbox import DevboxSSHKey
    from core.models.rounds import HostJob
    from core.models.user import User

    user = User.objects.create(username="keys@example.org", email="keys@example.org")

    import base64
    def key_for(label):
        return "ssh-ed25519 " + base64.b64encode((label * 40).encode()).decode()

    def add_key(label):
        DevboxSSHKey.objects.create(team=team, name=label, public_key=key_for(label), created_by=user)

    def keys_of(job):
        return [key for spec in job.params["teams"] for key in spec["authorized_keys"]]

    add_key("first")
    assert reconfigure(api, team).json()["reconfigure_enqueued"] is True
    add_key("second")  # the first job is still queued, and does not have this key
    assert reconfigure(api, team).json()["reconfigure_enqueued"] is True

    first = HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_RECONFIGURE).order_by("id").first()
    first.state = HostJob.State.RUNNING
    first.save(update_fields=["state"])
    add_key("third")  # a running job does not have it either
    assert reconfigure(api, team).json()["reconfigure_enqueued"] is True

    newest = HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_RECONFIGURE).order_by("-id").first()
    assert {key_for(name) for name in ("first", "second", "third")} <= set(keys_of(newest))


def test_devbox_reconfigure_needs_the_token(api, team):
    for token in (None, "wrong"):
        assert reconfigure(api, team, token=token).status_code == 401


def test_devbox_reset_does_not_pile_up_rebuilds(api, team):
    """A second reset while a rebuild is still queued neither enqueues another
    job nor bumps the version again (docs/security-assessment.md, D2)."""
    from core.models.rounds import HostJob

    assert reset(api, team).json()["rebuild_enqueued"] is True
    team.refresh_from_db()
    version = team.devbox_version

    again = reset(api, team).json()
    assert again["rebuild_enqueued"] is False
    assert again["devbox_version"] == version  # not bumped again
    assert HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_REBUILD).count() == 1

    # Once the rebuild has finished, a reset enqueues a fresh one.
    job = HostJob.objects.get(kind=HostJob.Kind.DEVBOX_REBUILD)
    job.state = HostJob.State.DONE
    job.save(update_fields=["state"])
    assert reset(api, team).json()["rebuild_enqueued"] is True
    assert HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_REBUILD).count() == 2


@pytest.mark.parametrize("token", [None, "wrong", ""])
def test_devbox_reset_needs_the_token(api, team, token):
    from core.models.rounds import HostJob

    assert reset(api, team, token=token).status_code == 401
    assert not HostJob.objects.exists()


def test_devbox_reset_is_post_only_and_for_known_teams(api, team):
    import uuid

    assert api.get(f"/devbox/{team.uuid}/reset/", HTTP_AUTHORIZATION=f"Bearer {TOKEN}").status_code == 405
    unknown = api.post(f"/devbox/{uuid.uuid4()}/reset/", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
    assert unknown.status_code == 404


def test_queued_reconfigure_refreshes_the_keys_after_a_removal(api, team, app_roles):
    from conftest import as_role
    from core.models.devbox import DevboxSSHKey
    from core.models.rounds import HostJob

    with as_role("django_orchestrator"):
        assert reconfigure(api, team).status_code == 200
    DevboxSSHKey.objects.filter(team=team).delete()
    with as_role("django_orchestrator"):
        assert reconfigure(api, team).status_code == 200
    job = HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_RECONFIGURE).order_by("-id").first()
    red = next(t for t in job.params["teams"] if t["slug"] == f"t{team.pk}")
    assert red["authorized_keys"] == []  # never re-install a revoked key


def test_running_reconfigure_gets_a_follow_up_after_a_key_change(api, team, app_roles):
    from conftest import as_role
    from core.models.devbox import DevboxSSHKey
    from core.models.rounds import HostJob

    with as_role("django_orchestrator"):
        reconfigure(api, team)
    job = HostJob.objects.get(kind=HostJob.Kind.DEVBOX_RECONFIGURE)
    job.state = HostJob.State.RUNNING
    job.save(update_fields=["state"])
    DevboxSSHKey.objects.filter(team=team).delete()
    with as_role("django_orchestrator"):
        response = reconfigure(api, team)
    assert response.json()["reconfigure_enqueued"] is True
    next_job = HostJob.objects.get(kind=HostJob.Kind.DEVBOX_RECONFIGURE, state=HostJob.State.QUEUED)
    red = next(t for t in next_job.params["teams"] if t["slug"] == f"t{team.pk}")
    assert red["authorized_keys"] == []


@pytest.mark.parametrize('kind', ['devbox_rebuild', 'devbox_provision'])
def test_key_revocation_must_follow_later_full_stack_jobs(api, team, app_roles, kind):
    from conftest import as_role
    from core.models.devbox import DevboxSSHKey
    from core.models.rounds import HostJob
    from orchestrator.rounds.devbox import provisioned_team_specs

    with as_role('django_orchestrator'):
        assert reconfigure(api, team).status_code == 200
        stale = provisioned_team_specs(team.competition)
        rebuild = HostJob.objects.create(kind=kind,
            params={'team_uuid': str(team.uuid), 'teams': stale})
    DevboxSSHKey.objects.filter(team=team).delete()
    with as_role('django_orchestrator'):
        assert reconfigure(api, team).status_code == 200
    # The last snapshot the agent applies must revoke the key. Updating the
    # earlier reconfigure alone would let the later rebuild restore it.
    last = HostJob.objects.order_by('-id').first()
    assert last.id > rebuild.id
    assert last.kind == HostJob.Kind.DEVBOX_RECONFIGURE
    spec = next(t for t in last.params['teams'] if t['team_uuid'] == str(team.uuid))
    assert spec['authorized_keys'] == []


@pytest.mark.parametrize('producer', ['reset', 'provision'])
def test_full_snapshot_creation_is_ordered_with_key_revocation(api, team, app_roles, django_env, monkeypatch, producer):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from conftest import as_role
    from core.models.rounds import HostJob
    from django.db import connection
    from orchestrator import api as views
    from orchestrator.rounds import devbox

    entered, release = threading.Event(), threading.Event()
    module, name = (views, 'provisioned_team_specs') if producer == 'reset' else (devbox, 'team_spec')
    original = getattr(module, name)

    def pause_snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        if threading.current_thread().name.startswith('full-snapshot') and not entered.is_set():
            entered.set()
            assert release.wait(15)
        return result

    monkeypatch.setattr(module, name, pause_snapshot)

    def full_job():
        try:
            if producer == 'reset':
                assert reset(api, team).status_code == 200
            else:
                assert devbox.provision(team.competition)[1] is not None
        finally:
            connection.close()

    def revoke():
        try:
            assert reconfigure(api, team).status_code == 200
        finally:
            connection.close()

    with as_role('django_orchestrator'), ThreadPoolExecutor(max_workers=1, thread_name_prefix='full-snapshot') as full_pool, ThreadPoolExecutor(max_workers=1) as key_pool:
        pending = full_pool.submit(full_job)
        try:
            assert entered.wait(10)
            admin = django_env.connect()
            try:
                with admin.cursor() as cur:
                    cur.execute('DELETE FROM devbox_ssh_keys WHERE team_id=%s', [team.pk])
                follow_up = key_pool.submit(revoke)
                deadline = time.monotonic() + 10
                while True:
                    with admin.cursor() as cur:
                        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE usename='django_orchestrator' AND wait_event='advisory'")
                        if cur.fetchone()[0]:
                            break
                    assert not follow_up.done(), 'key update passed an unfinished full snapshot'
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
            finally:
                admin.close()
        finally:
            release.set()
        pending.result(timeout=15)
        follow_up.result(timeout=15)
    last = HostJob.objects.order_by('-id').first()
    assert last.kind == HostJob.Kind.DEVBOX_RECONFIGURE
    spec = next(t for t in last.params['teams'] if t['team_uuid'] == str(team.uuid))
    assert spec['authorized_keys'] == []


@pytest.mark.parametrize('producer', ['reset', 'provision'])
def test_identical_old_key_snapshot_cannot_skip_a_later_rebuild(api, team, app_roles, producer):
    from conftest import as_role
    from core.models.devbox import DevboxSSHKey
    from core.models.rounds import HostJob
    from orchestrator.rounds.devbox import provision

    key = DevboxSSHKey.objects.get(team=team)
    fields = {name: getattr(key, name) for name in ('team', 'created_by', 'name', 'public_key')}
    with as_role('django_orchestrator'):
        assert reconfigure(api, team).status_code == 200
    original = HostJob.objects.get()
    key.delete()
    with as_role('django_orchestrator'):
        if producer == 'reset':
            assert reset(api, team).status_code == 200
        else:
            assert provision(team.competition)[1] is not None
    barrier = HostJob.objects.order_by('-id').first()
    DevboxSSHKey.objects.create(**fields)
    with as_role('django_orchestrator'):
        response = reconfigure(api, team)
    latest = HostJob.objects.order_by('-id').first()
    assert response.json()['reconfigure_enqueued'] is True
    assert latest.pk > barrier.pk
    assert latest.params['teams'] == original.params['teams']
