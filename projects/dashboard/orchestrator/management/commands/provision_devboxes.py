"""Provision every team's devbox (docs/devbox.md).

Ensures each team of the current competition has a Devbox row with a stable
index and services-source address, then enqueues a single devbox_provision
host job carrying the full team list (slug, index, uuid and the team's
registered SSH keys). The round agent builds/updates the devbox stack and the
SSH gateway and reports the VMs back; it is idempotent, so this is safe to run
whenever teams change (e.g. after `worms teams create`).

    manage.py provision_devboxes            # enqueue a provision job
    manage.py provision_devboxes --dry-run  # show what would be enqueued

Indices are assigned once and kept (a deleted team frees its index for a new
one); at most 32 teams (the services block 10.2.0.32/27).
"""

from core.models.competition import ApplicationSetting
from core.models.devbox import Devbox, DevboxSSHKey
from core.models.rounds import HostJob
from core.models.team import Team
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

MAX_TEAMS = 32


def team_slug(team) -> str:
    # A stable, valid slug (generate_devbox / the agent accept [a-z0-9-]).
    return f"t{team.id}"


class Command(BaseCommand):
    help = "Ensure each team has a devbox and enqueue a provision host job."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, dry_run, **options):
        competition = ApplicationSetting.get_current_competition()
        if competition is None:
            raise CommandError("no current competition")

        teams = list(Team.objects.filter(competition=competition).order_by("id"))
        if not teams:
            self.stdout.write("no teams; nothing to do")
            return
        if len(teams) > MAX_TEAMS:
            raise CommandError(f"{len(teams)} teams exceeds the devbox maximum of {MAX_TEAMS}")

        with transaction.atomic():
            used = set(
                Devbox.objects.filter(index__isnull=False).values_list("index", flat=True)
            )
            spec = []
            for team in teams:
                devbox, _ = Devbox.objects.get_or_create(team=team)
                if devbox.index is None:
                    devbox.index = self._next_free(used)
                    used.add(devbox.index)
                    devbox.services_source_ip = f"10.2.0.{32 + devbox.index}"
                    if not dry_run:
                        devbox.save(update_fields=["index", "services_source_ip"])
                spec.append(
                    {
                        "slug": team_slug(team),
                        "index": devbox.index,
                        "team_uuid": str(team.uuid),
                        "authorized_keys": list(
                            DevboxSSHKey.objects.filter(team=team).values_list("public_key", flat=True)
                        ),
                    }
                )

            self.stdout.write(f"{len(spec)} team(s): " + ", ".join(f"{t['slug']}@{t['index']}" for t in spec))
            if dry_run:
                self.stdout.write("(dry run: no job enqueued)")
                return

            job = HostJob.objects.create(
                kind=HostJob.Kind.DEVBOX_PROVISION, params={"teams": spec}
            )
            self.stdout.write(f"enqueued devbox_provision host job {job.uuid}")

    @staticmethod
    def _next_free(used):
        for i in range(MAX_TEAMS):
            if i not in used:
                return i
        raise CommandError("no free devbox index (max 32 teams)")
