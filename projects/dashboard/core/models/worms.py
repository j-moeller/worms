from core import clock
from core.models.user import User
from django.db import models


class MalwareArtifact(models.Model):
    content_hash = models.TextField(unique=True)
    storage_key = models.TextField()
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        db_table = "malware_artifacts"


class Upload(models.Model):
    # Made in the dev view: it runs on the team's devbox and never enters a round
    # (orchestrator/rounds/devbox_runs.py, orchestrator/rounds/planner.py).
    devbox = models.BooleanField(default=False, db_default=False)
    artifact = models.ForeignKey(
        MalwareArtifact,
        on_delete=models.CASCADE,
        related_name="uploads",
    )
    # Who uploaded it...
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="uploads",
    )
    # ...for which team: the team's latest upload before a round's start is
    # its entry (orchestrator/rounds/planner.py), whoever is in the team now.
    # A member who changes teams does not take their uploads along. Null only
    # for uploads from before this was recorded that could not be attributed.
    team = models.ForeignKey(
        "core.Team",
        on_delete=models.CASCADE,
        related_name="uploads",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        db_table = "uploads"
        indexes = [
            models.Index(fields=["artifact"]),
            models.Index(fields=["user"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["team", "created_at"], name="uploads_team_time"),
        ]
