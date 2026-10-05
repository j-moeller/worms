from core.models.competition import Competition, Round
from core.models.scoring import FlagCapture, FlagCaptureAttempt
from core.models.team import AccessTokens, Team
from dashboard.views.decorators import user_in_team_required
from dashboard.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest


@login_required
@user_in_team_required
def flag_capture_attempts(request: HttpRequest, team: Team):
    team_token_hashes = AccessTokens.objects.filter(
        team=team,
    ).values_list("access_token_hash", flat=True)

    attempts = list(
        FlagCaptureAttempt.objects.filter(
            team_token_hash__in=team_token_hashes,
        ).order_by("-created_at", "-id")
    )

    attempt_ids = [attempt.id for attempt in attempts]

    # FIXME: Competitino ==> active
    captures = FlagCapture.objects.filter(
        flag_capture_attempt_id__in=attempt_ids,
        team=request.team,
        round__competition=request.active_competition,
        vm__round__competition=request.active_competition,
    ).select_related("vm", "round")

    capture_by_attempt = {
        capture.flag_capture_attempt_id: capture for capture in captures
    }

    rounds = list(
        Round.objects.filter(
            competition=request.active_competition,
        ).order_by("-round_start", "-id")
    )

    rows = []

    for round_obj in rounds:
        for attempt in attempts:
            if not round_obj.is_active(attempt.created_at):
                continue

            capture = capture_by_attempt.get(attempt.id)

            if (
                capture is not None
                and capture.round_id == round_obj.id
                and capture.vm.round_id == round_obj.id
            ):
                vm_identifier = capture.vm.vm_identifier
                invalid = False
            else:
                vm_identifier = None
                invalid = True

            if not invalid:
                rows.append(
                    {
                        "round": round_obj,
                        "attempt": attempt,
                        "vm_identifier": vm_identifier,
                        "invalid": invalid,
                    }
                )

    return render_with_base_info(
        request,
        "flag_capture_attempts.html",
        {"rows": rows},
    )
