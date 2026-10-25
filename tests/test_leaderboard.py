"""The leaderboard (dashboard/views/leaderboard.py): the overall ranking and,
per round, the teams ranked by the VMs they captured in it."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from test_dashboard_login import HOST, proxied, site  # noqa: F401 (fixture)

pytest.importorskip("django")


def _round(competition, name, start, end):
    from core.models.competition import Round

    return Round.objects.create(competition=competition, name=name, seed=1, round_start=start, round_end=end)


def _captures(round_obj, team, count, sandbox=8):
    """A run of `team` in `round_obj` with `sandbox` VMs, `count` of them captured."""
    from core.models.rounds import RoundRun
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from core.models.vm import VirtualMachine

    position = RoundRun.objects.filter(round=round_obj).count()
    run = RoundRun.objects.create(round=round_obj, team=team, position=position, seed=1, state="finished")
    for i in range(sandbox):
        vm = VirtualMachine.objects.create(run=run, round=round_obj, vm_identifier=f"vm{i}",
                                           vm_maintenance_ip=f"10.4.{position}.{i}", ctf_flag_hash=f"{run.pk}-{i}")  # fmt: skip
        if i < count:
            attempt = FlagCaptureAttempt.objects.create(team_uuid=str(team.uuid), vm_flag_hash=vm.ctf_flag_hash, processed=True)
            FlagCapture.objects.create(team=team, round=round_obj, vm=vm, flag_capture_attempt=attempt)
    return run


@pytest.fixture
def board(site):  # noqa: F811
    from django.utils import timezone

    from core.models.competition import ApplicationSetting
    from core.models.team import Team

    comp = ApplicationSetting.get_current_competition()
    now = timezone.now()
    comp.registration_starts_at, comp.registration_ends_at = now - timedelta(days=9), now - timedelta(days=8)
    comp.save()
    teams = {n: Team.objects.create(name=n, competition=comp) for n in ("ants", "bees", "cats", "dogs")}
    r1 = _round(comp, "R1", now - timedelta(days=2), now - timedelta(days=1))
    _captures(r1, teams["bees"], 5)
    _captures(r1, teams["ants"], 3)
    _captures(r1, teams["cats"], 3)
    r2 = _round(comp, "R2", now - timedelta(hours=1), now + timedelta(hours=5))
    _captures(r2, teams["dogs"], 2, sandbox=6)
    _round(comp, "R3", now + timedelta(days=1), now + timedelta(days=2))  # not started: not shown
    return teams


def _rows(page, round_name):
    section = page[page.index(f">{round_name}</h2>"):]
    section = section[: section.index("</section>")]
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", section, flags=re.S)[1:]:
        cells = [re.sub(r"<[^>]+>", " ", c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, flags=re.S)]
        rows.append([re.sub(r"\s+", " ", c).strip() for c in cells])
    return rows


def test_each_round_ranks_the_teams_by_captured_vms(site, board):  # noqa: F811
    page = site.get("/", **proxied(HOST)).content.decode()
    r1 = _rows(page, "R1")
    assert r1 == [
        ["01", "bees", "5 / 8", "25"],
        ["02", "ants", "3 / 8", "18"],
        ["", "cats", "3 / 8", "18"],     # a shared place
        ["04", "dogs", "0", "0"],         # no run, nothing captured
    ]
    r2 = _rows(page, "R2")
    assert r2[0] == ["01", "dogs", "2 / 6", "25"]
    assert "running — provisional" in page and "final" in page
    assert ">R3</h2>" not in page
    # The overall ranking adds the round points up: bees 25, ants/cats 18, dogs 25 (+25 running).
    overall = page[: page.index(">R2</h2>")]
    assert "bees" in overall and "Rounds won" in overall


def test_rounds_won_counts_finished_rounds(board):
    from core.models.competition import Round
    from core.models.team import Team

    from dashboard.views.leaderboard import score_rounds_won

    teams = list(Team.objects.order_by("name"))
    rounds = list(Round.objects.order_by("round_start"))
    score_rounds_won(teams, rounds, [], None)
    won = {t.name: t.total_points for t in teams}
    # R1 is finished (bees won), R2 is running (does not count yet).
    assert won == {"ants": 0, "bees": 1, "cats": 0, "dogs": 0}


def test_a_round_without_dates_does_not_break_the_board(site, board):  # noqa: F811
    from core.models.competition import ApplicationSetting, Round

    Round.objects.create(competition=ApplicationSetting.get_current_competition(), name="undated", seed=1)
    assert site.get("/", **proxied(HOST)).status_code == 200
