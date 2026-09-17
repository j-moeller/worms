from core.models.competition import Competition, Round
from core.models.scoring import FlagCapture
from core.models.team import Team, TeamMembership
from core.views.decorators import require_competition
from core.views.util import render_with_base_info
from django.db.models import BooleanField, Count, Exists, OuterRef, Value
from django.http import HttpRequest
from django.utils import timezone


@require_competition
def leaderboard(request: HttpRequest, competition: Competition):
    if request.user.is_authenticated:
        user_team_membership = TeamMembership.objects.filter(
            team=OuterRef("pk"),
            user=request.user,
        )

        teams = list(
            Team.objects.filter(competition=competition)
            .annotate(
                points=Count("flag_captures"),
                is_user_team=Exists(user_team_membership),
            )
            .order_by("-points", "name")
        )
    else:
        teams = list(
            Team.objects.filter(competition=competition)
            .annotate(
                points=Count("flag_captures"),
                is_user_team=Value(False, output_field=BooleanField()),
            )
            .order_by("-points", "name")
        )

    captures = list(
        FlagCapture.objects.filter(round__competition=competition)
        .select_related(
            "team",
            "flag_capture_attempt",
        )
        .order_by("flag_capture_attempt__created_at")
    )

    chart_data = []

    points_by_team = {}

    for capture in captures:
        team_id = capture.team_id

        points_by_team[team_id] = points_by_team.get(team_id, 0) + 1

        chart_data.append(
            {
                "time": capture.flag_capture_attempt.created_at.isoformat(),
                "team": capture.team.name,
                "points": points_by_team[team_id],
            }
        )

    now = timezone.now()
    rounds = Round.objects.filter(competition=competition).order_by("round_start")

    round_winners = {}

    for round in rounds:
        if now < round.round_end:
            continue

        teams_in_round = (
            Team.objects.filter(
                competition=competition,
                flag_captures__round=round,
            )
            .annotate(round_points=Count("flag_captures"))
            .filter(round_points__gt=0)
            .order_by("-round_points", "name")
        )

        if teams_in_round:
            winning_points = teams_in_round[0].round_points

            for team in teams_in_round.filter(round_points=winning_points):
                round_winners.setdefault(team.id, []).append(round.name)

    current_round = next(
        (round for round in rounds if round.round_start <= now <= round.round_end),
        None,
    )

    current_leader_ids = set()

    if current_round:
        teams_in_current_round = (
            Team.objects.filter(
                competition=competition,
                flag_captures__round=current_round,
            )
            .annotate(round_points=Count("flag_captures"))
            .filter(round_points__gt=0)
            .order_by("-round_points", "name")
        )

        if teams_in_current_round:
            winning_points = teams_in_current_round[0].round_points
            current_leader_ids = {
                team.id
                for team in teams_in_current_round.filter(round_points=winning_points)
            }

    for team in teams:
        team.round_wins = round_winners.get(team.id, [])
        team.is_current_leader = team.id in current_leader_ids

    round_data = [
        {
            "name": round.name,
            "start": round.round_start.isoformat(),
            "end": round.round_end.isoformat(),
        }
        for round in rounds
    ]

    if len(teams) > 0:
        teams[0].is_current_winner = True

    return render_with_base_info(
        request,
        "leaderboard.html",
        {
            "competition": competition,
            "teams": teams,
            "chart_data": chart_data,
            "round_data": round_data,
            "round_winners": round_winners,
        },
    )
