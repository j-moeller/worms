"""Notifications from staff to the teams: the bell in the header, next to Upload and Progression,
and the page it opens (dashboard/views/notifications.py). Staff write, edit, switch on and off and
delete them on /staff/notifications/ (dashboard/views/staff_notifications.py).

A notification is for every team, or for the teams it names. Only active ones are shown, newest
first; the bell takes the colour of the most severe of them.
"""

import uuid

from core import clock
from core.models.team import Team
from core.models.user import User
from django.db import models
from django.db.models import Q


class Notification(models.Model):
    class Severity(models.TextChoices):
        # Least to most severe: the order the bell and the lists rank them in (RANK).
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        URGENT = "urgent", "Urgent"
        ERROR = "error", "Error"

    class Audience(models.TextChoices):
        ALL = "all", "Every team"
        TEAMS = "teams", "Selected teams"

    # Names it in URLs, never the primary key.
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    severity = models.TextField(choices=Severity.choices, default=Severity.INFO)
    message = models.TextField()
    active = models.BooleanField(default=True)
    audience = models.TextField(choices=Audience.choices, default=Audience.ALL)
    # With audience "teams": who sees it (one team, or several). Ignored with "all".
    teams = models.ManyToManyField(Team, blank=True, related_name="notifications")
    created_at = models.DateTimeField(default=clock.now)
    updated_at = models.DateTimeField(default=clock.now)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "notifications"
        indexes = [models.Index(fields=["active", "created_at"], name="notifications_active_created")]

    RANK = {severity: rank for rank, severity in enumerate(Severity.values)}
    # The site's colours for them (base.css: badge--*, notice--*, site-nav__bell--*).
    TONE = {"info": "info", "warning": "warn", "urgent": "hot", "error": "bad"}

    def __str__(self):
        return f"Notification({self.severity}, {self.message[:40]!r})"

    @property
    def tone(self) -> str:
        return self.TONE.get(self.severity, "info")

    @property
    def edited(self) -> bool:
        return self.updated_at > self.created_at

    @classmethod
    def shown_to(cls, team):
        """The active notifications a member of `team` sees (None: a user without a team), newest
        first: every team's, and those naming `team`."""
        audience = Q(audience=cls.Audience.ALL)
        if team is not None:
            audience |= Q(audience=cls.Audience.TEAMS, teams=team)
        return cls.objects.filter(audience, active=True).distinct().order_by("-created_at", "-id")

    @classmethod
    def most_severe(cls, severities) -> str | None:
        return max(severities, key=cls.RANK.__getitem__, default=None)
