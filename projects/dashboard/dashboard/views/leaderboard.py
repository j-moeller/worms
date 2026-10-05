from collections import defaultdict
from typing import Callable

from core import clock
from core.models.competition import Competition, Round
from core.models.scoring import FlagCapture
from core.models.team import Team
from dashboard.views.decorators import require_competition
from dashboard.views.util import (
    get_current_round,
    get_flag_captures,
    get_rounds,
    get_teams,
    render_with_base_info,
)
from django.http import HttpRequest

SCORING_METHOD = "formula_one"

SCORING_METHOD_FLAG_CAPTURES = "flag_captures"
SCORING_METHOD_ROUNDS_WON = "rounds_won"
SCORING_METHOD_FORMULA_ONE = "formula_one"

FORMULA_ONE_POINTS = (25, 18, 15, 12, 10, 8, 6, 4, 2, 1)


def score_flag_captures(
    teams: list[Team],
    rounds: list[Round],
    captures: list[FlagCapture],
    current_round: Round | None,
) -> None:
    """
    Score one point for every flag capture.

    The score is split into:
    - passed_round_points
    - current_round_points
    - total_points
    """
    passed_points = defaultdict(int)
    current_points = defaultdict(int)

    for capture in captures:
        if current_round and capture.round_id == current_round.id:
            current_points[capture.team_id] += 1
        elif capture.round.is_finished(clock.now()):
            passed_points[capture.team_id] += 1

    for team in teams:
        team.passed_round_points = passed_points[team.id]
        team.current_round_points = current_points[team.id]
        team.total_points = team.passed_round_points + team.current_round_points


def score_rounds_won(
    teams: list[Team],
    rounds: list[Round],
    captures: list[FlagCapture],
    current_round: Round | None,
) -> None:
    """
    Give one point for every round won.

    Tied teams all win the round and therefore all receive one point.
    The current round does not contribute until it has finished.
    """
    passed_points = defaultdict(int)

    for round in rounds:
        if round.is_finished(clock.now()):
            continue

        standings = FlagCapture.get_round_standings(round)

        if not standings:
            continue

        winning_score = standings[0][1]

        for team_id, score in standings:
            if score != winning_score:
                break

            passed_points[team_id] += 1

    for team in teams:
        team.passed_round_points = passed_points[team.id]
        team.current_round_points = 0
        team.total_points = team.passed_round_points


def get_formula_one_points(
    standings: list[tuple[int, int]],
) -> dict[int, int]:
    """
    Assign Formula One points based on round standings.

    Equal scores receive the same position and therefore the same points.

    Example:
        A: 10
        B: 10
        C:  8
        D:  5

    Results:
        A: 25
        B: 25
        C: 15
        D: 12
    """
    points_by_team = {}

    previous_score = None
    position = 0

    for index, (team_id, score) in enumerate(standings):
        if score != previous_score:
            position = index + 1
            previous_score = score

        if position <= len(FORMULA_ONE_POINTS):
            points_by_team[team_id] = FORMULA_ONE_POINTS[position - 1]
        else:
            points_by_team[team_id] = 0

    return points_by_team


def score_formula_one(
    teams: list[Team],
    rounds: list[Round],
    captures: list[FlagCapture],
    current_round: Round | None,
) -> None:
    """
    Score each round using Formula One points.

    Completed rounds contribute to passed_round_points.
    The current round contributes to current_round_points.
    """
    passed_points = defaultdict(int)
    current_points = defaultdict(int)

    for round in rounds:
        standings = FlagCapture.get_round_standings(round)

        if not standings:
            continue

        round_points = get_formula_one_points(standings)

        if current_round and round.id == current_round.id:
            for team_id, points in round_points.items():
                current_points[team_id] += points
        elif round.round_end <= clock.now():
            for team_id, points in round_points.items():
                passed_points[team_id] += points

    for team in teams:
        team.passed_round_points = passed_points[team.id]
        team.current_round_points = current_points[team.id]
        team.total_points = team.passed_round_points + team.current_round_points


SCORING_FUNCTIONS: dict[str, Callable] = {
    SCORING_METHOD_FLAG_CAPTURES: score_flag_captures,
    SCORING_METHOD_ROUNDS_WON: score_rounds_won,
    SCORING_METHOD_FORMULA_ONE: score_formula_one,
}


def score_teams(
    teams: list[Team],
    rounds: list[Round],
    captures: list[FlagCapture],
    current_round: Round | None,
) -> None:
    """Apply the configured scoring method to all teams."""
    scoring_function = SCORING_FUNCTIONS[SCORING_METHOD]

    scoring_function(
        teams,
        rounds,
        captures,
        current_round,
    )


def add_places(teams: list[Team]) -> None:
    """Add leaderboard placement information to each team."""
    previous_points = None
    current_place = 0

    for index, team in enumerate(teams):
        if team.total_points != previous_points:
            current_place = index + 1
            previous_points = team.total_points

        team.place = current_place
        team.skip_place = (
            index > 0 and team.total_points == teams[index - 1].total_points
        )


def add_round_winners(
    teams: list[Team],
    rounds: list[Round],
    captures: list[FlagCapture],
) -> None:
    """Add the names of rounds won by each team."""
    round_winners = defaultdict(list)

    for round in rounds:
        if not round.is_finished(clock.now()):
            continue

        standings = FlagCapture.get_round_standings(round)

        if not standings:
            continue

        winning_score = standings[0][1]

        for team_id, score in standings:
            if score != winning_score:
                break

            round_winners[team_id].append(round.name)

    for team in teams:
        team.round_wins = round_winners.get(team.id, [])


def add_current_round_leaders(
    teams: list[Team],
    current_round: Round | None,
    captures: list[FlagCapture],
) -> None:
    """Mark all teams currently leading the active round."""
    if not current_round:
        return

    standings = FlagCapture.get_round_standings(current_round)

    if not standings:
        return

    winning_score = standings[0][1]
    leader_ids = {team_id for team_id, score in standings if score == winning_score}

    for team in teams:
        team.is_current_leader = team.id in leader_ids


@require_competition
def leaderboard(request: HttpRequest, competition: Competition):
    team_results = get_teams(request.active_competition, request)
    rounds = get_rounds(request.active_competition)
    captures = get_flag_captures(request.active_competition)
    current_round = get_current_round(rounds, request.time_now)

    score_teams(
        teams=team_results,
        rounds=rounds,
        captures=captures,
        current_round=current_round,
    )

    # The leaderboard is always sorted by the configured total score.
    team_results.sort(key=lambda team: (-team.total_points, team.name))

    add_places(team_results)
    add_round_winners(team_results, rounds, captures)
    add_current_round_leaders(team_results, current_round, captures)

    if team_results:
        team_results[0].is_current_winner = True

    if current_round:
        is_round_active = current_round.is_active(request.time_now)
    else:
        is_round_active = False

    print(team_results)

    return render_with_base_info(
        request,
        "leaderboard.html",
        {
            "current_round": current_round,
            "teams": team_results,
            "is_round_active": is_round_active,
        },
    )
