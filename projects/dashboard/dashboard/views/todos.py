from core import clock
from core.models.competition import Competition, Round
from dashboard.views.decorators import require_competition
from dashboard.views.util import get_current_round, get_rounds, render_with_base_info
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpRequest
from django.shortcuts import redirect


@login_required
@require_competition
def todos(request: HttpRequest, competition: Competition):
    if not request.user.is_staff:
        return redirect("index")

    now = clock.now()
    rounds = get_rounds(competition)
    current_round = get_current_round(rounds, now)

    round_staff_information = list(
        Round.objects.filter(competition=competition)
        .annotate(
            virtual_machine_count=Count(
                "virtual_machines",
                distinct=True,
            ),
            # Runs of this round that deploy a team's upload (RoundRun.upload).
            worm_deployment_count=Count(
                "runs",
                filter=Q(runs__upload__isnull=False),
                distinct=True,
            ),
        )
        .order_by("round_start")
    )

    # Find the first round that has not started yet.
    next_round_found = False
    for round in round_staff_information:
        if round.is_active(now):
            state = "in_progress"
        elif round.is_finished(now):
            state = "passed"
        elif now < round.round_start:
            if not next_round_found and current_round is None:
                state = "incoming"
                next_round_found = True
            else:
                state = "scheduled"
        round.state = state

    return render_with_base_info(
        request, "todos.html", {"round_staff_information": round_staff_information}
    )
