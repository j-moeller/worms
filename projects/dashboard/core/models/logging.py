import uuid

from core import clock
from core.models.competition import Round
from core.models.team import Team
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now


class LoggingAttempt(models.Model):
    # No database default (UUID4() is not available on SQLite, the DEBUG
    # database): the logging API generates it (sandbox_api/files/app.py).
    uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    # The running team's uuid, recorded by the logging API directly (only one
    # team runs at a time, so no access token is needed there; see
    # docs/rounds.md). Nullable: a log sent while no round is active is not
    # attributed.
    team_uuid = models.TextField(null=True, blank=True)
    log_data = models.JSONField()

    # True when the log came from a team's devbox (attributed by source
    # address), not from the running round's sandbox (docs/devbox.md).
    devbox = models.BooleanField(default=False, db_default=False)

    # Database defaults as well: the sandbox API inserts only the columns
    # above (INSERT-only role), so these must be filled by PostgreSQL.
    processed = models.BooleanField(default=False, db_default=False)
    processed_at = models.DateTimeField(default=None, blank=True, null=True)
    created_at = models.DateTimeField(default=clock.now, db_default=Now())

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
    created_at = models.DateTimeField(default=clock.now)

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
