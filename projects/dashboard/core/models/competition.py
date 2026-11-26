import uuid
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
    # What URLs and the team API name a round by: not the primary key, which says how many
    # rounds the database has seen.
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
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

    # The round's gameplan: a file in gameplans/ on the host and the orchestrator, named here
    # and nowhere read by the dashboard (docs/05-live-environment.md). Set by staff on the dashboard.
    # db_default too: rounds inserted with raw SQL (the simulation) get the defaults.
    gameplan = models.TextField(blank=True, default="", db_default="")
    # Seconds from a team's worm starting to the end of its run (the gameplan's round_duration).
    team_duration = models.PositiveIntegerField(default=3600, db_default=3600)
    # The orchestrator's check of the gameplan against this round, before the round starts:
    # "" (not yet), "ok" or "invalid" (why: the orchestrator's journal, never the database).
    gameplan_status = models.TextField(blank=True, default="", db_default="")
    gameplan_checked_at = models.DateTimeField(null=True, blank=True)
    # When the operator published the round's results (`worms round publish`): until then no team
    # sees anything of its attempts, nor the leaderboard its points (FlagCapture.published,
    # RoundRun.shown_to_team). Staff see everything throughout.
    results_published_at = models.DateTimeField(null=True, blank=True)
    # Unused since 2026-11-23 (a round runs only from `worms round start`, which stops the devboxes
    # itself, asked); kept so that no migration drops what earlier rounds recorded.
    devboxes_off = models.BooleanField(default=True, db_default=True)
    devbox_hold = models.TextField(blank=True, default="", db_default="")
    on_hold = models.BooleanField(default=False, db_default=False)
    # Staff's own note on the round, written and shown in the rounds panel only (round_note): no
    # team page, team API or progression shows it.
    note = models.TextField(blank=True, default="", db_default="")
    # A `worms round start` session of this round is alive (src/worms_cli/rounds.py): it writes the
    # time every SESSION_BEAT seconds and clears it when it ends. Staff change a round whenever
    # none is (session_running); a killed session's mark is stale after SESSION_STALE seconds.
    session_seen_at = models.DateTimeField(null=True, blank=True)
    # The teams that take part, by id: null, every team of the competition (the planner). `worms round
    # dev-test` names its own, so that no other team's upload runs in it.
    entrants = models.JSONField(null=True, blank=True)
    SESSION_BEAT = 30
    SESSION_STALE = 120

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

    # What a round's deletion deletes, children first (the foreign keys do not cascade): its attempts'
    # events, flag captures, logs, VMs and host jobs, its attempts, the round. %(r)s: the round's id.
    DELETE_WITH_RECORDS = (
        "DELETE FROM run_events WHERE run_id IN (SELECT id FROM round_runs WHERE round_id = %(r)s)"
        " OR vm_id IN (SELECT id FROM virtual_machines WHERE round_id = %(r)s)",
        "DELETE FROM flag_captures WHERE round_id = %(r)s OR vm_id IN (SELECT id FROM virtual_machines WHERE round_id = %(r)s)",
        "DELETE FROM logging WHERE round_id = %(r)s OR run_id IN (SELECT id FROM round_runs WHERE round_id = %(r)s)",
        "DELETE FROM virtual_machines WHERE round_id = %(r)s OR run_id IN (SELECT id FROM round_runs WHERE round_id = %(r)s)",
        "DELETE FROM host_jobs WHERE run_id IN (SELECT id FROM round_runs WHERE round_id = %(r)s)",
        "DELETE FROM round_runs WHERE round_id = %(r)s",
        "DELETE FROM rounds WHERE id = %(r)s",
    )

    def delete_with_records(self) -> None:
        """This round and everything under it (DELETE_WITH_RECORDS), in the caller's transaction:
        the caller has checked that no session runs it, with its row locked (the dashboard's
        round_delete; `worms round dev-test` after its own session)."""
        from django.db import connection

        with connection.cursor() as cursor:
            for statement in self.DELETE_WITH_RECORDS:
                cursor.execute(statement, {"r": self.pk})

    def teams(self):
        """The teams that take part: its entrants, or every team of its competition."""
        from core.models.team import Team

        teams = Team.objects.filter(competition_id=self.competition_id)
        return teams if self.entrants is None else teams.filter(id__in=self.entrants)

    def session_running(self, now) -> bool:
        """Whether `worms round start` runs this round now (its heartbeat is fresh)."""
        return self.session_seen_at is not None and (now - self.session_seen_at).total_seconds() < self.SESSION_STALE

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
