from core import clock
from core.models.competition import Round
from core.models.team import Team
from core.models.user import User
from core.models.vm import VirtualMachine
from django.db import models


class MalwareArtifact(models.Model):
    content_hash = models.TextField(unique=True)
    storage_key = models.TextField()
    created_at = models.DateTimeField(default=clock.now)

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
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        db_table = "uploads"
        indexes = [
            models.Index(fields=["artifact"]),
            models.Index(fields=["user"]),
            models.Index(fields=["created_at"]),
        ]


class WormDeploymentPlan(models.Model):
    round = models.ForeignKey(
        Round,
        on_delete=models.CASCADE,
        related_name="worm_deployment_plans",
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="worm_deployment_plans",
    )
    upload = models.ForeignKey(
        Upload,
        on_delete=models.CASCADE,
        related_name="worm_deployment_plans",
    )
    virtual_machine = models.ForeignKey(
        VirtualMachine,
        on_delete=models.CASCADE,
        related_name="worm_deployment_plans",
    )
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        db_table = "worm_deployment_plans"
        indexes = [
            models.Index(fields=["virtual_machine"]),
            models.Index(fields=["team"]),
            models.Index(fields=["upload"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["round", "team"],
                name="unique_worm_deployment_per_round_team",
            ),
        ]


# TODO: WormDeploymentPlan: Which VM should we deploy to
# TODO: WormDeployment: We actually uploaded it


class WormDeployment(models.Model):
    worm_deployment_plan = models.ForeignKey(
        WormDeploymentPlan,
        on_delete=models.CASCADE,
        related_name="worm_deployments",
    )
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        db_table = "worm_deployments"
