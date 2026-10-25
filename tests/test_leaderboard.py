"""The leaderboard (dashboard/views/leaderboard.py): the overall ranking over
all rounds (the default), a tab per started round showing that round's ranking
by VMs captured, and the 🔥 / ⭐️ badges."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from test_dashboard_login import HOST, proxied, site  # noqa: F401 (fixture)

pytest.importorskip("django")

FLAME = "\U0001f525"   # 🔥 leads the running round
STAR = "⭐️"  # ⭐️ won a round (with the emoji variation selector)


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
    r3 = _round(comp, "R3", now + timedelta(days=1), now + timedelta(days=2))  # not started
    return {"teams": teams, "r1": r1, "r2": r2, "r3": r3}


def _get(site, path="/"):  # noqa: F811
    return site.get(path, **proxied(HOST)).content.decode()


def _tabs(page):
    nav = re.search(r'<nav class="round-tabs"[^>]*>(.*?)</nav>', page, flags=re.S).group(1)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", tab)).strip()
            for tab in re.findall(r"<a [^>]*>(.*?)</a>", nav, flags=re.S)]


def _current_tab(page):
    nav = re.search(r'<nav class="round-tabs"[^>]*>(.*?)</nav>', page, flags=re.S).group(1)
    current = re.search(r'<a [^>]*aria-current="page"[^>]*>(.*?)</a>', nav, flags=re.S)
    if not current:
        return None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", current.group(1))).replace(FLAME, "").strip()


def _rows(page):
    body = re.search(r"<tbody>(.*?)</tbody>", page, flags=re.S).group(1)
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, flags=re.S):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, flags=re.S)]
        rows.append(cells)
    return rows


# --- the tabs ---------------------------------------------------------------------


def test_the_tabs_are_all_plus_every_started_round(site, board):  # noqa: F811
    page = _get(site)
    assert _tabs(page) == ["All", "R1", "R2" + f" {FLAME}"]   # R3 has not started; R2 is running
    assert _current_tab(page) == "All"                        # All is the default


def test_an_unknown_or_unstarted_round_falls_back_to_all(site, board):  # noqa: F811
    for query in (f"?round={board['r3'].id}", "?round=999", "?round=notanumber"):
        page = _get(site, "/" + query)
        assert _current_tab(page) == "All"
        assert ">Ranking</h2>" in page


# --- the overall ("All") leaderboard ----------------------------------------------


def test_the_default_is_the_overall_ranking_with_badges(site, board):  # noqa: F811
    page = _get(site)
    assert ">Ranking</h2>" in page and "Overall ranking across all rounds" in page
    # bees 25, dogs 0+25 (running), ants/cats 18 -> bees, dogs, ants, cats.
    assert _rows(page) == [
        ["01", f"bees {STAR}", "25 +0"],    # won R1, nothing in the running round
        ["", f"dogs {FLAME}", "25 +25"],    # also 25 (shared place), leading the running R2
        ["03", "ants", "18 +0"],
        ["", "cats", "18 +0"],              # also 18 (shared place)
    ]
    # No "running"/"leading" word badges -- only the emoji.
    assert "leading</span>" not in page and ">running<" not in page


def test_no_running_badge_anywhere(site, board):  # noqa: F811
    for path in ("/", f"/?round={board['r1'].id}", f"/?round={board['r2'].id}"):
        page = _get(site, path)
        assert "badge--state" not in page
        assert not re.search(r">\s*running\s*<", page)


# --- a single round's tab ---------------------------------------------------------


def test_a_finished_round_tab_ranks_by_captured_vms_and_stars_the_winner(site, board):  # noqa: F811
    page = _get(site, f"/?round={board['r1'].id}")
    assert _current_tab(page) == "R1"
    assert "Final." in page
    assert _rows(page) == [
        ["01", f"bees {STAR}", "5 / 8", "25"],   # won it
        ["02", "ants", "3 / 8", "18"],
        ["", "cats", "3 / 8", "18"],              # shared place
        ["04", "dogs", "0", "0"],                 # no run, nothing captured
    ]
    assert FLAME not in _rows(page)[0][1]          # finished -> star, not flame


def test_a_running_round_tab_flames_the_leader(site, board):  # noqa: F811
    page = _get(site, f"/?round={board['r2'].id}")
    assert _current_tab(page) == "R2"
    assert "provisional" in page
    assert _rows(page)[0] == ["01", f"dogs {FLAME}", "2 / 6", "25"]
    assert STAR not in page.split("</nav>")[1]     # running -> flame, not star (ignore the tab flame)


# --- scoring regressions ----------------------------------------------------------


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
    # An undated round is not a tab (it has not started).
    assert "undated" not in _tabs(_get(site))
