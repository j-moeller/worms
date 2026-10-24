from core import clock
from core.models.competition import Round
from core.models.team import Team
from core.models.vm import VirtualMachine
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now
from django.db.models.aggregates import Count


class FlagCaptureAttempt(models.Model):
    # Only one team runs at a time, so the scoring API is told which team is
    # running (its uuid) and records it directly -- no access token is needed
    # at the scoring server (docs/rounds.md). Nullable: an attempt made while
    # no round is active carries no team and is never scored.
    team_uuid = models.TextField(null=True, blank=True)
    vm_flag_hash = models.TextField()

    # True when the submission came from a team's devbox (attributed by source
    # address), not from the running round's sandbox. Devbox attempts are
    # scored against the team's DevboxVMs, never the round (docs/devbox.md).
    devbox = models.BooleanField(default=False, db_default=False)

    # Database defaults as well: the sandbox API inserts only the columns
    # above (INSERT-only role), so these must be filled by PostgreSQL.
    processed = models.BooleanField(default=False, db_default=False)
    processed_at = models.DateTimeField(default=None, blank=True, null=True)
    created_at = models.DateTimeField(default=clock.now, db_default=Now())

    class Meta:
        db_table = "flag_capture_attempts"
        indexes = [
            models.Index(
                fields=["id"],
                name="pending_flag_capture_attempts",
                condition=Q(processed=False),
            ),
            # A team's submissions in a time window, in order: the team API's
            # keyset pages (dashboard/api.py) and the round timeline.
            models.Index(fields=["team_uuid", "created_at", "id"], name="flag_attempts_team_time"),
        ]


class FlagCapture(models.Model):
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="flag_captures",
    )
    round = models.ForeignKey(
        Round,
        on_delete=models.CASCADE,
        related_name="flag_captures",
    )
    vm = models.ForeignKey(
        VirtualMachine,
        on_delete=models.CASCADE,
        related_name="flag_captures",
    )
    flag_capture_attempt = models.ForeignKey(
        FlagCaptureAttempt,
        on_delete=models.CASCADE,
        related_name="flag_captures",
    )
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        db_table = "flag_captures"
        indexes = [
            models.Index(fields=["team"]),
            models.Index(fields=["round"]),
            models.Index(fields=["vm"]),
            models.Index(fields=["flag_capture_attempt"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["flag_capture_attempt"],
                name="unique_capture_per_attempt",
            ),
            # A VM counts once per team, however often its flag is submitted.
            models.UniqueConstraint(
                fields=["team", "vm"],
                name="unique_capture_per_team_vm",
            ),
        ]

    def __str__(self):
        return f"FlagCapture({self.team}, {self.vm}, , {self.round})"

    @staticmethod
    def get_round_standings(round: Round) -> list[tuple[int, int]]:
        """Return (team_id, score) pairs for a round."""
        return list(
            FlagCapture.objects.filter(round=round)
            .values("team_id")
            .annotate(score=Count("id"))
            .order_by("-score", "team_id")
            .values_list("team_id", "score")
        )
