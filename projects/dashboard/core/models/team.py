import uuid

from core.models.competition import Competition
from core.models.user import User
from django.db import models
from django.utils import timezone


class Team(models.Model):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True)

    name = models.TextField()
    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="teams",
    )
    max_members = models.PositiveIntegerField(default=4)
    created_at = models.DateTimeField(default=timezone.now)

    users = models.ManyToManyField(
        User,
        through="TeamMembership",
        related_name="teams",
    )

    class Meta:
        db_table = "teams"
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "name"],
                name="unique_team_per_competition",
            ),
        ]


class AccessTokens(models.Model):
    access_token_hash = models.TextField(unique=True)
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    revoked = models.BooleanField(default=False)
    revoked_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)


class TeamMembership(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="team_memberships",
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "team_memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "team"],
                name="unique_user_team_membership",
            ),
        ]
