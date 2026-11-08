"""Provision every team's devbox (docs/04-dev-environment.md).

Ensures each team of the current competition has a Devbox row with a stable
index and services-source address, then enqueues a single devbox_provision
host job carrying the full team list (slug, index, uuid and the team's
registered SSH keys). The round agent builds/updates the devbox stack and the
SSH gateway and reports the VMs back; it is idempotent, so this is safe to run
whenever teams change: after staff create teams on the dashboard, the
operator runs it (`worms devbox provision`).

    manage.py provision_devboxes            # enqueue a provision job

Indices are assigned once and kept (a deleted team frees its index for a new
one); at most 16 teams (the link networks 10.128.0.0/19).
"""

from core.models.competition import ApplicationSetting
from django.core.management.base import BaseCommand, CommandError

from orchestrator.rounds.devbox import ProvisionError, provision


class Command(BaseCommand):
    help = "Ensure each team has a devbox and enqueue a provision host job."

    def handle(self, **options):
        competition = ApplicationSetting.get_current_competition()
        if competition is None:
            raise CommandError("no current competition")
        try:
            spec, job = provision(competition)
        except ProvisionError as exc:
            raise CommandError(str(exc))
        if not spec:
            self.stdout.write("no teams; nothing to do")
            return
        self.stdout.write(f"{len(spec)} team(s): " + ", ".join(f"{t['slug']}@{t['index']}" for t in spec))
        if job is None:
            self.stdout.write("a provision job with this team list is already queued")
        else:
            self.stdout.write(f"enqueued devbox_provision host job {job.uuid}")
