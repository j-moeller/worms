from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Competition(models.Model):
    name = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    registration_starts_at = models.DateTimeField()
    registration_ends_at = models.DateTimeField()

    class Meta:
        db_table = "competitions"

    @property
    def registration_is_open(self):
        now = timezone.now()
        return self.registration_starts_at <= now <= self.registration_ends_at


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
            models.Index(fields=["round_start"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(round_end__gt=F("round_start")),
                name="round_end_after_round_start",
            ),
        ]

    def __str__(self):
        return f"Round({self.name})"
