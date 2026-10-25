"""Give the admin account a new one-time password.

    worms admin reset-password            # the CLI (src/manage.py) runs this
    manage.py reset_admin_password [--username admin]

The password is random, written once to stdout (the operator's terminal) and
stored only as Django's password hash: not in the vault, not in a log. The
dashboard forces a new password at the first login (must_change_password, the
same mechanism as the students' one-time passwords). Setting it also ends every
existing session of the account (Django ties sessions to the password hash).

Creates the account if it is missing (like `init`). Refuses an existing account
that is not a superuser, so it cannot turn a student into an admin.
"""

from core.accounts import generate_password
from core.management.commands.create_users import _write_db
from core.models.user import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Set a new one-time password for the admin account and print it once."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="admin")
        parser.add_argument("--length", type=int, default=20, help="password length (default 20)")

    def handle(self, *args, username, length, **options):
        if length < 16:
            raise CommandError("--length must be at least 16")
        using = _write_db()
        with transaction.atomic(using=using):
            admin = User.objects.using(using).select_for_update().filter(username=username).first()
            created = admin is None
            if created:
                admin = User(username=username, email=f"{username}@worms.internal")
            elif not admin.is_superuser:
                raise CommandError(f"{username!r} is not an admin account (not a superuser); refusing")
            admin.is_staff = admin.is_superuser = admin.is_active = True
            password = generate_password(length)
            admin.set_password(password)
            admin.must_change_password = True
            admin.save(using=using)

        action = "created" if created else "reset"
        self.stderr.write(
            f"admin: {action} {username!r}; one-time password below (shown once, not stored; "
            "the dashboard asks for a new one at the first login):"
        )
        self.stdout.write(password)
