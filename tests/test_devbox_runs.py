"""An upload in the dev view runs on VM A of the team's devbox as it is, not reset first
(orchestrator/rounds/devbox_runs.py, docs/04-dev-environment.md), and never enters a round.

The page through the dashboard's real URLs and role, the scheduling as the orchestrator's real
role, each with its real grants. The agent's part is in test_round_agent.py.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import timedelta

import pytest

from conftest import REPO, as_role
from test_dashboard_login import HOST, proxied, site  # noqa: F401 (fixture)
from test_dashboard_pages import team_upload  # noqa: F401 (fixture)
from test_profile_page import _dev_view, _devbox

pytest.importorskip("django")

ORIGIN = {"HTTP_ORIGIN": f"https://{HOST}"}
KEY = "malware-artifacts/aa/bb/" + "c" * 64 + ".zip"


def _zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("main.sh", "echo worm\n")
    return buffer.getvalue()


def _page(client):
    return re.sub(r"\s+", " ", client.get("/upload/", **proxied(HOST)).content.decode())


def _post(client, environment):
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', _page(client)).group(1)
    blob = io.BytesIO(_zip())
    blob.name = "worm.zip"
    fields = {"csrfmiddlewaretoken": token, "artifact": blob}
    if environment is not None:
        fields["environment"] = environment
    with as_role("django_dashboard"):
        return client.post("/upload/", fields, **ORIGIN, **proxied(HOST))


def _dev_upload(team):
    from core.models.user import User
    from core.models.worms import MalwareArtifact, Upload

    artifact, _ = MalwareArtifact.objects.get_or_create(content_hash="c" * 64, defaults={"storage_key": KEY})
    return Upload.objects.create(artifact=artifact, user=User.objects.get(username="admin"), team=team, devbox=True)


def _advance(now=None):
    from orchestrator.rounds import devbox_runs

    with as_role("django_orchestrator"):
        devbox_runs.advance(now)


def _run(upload):
    from core.models.devbox import DevboxRun

    return DevboxRun.objects.get(upload=upload)


def _done(job, **result):
    from core.models.rounds import HostJob
    from django.utils import timezone

    HostJob.objects.filter(pk=job.pk).update(state="done", finished_at=timezone.now(), result=result)


def _jobs(kind):
    from core.models.rounds import HostJob

    return list(HostJob.objects.filter(kind=kind).order_by("id"))


def _to_running(upload):
    """Through the deploy, to a worm that has started."""
    from django.utils import timezone

    _advance()
    _done(_run(upload).deploy_job, armed_at=timezone.now().isoformat())
    _advance()
    _advance(timezone.now() + timedelta(seconds=11))
    assert _run(upload).state == "running"


@pytest.fixture
def team(site, team_upload, app_roles):  # noqa: F811
    team = team_upload[0]
    _devbox(team)
    return team


# ---- The page


def test_each_view_warns_about_what_an_upload_does_there(site, team):  # noqa: F811
    live = _page(site)
    assert "This is your team's entry." in live and "switch to the Dev view" in live
    assert "your devbox as it is" not in live and "a sandbox VM" in live
    assert re.search(r'<button type="submit" class="btn btn--primary">Upload</button>', live)

    _dev_view(site)
    dev = _page(site)
    assert "Your upload runs on VM A of your devbox as it is" in dev and "nothing is reset" in dev
    assert "a flag counts only once until you reset your devbox" in dev and "does not enter any round" in dev
    assert "This is your team's entry" not in dev
    assert re.search(r'<button type="submit" class="btn btn--primary">Upload and run</button>', dev)
    assert "VM A of your devbox" in dev


def test_an_upload_in_the_dev_view_is_a_dev_upload(site, team):  # noqa: F811
    from core.models.rounds import HostJob
    from core.models.worms import Upload

    _dev_view(site)
    jobs = HostJob.objects.count()

    assert _post(site, "dev").status_code == 302

    made = Upload.objects.filter(team=team).order_by("-id").first()
    assert made.devbox is True
    # The dashboard only stores it. It cannot ask the agent for anything itself.
    assert HostJob.objects.count() == jobs
    assert "It runs on VM A of your devbox as it is" in _page(site)


def test_an_upload_in_the_live_view_stays_an_entry(site, team):  # noqa: F811
    from core.models.worms import Upload

    assert _post(site, "live").status_code == 302

    made = Upload.objects.filter(team=team).order_by("-id").first()
    assert made.devbox is False
    _advance()
    assert not hasattr(made, "devbox_run") and _jobs("devbox_deploy") == [] and _jobs("devbox_rebuild") == []


def test_each_view_lists_only_its_own_uploads(site, team, team_upload):  # noqa: F811
    from core.models.devbox import DevboxRun

    live = team_upload[1].artifact.content_hash
    dev = _dev_upload(team).artifact.content_hash
    page = _page(site)
    assert live in page and dev not in page and ">Rounds<" in page

    _dev_view(site)
    page = _page(site)
    assert dev in page and live not in page and ">On your devbox<" in page and "Waiting" in page

    DevboxRun.objects.create(upload=team.uploads.get(devbox=True), team=team, state="running")
    assert "Running" in _page(site)


@pytest.mark.parametrize("view, form", [("live", "dev"), ("dev", "live"), ("live", None), ("dev", None), ("dev", "nonsense")])
def test_a_page_from_the_other_view_uploads_nothing(site, team, view, form):  # noqa: F811
    """Live and Dev share a session. A stale tab must not turn a test into a round's entry."""
    from core.models.worms import Upload

    before = Upload.objects.count()
    if view == "dev":
        _dev_view(site)

    assert _post(site, form).status_code == 302

    assert Upload.objects.count() == before
    assert f"You are in the {view.capitalize()} view now" in _page(site)


