"""Idempotent database initialisation: the admin account and the default
competition.

Run once as part of provisioning (the orchestrator, after the migrations) and
safe to re-run: it creates what is missing and changes nothing that is already
there. It never prints or hardcodes the admin password -- that comes from the
environment (the Ansible vault delivers it, like every other secret).

    manage.py init                       # admin + default "WORMS <year>"
    manage.py init --competition "WORMS 2026"
    manage.py init --admin-username admin

Writes (users, competitions, application_setting) go through the `migrator`
database connection when it is configured (the orchestrator has it; the
least-privilege application role cannot create these rows), otherwise through
the default connection (DEBUG / sqlite).

The admin password is read from DJANGO_ADMIN_PASSWORD (preferred) or
DJANGO_SUPERUSER_PASSWORD. With neither set the admin user is still created
with an unusable password (login disabled) so the step stays idempotent; set
the password in the vault and re-run to enable it.
"""

import os

from core import clock
from core.models.competition import ApplicationSetting, Competition
from core.models.user import User
from django.core.management.base import BaseCommand
from django.db import connections, transaction


def _write_db():
    """The connection able to write users/competitions: the migrator (table
    owner) where configured, else the default (DEBUG/sqlite)."""
    return "migrator" if "migrator" in connections else "default"


class Command(BaseCommand):
    help = "Create the admin account and the default competition (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--admin-username",
            default=os.environ.get("DJANGO_ADMIN_USERNAME", "admin"),
            help="Admin username (default: admin, or $DJANGO_ADMIN_USERNAME).",
        )
        parser.add_argument(
            "--admin-email",
            default=os.environ.get("DJANGO_ADMIN_EMAIL", "admin@worms.internal"),
        )
        parser.add_argument(
            "--competition",
            default=None,
            help="Default competition name (default: 'WORMS <current year>').",
        )
        parser.add_argument(
            "--no-competition",
            action="store_true",
            help="Only ensure the admin account; do not create a competition.",
        )

    def handle(self, *args, **options):
        using = _write_db()
        with transaction.atomic(using=using):
            self._ensure_admin(using, options["admin_username"], options["admin_email"])
            if not options["no_competition"]:
                name = options["competition"] or f"WORMS {clock.now().year}"
                self._ensure_competition(using, name)

    # -- admin ---------------------------------------------------------------

    def _ensure_admin(self, using, username, email):
        admin = User.objects.using(using).filter(username=username).first()
        if admin is not None:
            # Keep it a staff/superuser account; never reset an existing
            # password (idempotent, and we must not clobber a changed one).
            changed = []
            if not admin.is_staff:
                admin.is_staff = True
                changed.append("is_staff")
            if not admin.is_superuser:
                admin.is_superuser = True
                changed.append("is_superuser")
            if not admin.is_active:
                admin.is_active = True
                changed.append("is_active")
            if changed:
                admin.save(using=using, update_fields=changed)
                self.stdout.write(f"admin: updated {username} ({', '.join(changed)})")
            else:
                self.stdout.write(f"admin: {username} already present")
            return

        password = os.environ.get("DJANGO_ADMIN_PASSWORD") or os.environ.get(
            "DJANGO_SUPERUSER_PASSWORD"
        )
        admin = User(
            username=username,
            email=email,
            is_staff=True,
            is_superuser=True,
            is_active=True,
        )
        if password:
            admin.set_password(password)
        else:
            admin.set_unusable_password()
            self.stderr.write(
                "admin: no DJANGO_ADMIN_PASSWORD set; created with login "
                "disabled. Set the password in the vault and re-run."
            )
        admin.save(using=using)
        self.stdout.write(f"admin: created {username} (is_staff=True, is_superuser=True)")

    # -- competition ---------------------------------------------------------

    def _ensure_competition(self, using, name):
        competition = Competition.objects.using(using).filter(name=name).first()
        if competition is None:
            competition = Competition.objects.using(using).create(name=name)
            self.stdout.write(f"competition: created {name!r}")
        else:
            self.stdout.write(f"competition: {name!r} already present")

        current = ApplicationSetting.objects.using(using).filter(key="current").first()
        if current is None:
            ApplicationSetting.objects.using(using).create(
                key="current", current_competition=competition
            )
            self.stdout.write(f"competition: set {name!r} as the current competition")
        else:
            self.stdout.write(
                f"competition: current is already "
                f"{current.current_competition.name!r} (unchanged)"
            )
