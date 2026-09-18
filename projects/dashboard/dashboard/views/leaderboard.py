from collections import defaultdict
from typing import Callable

from core.models.competition import Competition, Round
from core.models.scoring import FlagCapture
from core.models.team import Team, TeamMembership
from core import clock
from dashboard.views.decorators import require_competition
from dashboard.views.util import render_with_base_info
from django.db.models import BooleanField, Count, Exists, OuterRef, Value
from django.http import HttpRequest

SCORING_METHOD = "formula_one"

SCORING_METHOD_FLAG_CAPTURES = "flag_captures"
SCORING_METHOD_ROUNDS_WON = "rounds_won"
SCORING_METHOD_FORMULA_ONE = "formula_one"

FORMULA_ONE_POINTS = (25, 18, 15, 12, 10, 8, 6, 4, 2, 1)


def get_teams(
    competition: Competition,
    request: HttpRequest,
) -> list[Team]:
    """Return all teams with their leaderboard metadata."""
    if request.user.is_authenticated:
        user_team_membership = TeamMembership.objects.filter(
            team=OuterRef("pk"),
            user=request.user,
        )

        is_user_team = Exists(user_team_membership)
    else:
        is_user_team = Value(False, output_field=BooleanField())

    return list(
        Team.objects.filter(competition=competition)
        .annotate(
            is_user_team=is_user_team,
            flag_capture_count=Count("flag_captures"),
        )
        .order_by("name")
    )


def get_rounds(competition: Competition) -> list[Round]:
    """Return competition rounds ordered chronologically."""
    return list(Round.objects.filter(competition=competition).order_by("round_start"))


def get_current_round(rounds: list[Round], now) -> Round | None:
    """Return the currently active round, if there is one."""
    return next(
        (round for round in rounds if round.round_start <= now <= round.round_end),
        None,
    )


def get_flag_captures(
    competition: Competition,
) -> list[FlagCapture]:
    """Return all flag captures in chronological order."""
    return list(
        FlagCapture.objects.filter(round__competition=competition)
        .select_related(
            "team",
            "flag_capture_attempt",
            "round",
        )
        .order_by("flag_capture_attempt__created_at")
    )


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
        elif capture.round.round_end <= clock.now():
            passed_points[capture.team_id] += 1

    for team in teams:
        team.passed_round_points = passed_points[team.id]
        team.current_round_points = current_points[team.id]
        team.total_points = team.passed_round_points + team.current_round_points


def get_round_standings(round: Round) -> list[tuple[int, int]]:
    """Return (team_id, score) pairs for a round."""
    return list(
        FlagCapture.objects.filter(round=round)
        .values("team_id")
        .annotate(score=Count("id"))
        .order_by("-score", "team_id")
        .values_list("team_id", "score")
    )


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
        if round.round_end > clock.now():
            continue

        standings = get_round_standings(round)

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
        standings = get_round_standings(round)

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
        if round.round_end > clock.now():
            continue

        standings = get_round_standings(round)

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

    standings = get_round_standings(current_round)

    if not standings:
        return

    winning_score = standings[0][1]
    leader_ids = {team_id for team_id, score in standings if score == winning_score}

    for team in teams:
        team.is_current_leader = team.id in leader_ids


def build_round_data(rounds: list[Round]) -> list[dict]:
    """Build serializable round data for the template."""
    return [
        {
            "name": round.name,
            "start": round.round_start.isoformat(),
            "end": round.round_end.isoformat(),
        }
        for round in rounds
    ]


@require_competition
def leaderboard(request: HttpRequest, competition: Competition):
    now = clock.now()

    teams = get_teams(competition, request)
    rounds = get_rounds(competition)
    captures = get_flag_captures(competition)
    current_round = get_current_round(rounds, now)

    score_teams(
        teams=teams,
        rounds=rounds,
        captures=captures,
        current_round=current_round,
    )

    # The leaderboard is always sorted by the configured total score.
    teams.sort(key=lambda team: (-team.total_points, team.name))

    add_places(teams)
    add_round_winners(teams, rounds, captures)
    add_current_round_leaders(teams, current_round, captures)

    if teams:
        teams[0].is_current_winner = True

    if current_round:
        is_round_active = current_round.round_start < now < current_round.round_end
    else:
        is_round_active = False

    return render_with_base_info(
        request,
        "leaderboard.html",
        {
            "current_round": current_round,
            "competition": competition,
            "teams": teams,
            "round_data": build_round_data(rounds),
            "round_winners": {
                team.id: team.round_wins for team in teams if team.round_wins
            },
            "scoring_method": SCORING_METHOD,
            "is_round_active": is_round_active,
        },
    )
