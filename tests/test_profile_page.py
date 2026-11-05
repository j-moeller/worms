"""What moved between the pages. The profile's Team panel summarises the team's results: tokens
found in the dev view, and VMs captured overall and per round in the live view, in the
leaderboard's own numbers. The other pages took over what used to be elsewhere.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from test_dashboard_login import HOST, login, proxied, site  # noqa: F401 (fixtures)
from test_dashboard_pages import team_upload  # noqa: F401 (fixture)

pytest.importorskip("django")


def _get(site, path) -> str:  # noqa: F811
    response = site.get(path, **proxied(HOST))
    assert response.status_code == 200, path
    return response.content.decode()


def _team_panel(site, query="") -> str:  # noqa: F811
    return _get(site, f"/profile/{query}").split('id="team-title"', 1)[1].split("</section>", 1)[0]


def _dev_view(site):  # noqa: F811
    token = _get(site, "/profile/").split('name="csrfmiddlewaretoken" value="', 1)[1].split('"', 1)[0]
    response = site.post("/environment/", {"csrfmiddlewaretoken": token, "environment": "dev"}, HTTP_ORIGIN=f"https://{HOST}", **proxied(HOST))
    assert response.status_code == 302


def _devbox(team, captured=()):
    from core.models.devbox import Devbox, DevboxCapture, DevboxVM

    devbox = Devbox.objects.create(team=team, index=1, ssh_gateway_host="worms.example", ssh_gateway_port=2222, ssh_gateway_user="team")
    for n, role in enumerate("abc"):
        vm = DevboxVM.objects.create(devbox=devbox, vm_identifier=f"devbox-t1-{role}", role=role, ssh_fabric_ip=f"10.7.1.{11 + n}", addresses=[f"10.128.2.{11 + n}"])
        if role in captured:
            DevboxCapture.objects.create(team=team, devbox_vm=vm, devbox_version=team.devbox_version)
    return devbox


def _round(team, name, start_hours, end_hours):
    from core.models.competition import Round
    from django.utils import timezone

    now = timezone.now()
    return Round.objects.create(
        competition=team.competition, name=name, seed=1,
        round_start=now + timedelta(hours=start_hours), round_end=now + timedelta(hours=end_hours),
    )  # fmt: skip


def _run(round_obj, team, captured, sandbox=8):
    """A run of `team` with `sandbox` VMs, `captured` of them captured."""
    from core.models.rounds import RoundRun
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from core.models.vm import VirtualMachine

    position = RoundRun.objects.filter(round=round_obj).count()
    run = RoundRun.objects.create(round=round_obj, team=team, position=position, seed=1, state="finished")
    for i in range(sandbox):
        vm = VirtualMachine.objects.create(
            run=run, round=round_obj, vm_identifier=f"secret-vm-{i}", vm_maintenance_ip=f"10.4.{position}.{i}", ctf_flag_hash=f"{run.pk}-{i}",
        )  # fmt: skip
        if i < captured:
            attempt = FlagCaptureAttempt.objects.create(team_uuid=str(team.uuid), vm_flag_hash=vm.ctf_flag_hash, processed=True)
            FlagCapture.objects.create(team=team, round=round_obj, vm=vm, flag_capture_attempt=attempt)


def _stat(value, of) -> str:
    return f'<span class="stat__value">{value}<span class="stat__of">/{of}</span></span>'


# ---- The live view


@pytest.fixture
def rounds(site, team_upload):  # noqa: F811
    """red: 3/8 then 1/8. blue: 5/8 then 0/8. One round has not started."""
    from core.models.team import Team

    red = team_upload[0]
    blue = Team.objects.create(name="blue", competition=red.competition)
    first, second = _round(red, "Round 1", -9, -6), _round(red, "Round 2", -5, -2)
    _run(first, red, 3), _run(first, blue, 5), _run(second, red, 1), _run(second, blue, 0)
    return {"first": first, "second": second, "later": _round(red, "Round 3", 2, 5)}


def test_live_overall_adds_up_the_rounds(site, rounds):  # noqa: F811
    panel = _team_panel(site)
    assert _stat(4, 16) in panel and "VMs captured over 2 rounds" in panel
    # Round 1 is blue's (25) over red's (18); round 2 is red's alone (25): as on the leaderboard.
    assert "<dd>1 of 2</dd>" in panel and "<dd>43</dd>" in panel
    assert panel.count('class="pip is-on"') == 4 and panel.count('class="pip"') == 12


def test_live_shows_one_round_on_request(site, rounds):  # noqa: F811
    panel = _team_panel(site, f"?round={rounds['first'].id}")
    assert _stat(3, 8) in panel and "VMs captured in Round 1" in panel
    assert "<dd>2 of 2</dd>" in panel and "<dd>18</dd>" in panel
    assert panel.count('class="pip is-on"') == 3 and panel.count('class="pip"') == 5


def test_live_tabs_offer_overall_and_each_started_round(site, rounds):  # noqa: F811
    panel = _team_panel(site, f"?round={rounds['first'].id}")
    assert '<a class="round-tabs__tab" href="/profile/">Overall</a>' in panel
    assert f'href="/profile/?round={rounds["first"].id}" aria-current="page">Round 1</a>' in panel
    assert f'href="/profile/?round={rounds["second"].id}">Round 2</a>' in panel
    assert "Round 3" not in panel


@pytest.mark.parametrize("query", ["?round=999999", "?round=nonsense"])
def test_live_falls_back_to_overall_for_an_unknown_round(site, rounds, query):  # noqa: F811
    assert _stat(4, 16) in _team_panel(site, query)


def test_live_does_not_show_a_round_that_has_not_started(site, rounds):  # noqa: F811
    assert _stat(4, 16) in _team_panel(site, f"?round={rounds['later'].id}")


def test_live_names_no_vm(site, rounds):  # noqa: F811
    """A team may see only the VMs it captured, and this panel needs none of their names."""
    assert "secret-vm" not in _get(site, "/profile/") and "secret-vm" not in _get(site, f"/profile/?round={rounds['first'].id}")


def test_live_before_any_round(site, team_upload):  # noqa: F811
    panel = _team_panel(site)
    assert "No round has started yet." in panel and "stat__value" not in panel


# ---- The dev view


def test_dev_profile_shows_tokens_found_with_no_rounds_to_choose(site, team_upload):  # noqa: F811
    _devbox(team_upload[0], captured={"b"})
    _dev_view(site)
    panel = _team_panel(site)
    assert _stat(1, 3) in panel and "tokens submitted at this version" in panel and "version 1" in panel
    assert panel.count('class="pip is-on"') == 1 and panel.count('class="pip"') == 2
    assert "round-tabs" not in panel


def test_dev_profile_ignores_a_round_in_the_address(site, team_upload):  # noqa: F811
    _devbox(team_upload[0])
    _dev_view(site)
    assert _stat(0, 3) in _team_panel(site, "?round=1")


def test_dev_profile_without_a_devbox(site, team_upload):  # noqa: F811
    _dev_view(site)
    assert "Your devbox has not been built yet." in _team_panel(site)


def test_the_dev_dashboard_has_vms_first_and_no_score_or_logs(site, team_upload):  # noqa: F811
    _devbox(team_upload[0])
    _dev_view(site)
    page = _get(site, "/")
    assert page.index('id="vms-title"') < page.index('id="connect-title"')
    assert "score-title" not in page and "tokens submitted" not in page and 'class="stat' not in page
    assert "logs-title" not in page
    vms = page.split('id="vms-title"', 1)[1].split("</section>", 1)[0]
    assert "Reset devbox" in vms and 'action="/devbox/reset/"' in vms and "version 1" in vms


def _devbox_log(team, text):
    from core.models.devbox import DevboxLog
    from core.models.logging import LoggingAttempt

    attempt = LoggingAttempt.objects.create(team_uuid=str(team.uuid), devbox=True, log_data={"event": text}, processed=True)
    DevboxLog.objects.create(team=team, devbox_version=team.devbox_version, logging_attempt=attempt)


def test_devbox_logs_are_on_the_progression_page_as_text(site, team_upload):  # noqa: F811
    team = team_upload[0]
    _devbox_log(team, "<script>alert(1)</script>")
    _dev_view(site)
    page = _get(site, "/progression/")
    assert 'id="logs-title"' in page and "version 1" in page
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page


def test_the_progression_page_says_when_there_are_no_devbox_logs(site, team_upload):  # noqa: F811
    _dev_view(site)
    assert "No devbox logs at this version." in _get(site, "/progression/")


def test_the_live_progression_page_has_no_devbox_logs(site, team_upload):  # noqa: F811
    _devbox_log(team_upload[0], "only-for-dev")
    page = _get(site, "/progression/")
    assert "logs-title" not in page and "only-for-dev" not in page


# ---- What moved


def test_the_team_panel_no_longer_links_to_upload_or_progression(site, team_upload):  # noqa: F811
    panel = _team_panel(site)
    assert "red" in panel and "/upload/" not in panel and "/progression/" not in panel


def test_submitting_is_explained_on_upload_and_not_on_the_profile(site, team_upload):  # noqa: F811
    assert "submit-api-title" in _get(site, "/upload/") and "scoring.worms.internal/submit" in _get(site, "/upload/")
    assert "submit-api-title" not in _get(site, "/profile/")


def test_downloading_is_explained_on_progression_and_not_on_the_profile(site, team_upload):  # noqa: F811
    page = _get(site, "/progression/")
    assert "data-api-title" in page and "/api/v1/rounds/" in page and '<a href="/profile/">your profile</a>' in page
    assert "data-api-title" not in _get(site, "/profile/")


def test_downloading_stays_hidden_until_registration_closes(site, team_upload, monkeypatch):  # noqa: F811
    from core.models.competition import Competition

    monkeypatch.setattr(Competition, "is_registration_period", lambda self, *a, **k: True)
    assert "data-api-title" not in _get(site, "/progression/")


def test_the_token_form_starts_closed_even_with_no_tokens(site, team_upload):  # noqa: F811
    page = _get(site, "/profile/")
    assert '<details class="disclosure">' in page and "<details class=\"disclosure\" open" not in page
    assert "Tokens your worm uses" not in page  # submitting needs no token


def test_someone_without_a_team_sees_no_results(site):  # noqa: F811
    assert login(site, HOST, f"https://{HOST}").status_code == 302
    panel = _team_panel(site)
    assert "stat__value" not in panel and "round-tabs" not in panel
