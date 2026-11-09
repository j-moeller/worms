"""Event tables for a forensic investigation: who did what, when, from where
(core/events.py writes them; docs/07-security.md, "Audit trail").

One table per kind of event, each with the columns that kind needs. Only the
important events, so they stay small: tens to a few hundred rows a day.

How they are protected (setup_db_permissions):
- Each writing component has its own database account, used for nothing else:
  `dbuser_events_dashboard` on the dashboard, `dbuser_events_node` on the node;
  the console writes as the admin. An account may only INSERT, only into the
  tables of its component, and only the columns it fills.
- The database fills `created_at` (now()) and `written_by` (the inserting account,
  current_user). No writer may supply either, so no row can be backdated or
  claim another writer.
- No service may read, change or delete a row; only the admin reads them
  (`worms status audit`).
- Plain values instead of foreign keys: a row outlives the accounts and teams it
  names. Never a password, hash or token.
"""

from django.db import models
from django.db.models.functions import Now

# Every event table, for `worms reset database` (which keeps them) and the grants.
EVENT_TABLES = (
    "login_events", "password_events", "impersonation_events", "account_events", "team_events",
    "credential_events", "competition_events", "ssh_gateway_logins", "platform_events",
)  # fmt: skip


class Event(models.Model):
    created_at = models.DateTimeField(db_default=Now())
    # The database account that inserted the row (DEFAULT current_user, migration 0004).
    written_by = models.TextField(db_default="", editable=False)

    class Meta:
        abstract = True


class Actor(Event):
    """An event someone did, on the dashboard or the console."""

    actor_uuid = models.UUIDField(null=True)
    actor_username = models.TextField(null=True)
    # The client's address as nginx saw it (the dashboard), null on the console.
    client_ip = models.GenericIPAddressField(null=True)

    class Meta:
        abstract = True


class LoginEvent(Event):
    """A sign-in, a failed one, or a sign-out on the dashboard. A failed attempt keeps
    the name tried only if that account exists: a password typed into the name field
    is never stored."""

    outcome = models.TextField()  # ok, failed, logout
    user_uuid = models.UUIDField(null=True)
    username = models.TextField(null=True)
    client_ip = models.GenericIPAddressField(null=True)
    # Failed attempts not recorded since the last row (at most 30 a minute per process).
    not_recorded_before = models.IntegerField(default=0)

    class Meta:
        db_table = "login_events"
        indexes = [models.Index(fields=["created_at"], name="login_events_created")]


class PasswordEvent(Actor):
    """A user set their own password (changed), or staff/the admin issued a one-time
    password (reset). password_resets keeps its own row of a reset too."""

    kind = models.TextField()  # changed, reset
    target_uuid = models.UUIDField(null=True)
    target_username = models.TextField(null=True)
    target_role = models.TextField(null=True)  # student, staff, admin
    one_time_password_replaced = models.BooleanField(null=True)

    class Meta:
        db_table = "password_events"
        indexes = [models.Index(fields=["created_at"], name="password_events_created")]


class ImpersonationEvent(Actor):
    """The admin viewed the dashboard as another user (view as), read only."""

    kind = models.TextField()  # started, ended
    target_uuid = models.UUIDField(null=True)
    target_username = models.TextField(null=True)

    class Meta:
        db_table = "impersonation_events"
        indexes = [models.Index(fields=["created_at"], name="impersonation_events_created")]


class AccountEvent(Actor):
    """Accounts made or changed by staff on the dashboard, or on the console."""

    kind = models.TextField()  # created, imported, updated, activated, deactivated, staff_created
    target_uuid = models.UUIDField(null=True)
    target_username = models.TextField(null=True)
    detail = models.JSONField(default=dict)  # small: e.g. the accounts of an import

    class Meta:
        db_table = "account_events"
        indexes = [models.Index(fields=["created_at"], name="account_events_created")]


class TeamEvent(Actor):
    """Teams and memberships, and a team resetting its devbox."""

    kind = models.TextField()  # created, deleted, member_added, member_removed, joined, left, devbox_reset
    team_uuid = models.UUIDField(null=True)
    team_name = models.TextField(null=True)
    target_uuid = models.UUIDField(null=True)  # the member, for member_added/removed
    target_username = models.TextField(null=True)

    class Meta:
        db_table = "team_events"
        indexes = [models.Index(fields=["created_at"], name="team_events_created")]


class CredentialEvent(Actor):
    """A team's devbox SSH keys and team API tokens (never the token)."""

    kind = models.TextField()  # ssh_key_added, ssh_key_removed, access_token_created, access_token_revoked
    team_uuid = models.UUIDField(null=True)
    team_name = models.TextField(null=True)
    name = models.TextField(null=True)  # the key's or the token's name
    # sshd's form (SHA256:<base64>), so an SSH gateway login can be matched to its key.
    ssh_fingerprint = models.TextField(null=True)

    class Meta:
        db_table = "credential_events"
        indexes = [models.Index(fields=["created_at"], name="credential_events_created")]


class CompetitionEvent(Actor):
    """Staff changes to the competition and its rounds."""

    kind = models.TextField()  # competition_updated, round_created, round_updated, round_deleted
    round_id = models.BigIntegerField(null=True)
    detail = models.JSONField(default=dict)

    class Meta:
        db_table = "competition_events"
        indexes = [models.Index(fields=["created_at"], name="competition_events_created")]


class SSHGatewayLogin(Event):
    """A team's login through the devbox SSH gateway (scripts/round_agent/events.py)."""

    logged_in_at = models.DateTimeField()  # the gateway's time of the login
    gateway_user = models.TextField()
    key_type = models.TextField()
    ssh_fingerprint = models.TextField()  # matches credential_events.ssh_fingerprint
    # The team's public address, from the node's log of new connections to the
    # published port, matched by source port; null when it could not be told.
    client_ip = models.GenericIPAddressField(null=True)
    gateway_peer_port = models.IntegerField()

    class Meta:
        db_table = "ssh_gateway_logins"
        indexes = [models.Index(fields=["created_at"], name="ssh_gateway_logins_created")]


class PlatformEvent(Event):
    """The platform itself: the internet window, the firewall watchdog stopping team
    VMs (the node), a wipe of the database (the console)."""

    kind = models.TextField()  # internet_allowed, internet_denied, watchdog_stopped, database_reset
    actor = models.TextField(null=True)  # the operator or the tool, as the writer knows it
    detail = models.JSONField(default=dict)

    class Meta:
        db_table = "platform_events"
        indexes = [models.Index(fields=["created_at"], name="platform_events_created")]