def test_no_upload_in_the_dev_view_before_the_devbox_is_built(site, team_upload):  # noqa: F811
    from core.models.worms import Upload

    _dev_view(site)
    page = _page(site)
    assert "Your devbox has not been built yet" in page and 'name="artifact"' not in page
    before = Upload.objects.count()
    # A form from before, or a crafted post.
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page).group(1)
    blob = io.BytesIO(_zip())
    blob.name = "worm.zip"
    site.post("/upload/", {"csrfmiddlewaretoken": token, "artifact": blob, "environment": "dev"}, **ORIGIN, **proxied(HOST))
    assert Upload.objects.count() == before


def test_a_failures_reason_stays_with_staff(site, team):  # noqa: F811
    from core.models.rounds import HostJob

    upload = _dev_upload(team)
    _advance()
    HostJob.objects.filter(pk=_run(upload).deploy_job.pk).update(state="failed", error="ERRSECRET ssh debian@10.6.0.11")
    _advance()
    assert _run(upload).state == "failed" and "ERRSECRET" in _run(upload).error

    _dev_view(site)
    page = _page(site)
    assert "Could not be started" in page and "ERRSECRET" not in page and "10.6.0.11" not in page
    # The organiser the team is told to ask sees why, on the team's staff page (until 2026-11-26
    # only the CLI said it).
    from django.test import Client
    from test_dashboard_login import login

    staff = Client()
    login(staff, HOST, f"https://{HOST}")
    page = re.sub(r"\s+", " ", staff.get(f"/staff/teams/{team.uuid}/", **proxied(HOST)).content.decode())
    assert "Why it failed" in page and "ERRSECRET ssh debian@10.6.0.11" in page


# ---- Separate from the rounds


def test_a_dev_upload_never_enters_a_round(site, team, team_upload):  # noqa: F811
    from core.models.competition import Round
    from core.models.rounds import RoundRun
    from django.utils import timezone
    from orchestrator.rounds.planner import plan_round

    entry = team_upload[1]
    newer = _dev_upload(team)
    now = timezone.now()
    with_both = Round.objects.create(competition=team.competition, name="R1", seed=1, round_start=now + timedelta(minutes=1), round_end=now + timedelta(hours=1))

    plan_round(with_both)

    run = RoundRun.objects.get(round=with_both, team=team)
    assert newer.created_at > entry.created_at and run.upload_id == entry.pk

    # Without a live upload, the team has no entry at all.
    entry.delete()
    only_dev = Round.objects.create(competition=team.competition, name="R2", seed=2, round_start=now + timedelta(minutes=1), round_end=now + timedelta(hours=1))
    plan_round(only_dev)
    assert RoundRun.objects.get(round=only_dev, team=team).upload_id is None


