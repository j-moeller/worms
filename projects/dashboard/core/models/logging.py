import uuid

from core.models.competition import Round
from core.models.team import Team
from django.db import models
from django.db.models import Q
from django.utils import timezone


class LoggingAttempt(models.Model):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    team_token_hash = models.TextField()
    log_data = models.JSONField()

    processed = models.BooleanField(default=False)
    processed_at = models.DateTimeField(default=None, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "logging_attempts"
        indexes = [
            models.Index(
                fields=["id"],
                name="pending_logging_attempts",
                condition=Q(processed=False),
            ),
            models.Index(fields=["uuid"], name="pending_logging_attempts_uuid"),
        ]


class Logging(models.Model):
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="logging",
    )
    round = models.ForeignKey(
        Round,
        on_delete=models.CASCADE,
        related_name="logging",
    )
    logging_attempt = models.ForeignKey(
        LoggingAttempt,
        on_delete=models.CASCADE,
        related_name="logging",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "logging"
        indexes = [
            models.Index(fields=["team"]),
            models.Index(fields=["created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["logging_attempt"],
                name="unique_logging_per_attempt",
            ),
        ]
