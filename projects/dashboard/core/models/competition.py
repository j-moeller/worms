from typing import Optional

from core import clock
from django.db import models
from django.db.models import F, Q
from django.http import HttpRequest


class Competition(models.Model):
    name = models.TextField()
    created_at = models.DateTimeField(default=clock.now)

    is_dev_competition = models.BooleanField(default=False)

    registration_starts_at = models.DateTimeField(null=True, blank=True)
    registration_ends_at = models.DateTimeField(null=True, blank=True)

    # The competition's development competition (is_dev_competition): the
    # Dev environment of its teams (docs/devbox.md). Teams created in the
    # dashboard get it as their Team.development_competition.
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
        if self.is_dev_competition:
            return False
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

    # What every run of this round gets (copied into RoundRun.sandbox_params
    # when the runs are planned): the sandbox generator's size (vms, networks,
    # sites, max_interfaces -- validated again by the round agent), the game
    # plan (orchestrator/rounds/gameplan.py) and the timing of a run
    # (core/schedule.py). Set by staff on the dashboard (docs/rounds.md).
    # db_default too: rounds inserted with raw SQL (the simulation) get {}.
    sandbox_params = models.JSONField(default=dict, db_default={}, blank=True)

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
        if self.round_start is None or self.round_end is None:
            return False

        return now >= self.round_end
