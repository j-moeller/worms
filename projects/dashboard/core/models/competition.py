from typing import Optional

from core import clock
from django.db import models
from django.db.models import F, Q
from django.http import HttpRequest


class Competition(models.Model):
    name = models.TextField()
    created_at = models.DateTimeField(default=clock.now)

    # Unused since dev rounds were removed (2026-11-06): the devbox has no rounds. The
    # columns stay so no migration or database grant has to change.
    is_dev_competition = models.BooleanField(default=False)

    registration_starts_at = models.DateTimeField(null=True, blank=True)
    registration_ends_at = models.DateTimeField(null=True, blank=True)

    # Unused, as is_dev_competition above.
    development_competition = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        db_table = "competitions"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        registration_starts_at__isnull=True,
                        registration_ends_at__isnull=True,
                    )
                    | models.Q(
                        registration_starts_at__isnull=False,
                        registration_ends_at__isnull=False,
                    )
                ),
                name="registration_dates_both_null_or_set",
            ),
        ]

    def is_registration_period(self):
        # No registration window set (e.g. the default competition that
        # `manage.py init` creates): there is no registration period.
        if self.registration_starts_at is None or self.registration_ends_at is None:
            return False

        return self.registration_starts_at <= clock.now() <= self.registration_ends_at


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

    @staticmethod
    def get_current_competition():
        try:
            settings = ApplicationSetting.objects.select_related(
                "current_competition"
            ).get(key="current")
        except ApplicationSetting.DoesNotExist:
            return None

        return settings.current_competition


class Round(models.Model):
    seed = models.BigIntegerField()
    round_start = models.DateTimeField(null=True, blank=True)
    round_end = models.DateTimeField(null=True, blank=True)
    name = models.TextField()
    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="rounds",
    )
    created_at = models.DateTimeField(default=clock.now)

    # The round's gameplan: a file in gameplans/ on the node and the orchestrator, named here
    # and nowhere read by the dashboard (docs/05-live-environment.md). Set by staff on the dashboard.
    # db_default too: rounds inserted with raw SQL (the simulation) get the defaults.
    gameplan = models.TextField(blank=True, default="", db_default="")
    # Seconds from a team's worm starting to the end of its run (the gameplan's round_duration).
    team_duration = models.PositiveIntegerField(default=3600, db_default=3600)
    # The orchestrator's check of the gameplan against this round, before the round starts:
    # "" (not yet), "ok" or "invalid" (why: the orchestrator's journal, never the database).
    gameplan_status = models.TextField(blank=True, default="", db_default="")
    gameplan_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "rounds"
        indexes = [
            models.Index(fields=["competition"]),
            models.Index(fields=["round_start"]),
            models.Index(fields=["round_end"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(round_start__isnull=True)
                    | Q(round_end__isnull=True)
                    | Q(round_end__gt=F("round_start"))
                ),
                name="round_end_after_round_start",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        round_start__isnull=True,
                        round_end__isnull=True,
                    )
                    | Q(
                        round_start__isnull=False,
                        round_end__isnull=False,
                    )
                ),
                name="round_dates_both_null_or_set",
            ),
        ]

    def __str__(self):
        return f"Round({self.name})"

    def is_active(self, now):
        if self.round_start is None or self.round_end is None:
            return True

        return self.round_start <= now < self.round_end

    def is_finished(self, now):
        """Over, results final: its end has passed and every team's run is over (a run whose
        attempt failed may go on past the end; docs/05-live-environment.md)."""
        if self.round_start is None or self.round_end is None:
            return False
        if now < self.round_end:
            return False
        from core.models.rounds import RoundRun

        return not RoundRun.objects.filter(round=self, state__in=RoundRun.OPEN_STATES).exists()
