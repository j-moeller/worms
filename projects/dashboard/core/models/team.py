import uuid

from core import clock
from core.models.competition import Competition
from core.models.user import User
from django.db import models


class Team(models.Model):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True)

    name = models.TextField()
    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="teams",
    )
    max_members = models.PositiveIntegerField(default=2)
    created_at = models.DateTimeField(default=clock.now)

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

    def __str__(self):
        return f"Team({self.name})"


class AccessTokens(models.Model):
    name = models.TextField()
    description = models.TextField(default="")
    access_token_hash = models.TextField(unique=True)
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    revoked = models.BooleanField(default=False)
    revoked_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(default=clock.now)


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
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        db_table = "team_memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "team"],
                name="unique_user_team_membership",
            ),
        ]