def test_everything_that_asks_for_a_teams_entry_leaves_dev_uploads_out():
    views = REPO / "projects" / "dashboard" / "dashboard" / "views"
    assert "devbox=False" in (views / "util.py").read_text()  # the "you have no upload" warning
    assert "filter(devbox=False)" in (views / "staff_teams.py").read_text()  # staff's "latest upload"


def test_a_dev_upload_touches_no_other_team(site, team):  # noqa: F811
    from core.models.team import Team

    other = Team.objects.create(name="blue", competition=team.competition)
    _advance()
    upload = _dev_upload(team)
    _advance()

    other.refresh_from_db()
    assert _run(upload).state == "deploying" and other.devbox_version == 1
    assert _jobs("devbox_deploy")[0].params["team_uuid"] == str(team.uuid) and _jobs("devbox_rebuild") == []


# ---- The run


def test_an_upload_runs_on_vm_a_of_the_devbox_as_it_is(site, team):  # noqa: F811
    from django.utils import timezone

    version = team.devbox_version
    upload = _dev_upload(team)
    assert not hasattr(upload, "devbox_run")

    # No reset (since 2026-11-24): the devbox as it is, at its version, so the score goes on.
    # The upload, to this team's VM A, with what the agent's validator asks for.
    _advance()
    _advance()
    run = _run(upload)
    team.refresh_from_db()
    [deploy] = _jobs("devbox_deploy")
    assert run.state == "deploying" and run.deploy_job == deploy and run.rebuild_job is None
    assert team.devbox_version == version == run.devbox_version and _jobs("devbox_rebuild") == []
    assert deploy.params["start_vm"] == f"devbox-t{team.id}-a" and deploy.params["team_slug"] == f"t{team.id}"
    assert deploy.params["artifact"] == {"storage_key": KEY, "content_hash": "c" * 64}
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    from round_agent import validation

    assert validation.validate("devbox_deploy", deploy.params)["run_duration"] == 3600

    # Dated from when the agent lit the fuse.
    armed = timezone.now() - timedelta(seconds=4)
    _done(deploy, armed_at=armed.isoformat())
    _advance()
    run = _run(upload)
    assert run.state == "armed" and run.armed_at == armed
    assert run.worm_start_at == armed + timedelta(seconds=10)
    assert run.ends_at == run.worm_start_at + timedelta(seconds=3600)

    _advance(run.worm_start_at + timedelta(seconds=1))
    assert _run(upload).state == "running"
    _advance(run.ends_at + timedelta(seconds=1))
    assert _run(upload).state == "finished"

    # Finished is the end: nothing more is asked of the agent.
    _advance(run.ends_at + timedelta(hours=1))
    assert len(_jobs("devbox_rebuild")) == 0 and len(_jobs("devbox_deploy")) == 1


def test_only_the_latest_upload_runs(site, team):  # noqa: F811
    first, second = _dev_upload(team), _dev_upload(team)

    _advance()

    assert _run(first).state == "replaced" and _run(second).state == "deploying"
    assert len(_jobs("devbox_deploy")) == 1 and _jobs("devbox_rebuild") == []


def test_an_upload_during_the_teams_reset_waits_for_it(site, team):  # noqa: F811
    """The team reset its devbox by hand: the upload goes onto the VMs that reset leaves, at its version."""
    from orchestrator.rounds.devbox import request_rebuild

    rebuild, _ = request_rebuild(team)
    upload = _dev_upload(team)
    _advance()
    assert _run(upload).state == "pending" and _jobs("devbox_deploy") == []

    _done(rebuild)
    _advance()
    team.refresh_from_db()
    assert _run(upload).state == "deploying" and _run(upload).devbox_version == team.devbox_version
    assert len(_jobs("devbox_rebuild")) == 1, "the upload asks for no reset of its own"


def test_a_new_upload_replaces_a_running_one(site, team):  # noqa: F811
    first = _dev_upload(team)
    _to_running(first)

    second = _dev_upload(team)
    _advance()

    assert _run(first).state == "replaced" and _run(second).state == "deploying"
    assert len(_jobs("devbox_deploy")) == 2 and _jobs("devbox_rebuild") == []


