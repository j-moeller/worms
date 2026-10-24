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
