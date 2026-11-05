import uuid

from core import clock
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    created_at = models.DateTimeField(default=clock.now)

    # Student accounts are created from a CSV with a one-time random password
    # (core/management/commands/create_users.py). On first login the dashboard
    # forces them to set their own password (dashboard.middleware,
    # dashboard/views/password.py) and clears this flag.
    # db_default too: the simulation inserts users with raw SQL (no Django
    # default applied), and the column must not be NOT NULL without a default.
    must_change_password = models.BooleanField(default=False, db_default=False)

    def __str__(self):
        if self.is_superuser:
            return f"{self.username} 👑"
        if self.is_staff:
            return f"{self.username} 🛡️"
        return self.username

    class Meta:
        db_table = "users"


class PasswordReset(models.Model):
    """A one-time password issued for someone else's account (core/accounts.py,
    reset_password). Written in the same transaction as the reset.

    Append only: the dashboard's database role may add and read rows, but not
    change or delete them (setup_db_permissions). Plain values instead of
    foreign keys: a row records what was true at the time and outlives both
    accounts. It holds no password and no hash.
    """

    created_at = models.DateTimeField(default=clock.now)
    actor_uuid = models.UUIDField()
    actor_username = models.TextField()
    actor_role = models.TextField()
    target_uuid = models.UUIDField()
    target_username = models.TextField()
    target_role = models.TextField()

    class Meta:
        db_table = "password_resets"