def test_the_dev_page_shows_the_upload_under_way_until_it_ends(site, team):  # noqa: F811
    from core.models.devbox import DevboxRun

    _dev_view(site)
    assert "data-running" not in _page(site) and "btn--running" not in _page(site)
    upload = _dev_upload(team)
    # Before the orchestrator has picked it up, too: it is about to go onto the devbox.
    for step in ("new", "running"):
        if step == "running":
            _to_running(upload)
        page = _page(site)
        assert "Your current upload is under way" in page and "This cancels your current upload" in page, step
        assert re.search(r'class="btn btn--primary btn--running">Cancel it and run this one</button>', page), step
        assert page.count('class="is-running"') == 1, step
    DevboxRun.objects.filter(upload=upload).update(state="finished")
    page = _page(site)
    assert "data-running" not in page and "btn--running" not in page and "Your upload runs on VM A of your devbox as it is" in page


def test_a_manual_reset_ends_a_running_upload(site, team):  # noqa: F811
    from orchestrator.rounds.devbox import request_rebuild

    upload = _dev_upload(team)
    _to_running(upload)

    request_rebuild(team)
    _advance()

    assert _run(upload).state == "replaced"
    assert len(_jobs("devbox_deploy")) == 1, "it is not deployed again"


@pytest.mark.parametrize("state", ["preparing", "running", "tearing_down"])
def test_a_running_round_goes_first(site, team, state):  # noqa: F811
    """The agent runs one job at a time: a round's timed jobs come first."""
    from core.models.competition import Round
    from core.models.rounds import RoundRun
    from django.utils import timezone

    now = timezone.now()
    rnd = Round.objects.create(competition=team.competition, name="R1", seed=1, round_start=now, round_end=now + timedelta(hours=1))
    run = RoundRun.objects.create(round=rnd, team=team, position=0, seed=1, state=state)
    upload = _dev_upload(team)
    version = team.devbox_version

    _advance()

    team.refresh_from_db()
    assert _run(upload).state == "pending" and _jobs("devbox_deploy") == [] and team.devbox_version == version

    # Once it is over, the upload goes ahead by itself.
    RoundRun.objects.filter(pk=run.pk).update(state="finished")
    _advance()
    assert _run(upload).state == "deploying"


def test_a_round_that_has_not_begun_does_not_hold_it_up(site, team):  # noqa: F811
    from core.models.competition import Round
    from core.models.rounds import RoundRun
    from django.utils import timezone

    now = timezone.now()
    rnd = Round.objects.create(competition=team.competition, name="R1", seed=1, round_start=now + timedelta(hours=1), round_end=now + timedelta(hours=2))
    RoundRun.objects.create(round=rnd, team=team, position=0, seed=1, state="pending")
    upload = _dev_upload(team)

    _advance()

    assert _run(upload).state == "deploying"


def test_a_failed_job_fails_the_run(site, team):  # noqa: F811
    from core.models.rounds import HostJob

    upload = _dev_upload(team)
    _advance()
    HostJob.objects.filter(pk=_run(upload).deploy_job.pk).update(state="failed", error="it broke")

    _advance()

    run = _run(upload)
    assert run.state == "failed" and "it broke" in run.error


def test_a_job_that_stops_reporting_fails_the_run_and_so_does_one_never_taken(site, team):  # noqa: F811
    from core.models.rounds import HostJob
    from django.utils import timezone

    upload = _dev_upload(team)
    _advance()
    later = timezone.now() + timedelta(hours=5)

    # Queued behind other work: still its turn to come.
    _advance(timezone.now() + timedelta(minutes=5))
    assert _run(upload).state == "deploying"

    started = timezone.now()
    HostJob.objects.filter(pk=_run(upload).deploy_job.pk).update(state="running", started_at=started, heartbeat_at=started)
    _advance(started + timedelta(minutes=5))
    assert _run(upload).state == "deploying"
    _advance(later)
    assert _run(upload).state == "failed" and "timed out" in _run(upload).error

    # One the round agent never takes (stopped, paused): after job_timeout off the queue, so that it
    # never runs later, and the run failed with why (until 2026-11-26 it waited for ever).
    upload = _dev_upload(team)
    _advance()
    job = _run(upload).deploy_job
    _advance(timezone.now() + timedelta(hours=1))
    assert _run(upload).state == "failed" and "not taken by the round agent within" in _run(upload).error
    job.refresh_from_db()
    assert job.state == "failed" and job.error.startswith("cancelled: ")
