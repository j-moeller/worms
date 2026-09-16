import uuid

from core.models.competition import Competition
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        if self.is_superuser:
            return f"{self.username} 👑"
        if self.is_staff:
            return f"{self.username} 🛡️"
        return self.username

    class Meta:
        db_table = "users"


class CompetitionParticipant(models.Model):
    """
    A user's participation in one particular competition.

    The same User can have multiple CompetitionParticipant records,
    e.g. if they take the course again in a later year.
    """

    competition = models.ForeignKey(
        Competition,
        on_delete=models.PROTECT,
        related_name="participants",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="competition_participations",
    )

    joined_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "competition_participants"
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "user"],
                name="unique_user_per_competition",
            ),
        ]
