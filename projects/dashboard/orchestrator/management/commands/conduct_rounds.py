"""The conductor loop (orchestrator/rounds/conductor.py).

Run as a long-lived systemd service on the orchestrator (ansible role
django_app, django_daemons). It plans the runs of any round whose deadline has
passed and then runs one attempt at a time, start to finish. A restart during an
attempt fails it, and the team's worm runs again (docs/05-live-environment.md): do not redeploy
the orchestrator while a round runs.

    manage.py conduct_rounds                 # loop forever
    manage.py conduct_rounds --once          # a single tick (tests, cron)
    manage.py conduct_rounds --plan-only     # only create the runs, then exit
"""

import time

from core.models.competition import ApplicationSetting
from core.models.rounds import RoundRun
from django.core.management.base import BaseCommand
from django.db import close_old_connections
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
        if plan_only:
            self.plan_due_rounds()
            return
        conductor = Conductor()
        import signal

        def stop(*_):
            # The attempt under way fails at its next wait and is queued to run again.
            conductor.stopping = True
            if not busy_now[0]:
                raise SystemExit(0)

        busy_now = [False]
        signal.signal(signal.SIGTERM, stop)
        if once:
            self.plan_due_rounds()
            conductor.tick()
            return
        self.stdout.write("conductor started")
        while not conductor.stopping:
            close_old_connections()
            busy = False
            try:
                self.plan_due_rounds()
                busy_now[0] = True
                busy = conductor.tick()
            except Exception:  # a bad run must not kill the loop
                import logging

                logging.getLogger("orchestrator").exception("conductor tick failed")
            finally:
                busy_now[0] = False
            if not busy and not conductor.stopping:
                time.sleep(interval)
