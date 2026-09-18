from core import clock
from core.models.competition import Round
from core.models.team import Team
from core.models.vm import VirtualMachine
from django.db import models
from django.db.models import Q


class FlagCaptureAttempt(models.Model):
    team_token_hash = models.TextField()
    vm_flag_hash = models.TextField()

    processed = models.BooleanField(default=False)
    processed_at = models.DateTimeField(default=None, blank=True, null=True)
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        db_table = "flag_capture_attempts"
        indexes = [
            models.Index(
                fields=["id"],
                name="pending_flag_capture_attempts",
                condition=Q(processed=False),
            ),
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
        ]

    def __str__(self):
        return f"FlagCapture({self.team}, {self.vm}, , {self.round})"
