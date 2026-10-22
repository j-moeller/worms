"""The conductor loop (orchestrator/rounds/conductor.py).

Run as a long-lived systemd service on the orchestrator (ansible role
django_app, django_daemons). It plans the runs of any round whose deadline has
passed and then advances the active run one step per tick. Safe to restart:
all state is in the database.

    manage.py conduct_rounds                 # loop forever
    manage.py conduct_rounds --once          # a single tick (tests, cron)
    manage.py conduct_rounds --plan-only     # only create the runs, then exit
"""

import time

from core.models.competition import ApplicationSetting
from core.models.rounds import RoundRun
from django.core.management.base import BaseCommand
from django.utils import timezone

from orchestrator.rounds.conductor import Conductor
from orchestrator.rounds.planner import plan_round


class Command(BaseCommand):
    help = "Run the round conductor"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="a single tick, then exit")
        parser.add_argument("--plan-only", action="store_true", help="only create runs for due rounds")
        parser.add_argument("--interval", type=float, default=5.0, help="seconds between idle ticks")

    def plan_due_rounds(self):
        """Create runs for every round whose deadline (round_start) has passed
        and that has no runs yet."""
        competition = ApplicationSetting.get_current_competition()
        if competition is None:
            return
        now = timezone.now()
        for round in competition.rounds.filter(round_start__lte=now):
            if not RoundRun.objects.filter(round=round).exists():
                runs = plan_round(round)
                self.stdout.write(f"planned {len(runs)} run(s) for round {round.name}")

    def handle(self, once, plan_only, interval, **options):
        self.plan_due_rounds()
        if plan_only:
            return
        conductor = Conductor()
        if once:
            self.plan_due_rounds()
            conductor.tick()
            return
        self.stdout.write("conductor started")
        while True:
            self.plan_due_rounds()
            busy = False
            try:
                busy = conductor.tick()
            except Exception:  # a bad run must not kill the loop
                import logging

                logging.getLogger("orchestrator").exception("conductor tick failed")
            if not busy:
                time.sleep(interval)
