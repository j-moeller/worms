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

    # Coalesced: a second reconfigure while one is queued does not pile up.
    assert reconfigure(api, team).json()["reconfigure_enqueued"] is False
    assert HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_RECONFIGURE).count() == 1


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
