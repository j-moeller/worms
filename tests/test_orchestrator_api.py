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
    from core.models.devbox import Devbox
    from core.models.team import Team

    team = Team.objects.create(name="red", competition=Competition.objects.create(name="WORMS 2026"))
    Devbox.objects.create(team=team, index=1)
    return team


def reset(api, team, token=TOKEN):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    return api.post(f"/devbox/{team.uuid}/reset/", **headers)


def test_devbox_reset_bumps_the_version_and_enqueues_a_rebuild(api, team):
    from core.models.rounds import HostJob

    response = reset(api, team)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["devbox_version"] == 2 and body["rebuild_enqueued"] is True
    job = HostJob.objects.get()
    assert job.kind == HostJob.Kind.DEVBOX_REBUILD
    assert job.params == {"team_uuid": str(team.uuid), "devbox_version": 2}


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
