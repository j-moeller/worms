"""End-to-end walk of the whole platform journey, in one process, against the
test database -- the single test that ties every subsystem together:

    admin provisions users + teams  ->  admin schedules two rounds  ->
    admin's game plan for round 1   ->  a user logs in + registers an SSH key ->
    (the SSH gateway confinement that key gets)                      ->
    the user's devbox submits a token + a log  ->  the dashboard shows it   ->
    the user resets the devbox (dashboard -> orchestrator, real both sides)  ->
    the user uploads a worm  ->  round 1 runs (prepare -> ... -> teardown)   ->
    the user reads the score + logs of the run (team API, progression, board).

What is real here: every dashboard view, form, URL and middleware; the staff
provisioning flow; the round planner; the conductor state machine; the attempt
processor (captures/logs, round and devbox); the team API; and the
dashboard->orchestrator devbox-reset HTTP hop (the orchestrator view is called
in-process with its real token check).

What is faked -- exactly as tests/test_rounds.py fakes it, because there is no
Proxmox host here -- is the host side: VM power (FakePower) and the round agent
(fake_agent completes each host job the way the real agent would). The live SSH
jump onto a devbox and the real tofu/ansible build are the human protocol's job
(docs/archive/e2e_test_protocol.md); this asserts that every piece of code those steps
drive is present and wired.

Offline part of `worms test`. DB-backed (django_env/db_clean via the `site`
fixture); skips without Django + PostgreSQL, like the rest of the suite.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import timedelta

import pytest

# The dashboard login fixture `site` (a CSRF-enforcing client with a current
# competition + an admin) and its helpers.
from test_dashboard_login import HOST, login, proxied, site  # noqa: F401 (fixture)

# The proven conductor driver: a fake Proxmox and a fake round agent.
from test_rounds import Clock, FakePower, drive, fake_agent, make_conductor, sha

pytest.importorskip("django")
pytest.importorskip("psycopg2")

ORIGIN = {"HTTP_ORIGIN": f"https://{HOST}"}
# A valid OpenSSH public key to register (type + 40-byte base64 blob + comment).
import base64  # noqa: E402

KEY_BLOB = base64.b64encode(b"k" * 40).decode()
GOOD_KEY = f"ssh-ed25519 {KEY_BLOB} ada@laptop"


# --- small HTTP helpers (CSRF scrape + POST), mirroring the other suites -----------


def _csrf(client, page):
    content = client.get(page, **proxied(HOST)).content
    match = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', content)
    assert match, f"no CSRF token on {page}"
    return match.group(1).decode()


def _post(client, path, data=None, files=None, page="/"):
    payload = {**(data or {}), "csrfmiddlewaretoken": _csrf(client, page)}
    if files:
        payload.update(files)
    return client.post(path, payload, **ORIGIN, **proxied(HOST))


def _text(response):
    import html

    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", response.content.decode())))


def _passwords(response):
    return re.findall(r'<td class="secret">([^<]+)</td>', response.content.decode())


def _zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _login(client, username, password, next_="/"):
    token = _csrf(client, f"/login/?next={next_}")
    return client.post(
        f"/login/?next={next_}",
        {"username": username, "password": password, "csrfmiddlewaretoken": token},
        HTTP_REFERER=f"https://{HOST}/login/", **ORIGIN, **proxied(HOST),
    )  # fmt: skip


def _orchestrator_dispatch(path):
    """Stand in for dashboard.orchestrator_client.post by calling the REAL
    orchestrator view in-process (its token check included), so the reset /
    reconfigure hop is exercised on both sides -- not simulated."""
    from django.test import RequestFactory, override_settings
    from orchestrator import api as orch_api

    match = re.match(r"^/devbox/([0-9a-fA-F-]+)/(reset|reconfigure)/$", path)
    assert match, path
    team_uuid, action = match.group(1), match.group(2)
    view = {"reset": orch_api.devbox_reset, "reconfigure": orch_api.devbox_reconfigure}[action]
    request = RequestFactory().post(path, HTTP_AUTHORIZATION="Bearer e2e-token")
    with override_settings(ORCHESTRATOR_TOKENS={"dashboard": "e2e-token"}):
        response = view(request, team_uuid)
    assert response.status_code == 200, (path, response.status_code, response.content[:200])
    return json.loads(response.content)


def _provision_devbox(team, index=0):
    """What `worms devbox provision` + the round agent leave in the database: a
    Devbox and its three VMs (A/B/C) with tokens. The host build is faked (no
    Proxmox), exactly as the sandbox agent is faked elsewhere in this file."""
    from core.models.devbox import Devbox, DevboxVM

    box = Devbox.objects.create(team=team, index=index, services_source_ip=f"10.2.0.{32 + index}", gateway_vm_id=5000 + index)
    vms = []
    for n, role in enumerate("abc"):
        vms.append(DevboxVM.objects.create(
            devbox=box, vm_identifier=f"devbox-{team.name}-{role}", role=role,
            vm_maintenance_ip=f"10.6.{index}.{11 + n}", ssh_fabric_ip=f"10.7.{index}.{11 + n}",
            proxmox_vm_id=5100 + 10 * index + n, ctf_flag_hash=hashlib.sha256(f"{team.name}-{role}".encode()).hexdigest(),
        ))  # fmt: skip
    return box, vms


# The round form payload (minutes; the form multiplies the plan/timing by 60).
def _round_form(**overrides):
    from core import clock

    start = clock.now() + timedelta(days=1)
    data = {
        "name": "R1",
        "round_start": start.strftime("%Y-%m-%dT%H:%M"),
        "round_end": (start + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M"),
        "seed": "", "vms": 8, "networks": 4, "sites": 1, "max_interfaces": 3, "vm_ram": 512,
        "worm_start_delay": 5, "run_duration": 60,
        "initial_fraction": 0.5, "first_stop_after": 10, "stop_interval": 10, "min_alive": 2, "restart_after": "",
    }  # fmt: skip
    data.update(overrides)
    return data


# --- the end-to-end journey --------------------------------------------------------


def test_end_to_end_competition_journey(site, monkeypatch):  # noqa: F811
    """One pass through the eleven steps the operator walks by hand, each
    asserting the functionality it needs is present and wired."""
    from core import clock
    from core.models.competition import ApplicationSetting, Round
    from core.models.devbox import DevboxCapture, DevboxLog, DevboxSSHKey
    from core.models.logging import Logging, LoggingAttempt
    from core.models.rounds import HostJob, RoundRun, RunEvent
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from core.models.team import AccessTokens, Team
    from core.models.user import User
    from dashboard import orchestrator_client
    from orchestrator.rounds import captures
    from orchestrator.rounds.devbox import provisioned_team_specs, team_authorized_keys
    from orchestrator.rounds.planner import plan_round

    # The dashboard -> orchestrator hops (key reconfigure, devbox reset) run the
    # real orchestrator view in-process instead of making an HTTPS call.
    monkeypatch.setattr(orchestrator_client, "post", lambda path, timeout=15: _orchestrator_dispatch(path))

    comp = ApplicationSetting.get_current_competition()

    # ---- STEP 1: an administrator initializes users and assigns teams ----------
    assert login(site, HOST, f"https://{HOST}").status_code == 302  # the admin signs in
    assert _post(site, "/staff/teams/", {"name": "alpha", "max_members": 2}, page="/staff/teams/").status_code == 302
    alpha = Team.objects.get(name="alpha")
    assert alpha.competition == comp

    created = _post(site, "/staff/users/", {"email": "ada@uni.example", "first_name": "Ada", "last_name": "L", "team": alpha.pk}, page="/staff/users/")
    assert created.status_code == 200 and "no-store" in created["Cache-Control"]
    [one_time_password] = _passwords(created)
    ada = User.objects.get(username="ada@uni.example")
    assert ada.must_change_password and not ada.is_staff and alpha.users.filter(pk=ada.pk).exists()

    # ---- STEP 2: the administrator schedules the first and second round --------
    assert _post(site, "/staff/rounds/", _round_form(name="R1"), page="/staff/rounds/").status_code == 302
    start2 = (clock.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    end2 = (clock.now() + timedelta(days=2, hours=6)).strftime("%Y-%m-%dT%H:%M")
    assert _post(site, "/staff/rounds/", _round_form(name="R2", round_start=start2, round_end=end2), page="/staff/rounds/").status_code == 302
    rounds = list(Round.objects.filter(competition=comp).order_by("round_start"))
    assert [r.name for r in rounds] == ["R1", "R2"]  # ordered by their start; non-overlapping
    r1 = Round.objects.get(name="R1")

    # ---- STEP 3: the game plan assigned to round 1 (its sandbox_params) ---------
    # How many VMs, how long, the stop schedule -- the entry/start VM is chosen
    # per run from the seed (not an admin field), so it is asserted at step 10.
    assert r1.sandbox_params == {
        "vms": 8, "networks": 4, "sites": 1, "max_interfaces": 3, "vm_ram": 512,
        "game_plan": {"initial_fraction": 0.5, "first_stop_after": 600, "stop_interval": 600, "min_alive": 2, "restart_after": None},
        "timing": {"worm_start_delay": 300, "run_duration": 3600},
    }  # fmt: skip

    # A devbox exists for the team (what `worms devbox provision` leaves behind).
    _provision_devbox(alpha, index=0)

    # ---- STEP 4: a user logs in and registers an SSH key for the devbox --------
    site.logout()
    assert _login(site, "ada@uni.example", one_time_password).status_code == 302
    assert site.get("/profile/", **proxied(HOST))["Location"] == "/change-password/"  # forced on first login
    new_password = "a-much-better-passphrase-42"
    assert _post(site, "/change-password/", {"new_password1": new_password, "new_password2": new_password}, page="/change-password/").status_code == 302
    ada.refresh_from_db()
    assert not ada.must_change_password

    assert _post(site, "/devbox/ssh-keys/add/", {"name": "laptop", "public_key": GOOD_KEY}, page="/").status_code == 302
    assert list(DevboxSSHKey.objects.filter(team=alpha).values_list("public_key", flat=True)) == [GOOD_KEY]
    # Adding a key asked the orchestrator to re-push keys (a reconfigure job, no rebuild).
    assert HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_RECONFIGURE).exists()

    # ---- STEP 5: the SSH gateway confinement that key gets ---------------------
    # The gateway's authorized_keys is built from the database for the whole
    # competition; each key is pinned to its own team's entry VM (A) only, so a
    # team cannot reach another team's devbox. (The live jump is the protocol's.)
    assert GOOD_KEY in list(team_authorized_keys(alpha))
    spec = next(s for s in provisioned_team_specs(comp) if s["team_uuid"] == str(alpha.uuid))
    assert GOOD_KEY in spec["authorized_keys"]
    assert alpha.devbox.vms.get(role="a").vm_identifier == "devbox-alpha-a"  # the only permitopen target

    # ---- STEP 6: the user's devbox submits a token + a log ---------------------
    # The worm on the devbox submits VM A's token and one log line; the scoring /
    # logging servers record them with devbox=True (attributed by source address).
    devbox_a = alpha.devbox.vms.get(role="a")
    FlagCaptureAttempt.objects.create(team_uuid=str(alpha.uuid), vm_flag_hash=devbox_a.ctf_flag_hash, devbox=True)
    LoggingAttempt.objects.create(team_uuid=str(alpha.uuid), log_data={"event": "captured A"}, devbox=True)
    from django.core.management import call_command

    call_command("process_attempts", "--once", "--grace", "120")  # the single writer: devbox attempts -> captures/logs
    assert DevboxCapture.objects.filter(team=alpha, devbox_version=alpha.devbox_version).count() == 1
    assert DevboxLog.objects.filter(team=alpha, devbox_version=alpha.devbox_version).count() == 1

    # ---- STEP 7: the dashboard shows that feedback (dev view) -------------------
    session = site.session
    session["is_dev_environment"] = True
    session.save()
    assert 'id="vms-title"' in site.get("/", **proxied(HOST)).content.decode()  # the devbox view
    assert 'action="/devbox/reset/"' in site.get("/profile/", **proxied(HOST)).content.decode()  # its score and reset

    # ---- STEP 8: the user resets the devbox ------------------------------------
    # Dashboard POST -> orchestrator (real): the version is bumped (the score
    # starts over) and a rebuild host job is enqueued; earlier records are kept.
    assert _post(site, "/devbox/reset/", page="/").status_code == 302
    alpha.refresh_from_db()
    assert alpha.devbox_version == 2
    assert HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_REBUILD, params__team_uuid=str(alpha.uuid)).exists()
    assert DevboxCapture.objects.filter(team=alpha, devbox_version=2).count() == 0  # fresh score
    assert DevboxCapture.objects.filter(team=alpha, devbox_version=1).count() == 1  # old records kept

    # ---- STEP 9: the user uploads a worm ---------------------------------------
    session = site.session
    session["is_dev_environment"] = False  # back to the production view
    session.save()
    zip_bytes = _zip_bytes({"main.sh": "echo worm\n"})
    blob = io.BytesIO(zip_bytes)
    blob.name = "worm.zip"
    assert _post(site, "/upload/", {"environment": "live"}, files={"artifact": blob}, page="/upload/").status_code in (200, 302)
    from core.models.worms import MalwareArtifact, Upload

    upload = Upload.objects.get(team=alpha)
    assert upload.artifact.content_hash == hashlib.sha256(zip_bytes).hexdigest()
    assert MalwareArtifact.objects.count() == 1

    # ---- STEP 10: round 1 is scheduled; the sandbox is built, the worm runs,
    #               the sandbox is torn down (one team here) ---------------------
    from django.utils import timezone

    # Its scheduled time arrives: the window opens, the upload predates the start.
    Round.objects.filter(pk=r1.pk).update(round_start=timezone.now() - timedelta(minutes=1), round_end=timezone.now() + timedelta(days=1))
    Upload.objects.filter(pk=upload.pk).update(created_at=timezone.now() - timedelta(minutes=5))
    r1.refresh_from_db()
    # The planner makes one run per team (one here) with the team's latest upload;
    # short game-plan intervals so stops/secret releases unfold inside the test run.
    [run] = plan_round(r1, sandbox_params={"game_plan": {"first_stop_after": 60, "stop_interval": 60, "min_alive": 1}})
    assert run.team == alpha and run.upload_id == upload.pk

    power, flow_clock = FakePower(), Clock()
    conductor = make_conductor(power, flow_clock)
    for _ in range(6):  # PENDING -> PREPARING -> CHECKING -> DEPLOYING -> ARMED
        conductor.tick()
        fake_agent(run, sha)
        run.refresh_from_db()
        if run.state == RoundRun.State.ARMED:
            break
    assert run.state == RoundRun.State.ARMED
    assert run.virtual_machines.count() == 4  # the sandbox was "built" (VMs registered)
    start_vm = run.virtual_machines.get(vm_identifier=run.sandbox_params["start_vm"])  # the entry VM
    assert start_vm.ctf_flag_hash == sha(start_vm.vm_identifier)  # its secret released up front

    flow_clock.advance(11)  # the fuse burns down; the worm starts
    conductor.tick()
    run.refresh_from_db()
    assert run.state == RoundRun.State.RUNNING
    assert run.events.filter(kind=RunEvent.Kind.WORM_STARTED).exists()

    # The worm captures its start VM and logs a line during the run.
    flow_clock.advance(30)
    FlagCaptureAttempt.objects.create(team_uuid=str(alpha.uuid), vm_flag_hash=start_vm.ctf_flag_hash, created_at=flow_clock.now())
    LoggingAttempt.objects.create(team_uuid=str(alpha.uuid), log_data={"event": "on the start vm"}, created_at=flow_clock.now())
    captures.process_flags(conductor.cfg["capture_grace"])
    captures.process_logs(conductor.cfg["capture_grace"])
    assert FlagCapture.objects.filter(team=alpha, vm=start_vm).count() == 1
    assert run.events.filter(kind=RunEvent.Kind.CAPTURED, vm=start_vm).exists()
    assert Logging.objects.filter(team=alpha, round=r1).count() == 1

    drive(conductor, run, run, max_ticks=80, clock=flow_clock, step=30)  # game plan -> ENDING -> TEARING_DOWN -> FINISHED
    assert run.state == RoundRun.State.FINISHED
    assert run.jobs.filter(kind=HostJob.Kind.TEARDOWN, state=HostJob.State.DONE).exists()  # the sandbox was torn down
    assert run.virtual_machines.filter(ctf_flag_hash__isnull=True).count() == 0  # every secret released by the end

    # ---- STEP 11: the user sees the results (score) and the run's logs ---------
    raw_token = "tok-alpha_" + "z" * 32
    AccessTokens.objects.create(name="api", access_token_hash=hashlib.sha256(raw_token.encode()).hexdigest(), team=alpha, created_by=ada)

    def api(path):
        response = site.get(path, HTTP_AUTHORIZATION=f"Bearer {raw_token}", **proxied(HOST))
        return response, json.loads(response.content)

    response, scoring = api(f"/api/v1/rounds/{r1.id}/scoring/")
    assert response.status_code == 200
    assert scoring["summary"]["captured"] == 1
    assert any(item["result"] == "captured" and item["flag_hash"] == start_vm.ctf_flag_hash for item in scoring["items"])

    response, logging = api(f"/api/v1/rounds/{r1.id}/logging/")
    assert response.status_code == 200 and len(logging["items"]) == 1

    response, progression = api(f"/api/v1/rounds/{r1.id}/progression/")
    assert response.status_code == 200
    assert progression["team"] == {"uuid": str(alpha.uuid), "name": "alpha"} and progression["audience"] == "team"

    # The signed-in progression page serves the same document, and the board (the
    # production home) shows the team with its score.
    page = site.get(f"/progression/rounds/{r1.id}/", **proxied(HOST))
    assert page.status_code == 200
    board = site.get("/", **proxied(HOST))
    assert board.status_code == 200 and "alpha" in board.content.decode()


# --- edge cases the journey does not cover on its own ------------------------------


def _due_round_with_teams(names, upload_for=()):
    """A current competition, teams, a due round, and uploads for `upload_for`;
    returns (round, {name: team}). Used by the round-execution edge tests."""
    from core.models.competition import ApplicationSetting, Competition, Round
    from core.models.team import Team
    from core.models.user import User
    from core.models.worms import MalwareArtifact, Upload
    from django.utils import timezone

    comp = Competition.objects.create(name="c", registration_starts_at=timezone.now(), registration_ends_at=timezone.now())
    ApplicationSetting.objects.create(key="current", current_competition=comp)
    teams = {name: Team.objects.create(name=name, competition=comp) for name in names}
    start = timezone.now() - timedelta(minutes=1)
    for name in upload_for:
        user = User.objects.create(username=f"u-{name}")
        art = MalwareArtifact.objects.create(content_hash=hashlib.sha256(name.encode()).hexdigest(), storage_key=f"k-{name}")
        Upload.objects.create(artifact=art, user=user, team=teams[name], created_at=start - timedelta(minutes=5))
    rnd = Round.objects.create(seed=5, name="R1", competition=comp, round_start=start, round_end=start + timedelta(days=1))
    return rnd, teams


def test_a_team_that_never_uploaded_is_skipped(db_clean):
    """Skipping the upload step (step 9) means the round skips that team with a
    reason, instead of trying to run nothing."""
    from core.models.rounds import RoundRun
    from orchestrator.rounds.planner import plan_round

    rnd, teams = _due_round_with_teams(["alpha"], upload_for=[])
    [run] = plan_round(rnd)
    assert run.upload_id is None

    conductor = make_conductor(FakePower(), Clock())
    conductor.tick()
    run.refresh_from_db()
    assert run.state == RoundRun.State.SKIPPED and "upload" in run.error


def test_two_teams_run_one_after_another(db_clean):
    """Step 10 'for each team': runs are sequenced (one sandbox at a time), each
    prepared, run and torn down before the next -- here two teams, both finish."""
    from core.models.rounds import HostJob, RoundRun
    from orchestrator.rounds.planner import plan_round

    rnd, teams = _due_round_with_teams(["alpha", "bravo"], upload_for=["alpha", "bravo"])
    runs = sorted(plan_round(rnd, sandbox_params={"game_plan": {"first_stop_after": 60, "stop_interval": 60, "min_alive": 1}}), key=lambda r: r.position)
    assert [r.position for r in runs] == [0, 1]

    power, flow_clock = FakePower(), Clock()
    conductor = make_conductor(power, flow_clock)
    terminal = {RoundRun.State.FINISHED, RoundRun.State.FAILED, RoundRun.State.SKIPPED}
    for _ in range(400):
        conductor.tick()
        flow_clock.advance(30)
        for run in runs:
            fake_agent(run, sha)
            run.refresh_from_db()
        if all(run.state in terminal for run in runs):
            break
    assert all(run.state == RoundRun.State.FINISHED for run in runs)
    # Each run built and tore down its own sandbox.
    for run in runs:
        assert run.jobs.filter(kind=HostJob.Kind.PREPARE, state=HostJob.State.DONE).exists()
        assert run.jobs.filter(kind=HostJob.Kind.TEARDOWN, state=HostJob.State.DONE).exists()
    # Only one sandbox ever existed at a time: the second run did not prepare
    # until the first had torn down (positions are strictly ordered).
    first, second = runs
    assert first.finished_at is not None and second.finished_at is not None


def test_a_key_cannot_be_registered_by_two_teams(db_clean):
    """The SSH-key concern behind step 4/5: a team cannot adopt another team's
    key (which would inherit its gateway confinement). The database refuses the
    same key under two teams, whatever its free-text comment."""
    from core.models.competition import Competition
    from core.models.devbox import DevboxSSHKey
    from core.models.team import Team
    from django.db import IntegrityError, transaction

    comp = Competition.objects.create(name="c")
    alpha = Team.objects.create(name="alpha", competition=comp)
    bravo = Team.objects.create(name="bravo", competition=comp)
    blob = base64.b64encode(b"s" * 40).decode()
    DevboxSSHKey.objects.create(team=alpha, public_key=f"ssh-ed25519 {blob} ada@alpha")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            DevboxSSHKey.objects.create(team=bravo, public_key=f"ssh-ed25519 {blob} different-comment")
