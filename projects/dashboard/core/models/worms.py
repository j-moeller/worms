from core.models.competition import Round
from core.models.team import Team
from core.models.user import User
from django.db import models
from django.utils import timezone


class MalwareArtifact(models.Model):
    content_hash = models.TextField(unique=True)
    storage_key = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "malware_artifacts"


class Upload(models.Model):
    artifact = models.ForeignKey(
        MalwareArtifact,
        on_delete=models.CASCADE,
        related_name="uploads",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="uploads",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "uploads"
        indexes = [
            models.Index(fields=["artifact"]),
            models.Index(fields=["user"]),
            models.Index(fields=["created_at"]),
        ]


class WormDeployment(models.Model):
    round = models.ForeignKey(
        Round,
        on_delete=models.CASCADE,
        related_name="worm_deployments",
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="worm_deployments",
    )
    upload = models.ForeignKey(
        Upload,
        on_delete=models.CASCADE,
        related_name="worm_deployments",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "worm_deployments"
        indexes = [
            models.Index(fields=["round"]),
            models.Index(fields=["team"]),
            models.Index(fields=["upload"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["round", "team"],
                name="unique_worm_deployment_per_round_team",
            ),
        ]
