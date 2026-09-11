import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Competition(models.Model):
    name = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "competitions"


class ApplicationSetting(models.Model):
    # This table should contain exactly one row with the key 'current', which
    # marks the current competition.
    key = models.CharField(8, unique=True)
    current_competition = models.ForeignKey(
        Competition,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        db_table = "application_setting"
        indexes = [
            models.Index(fields=["key"]),
        ]


class Round(models.Model):
    seed = models.BigIntegerField()
    round_start = models.DateTimeField()
    round_end = models.DateTimeField()
    name = models.TextField()
    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="rounds",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "rounds"
        indexes = [
            models.Index(fields=["competition"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(round_end__gt=F("round_start")),
                name="round_end_after_round_start",
            ),
        ]


class Team(models.Model):
    name = models.TextField()
    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="teams",
    )
    access_token_hash = models.TextField(unique=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teams"
        indexes = [
            models.Index(fields=["competition"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "name"],
                name="unique_team_per_competition",
            ),
        ]


class User(AbstractUser):
    """
    Django-authenticated application user.

    Authentication is handled by Django. The application-specific relationship
    to a CTF team remains here.
    """

    team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "users"


class Student(models.Model):
    matriculation_number = models.TextField()
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="student",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "students"


class VirtualMachine(models.Model):
    vm_identifier = models.TextField()
    vm_maintenance_ip = models.TextField()
    ctf_flag_hash = models.TextField(unique=True)
    round = models.ForeignKey(
        Round,
        on_delete=models.CASCADE,
        related_name="virtual_machines",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "virtual_machines"
        indexes = [
            models.Index(fields=["round"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["round", "vm_identifier"],
                name="unique_vm_per_round",
            ),
        ]


class FlagCaptureAttempt(models.Model):
    team_token_hash = models.TextField()
    vm_flag_hash = models.TextField()

    processed = models.BooleanField(default=False)
    processed_at = models.DateTimeField(default=None, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

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
    created_at = models.DateTimeField(default=timezone.now)

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
