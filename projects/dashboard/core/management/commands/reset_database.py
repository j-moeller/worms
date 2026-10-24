"""Reset the dashboard database to a clean slate and re-initialise it.

DANGER: this deletes ALL application data -- teams, users (including students
and their results), captures, logs, round runs, uploaded artifacts' metadata,
topology snapshots. It does NOT drop the schema or the migration history; it
TRUNCATEs the application tables and then runs `init` again (admin account and
the default competition).

    manage.py reset_database --yes

Writes go through the migrator connection where configured (it owns the
tables); a superuser role (the admin) also works. Intended for development and
for re-running a competition from scratch, driven by `worms reset-database`.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

# Tables we never truncate: Django's own migration bookkeeping and content
# types / permissions (recreated lazily, and TRUNCATE CASCADE from them would
# wipe auth needlessly).
KEEP = {"django_migrations", "django_content_type", "django_admin_log", "auth_permission"}


def _write_db():
    return "migrator" if "migrator" in connections else "default"


class Command(BaseCommand):
    help = "Wipe all application data and re-run init (DANGER)."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="required: confirm the wipe")
        parser.add_argument("--no-init", action="store_true", help="truncate only, do not re-init")

    def handle(self, yes, no_init, **options):
        if not yes:
            raise CommandError("refusing to wipe without --yes")

        using = _write_db()
        connection = connections[using]
        with connection.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
            tables = [row[0] for row in cur.fetchall() if row[0] not in KEEP]
            if tables:
                quoted = ", ".join(f'"{t}"' for t in tables)
                # One statement: RESTART IDENTITY resets sequences, CASCADE
                # handles FK order.
                cur.execute(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE")
        self.stdout.write(f"truncated {len(tables)} table(s)")

        if not no_init:
            call_command("init")
        self.stdout.write("database reset complete")
