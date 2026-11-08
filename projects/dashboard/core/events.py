"""Writes the event tables (core/models/events.py, docs/07-security.md, "Audit trail").

One function per table. Each writes one row with a plain INSERT: the writing
accounts may insert but not read (RETURNING would need SELECT), and the
database fills `created_at` and `written_by`. The dashboard writes through its
own events account (the `events` connection, `dbuser_events_dashboard`), never
its main role; the console writes as the admin.

Best effort: a failed insert is logged and never stops what the user was doing,
and it runs in its own savepoint so the surrounding transaction survives.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import threading
import time

from django.conf import settings
from django.db import DatabaseError, connections, transaction

log = logging.getLogger("dashboard.events")

MAX_TEXT = 200
MAX_DETAIL = 2048  # characters of JSON; the database refuses rows above 4096 bytes


def _using() -> str:
    return "events" if "events" in settings.DATABASES else "default"


def client_ip(request) -> str | None:
    """The client's address: nginx sets X-Real-IP to the connection's address (the node
    publishes the dashboard by DNAT without SNAT, so it is the real one)."""
    if request is None:
        return None
    for value in (request.META.get("HTTP_X_REAL_IP"), request.META.get("REMOTE_ADDR")):
        try:
            return str(ipaddress.ip_address((value or "").strip()))
        except ValueError:
            continue
    return None


def ssh_fingerprint(public_key: str) -> str | None:
    """The key's fingerprint as sshd logs it (`SHA256:<base64, no padding>`), so an SSH
    gateway login (ssh_gateway_logins) can be matched to the key and who registered it."""
    parts = (public_key or "").split()
    if len(parts) < 2:
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _text(value):
    return None if value is None else str(value)[:MAX_TEXT]


def _detail(detail) -> str:
    text = json.dumps(detail or {}, ensure_ascii=False, default=str, sort_keys=True)
    if len(text) <= MAX_DETAIL:
        return text
    return json.dumps({"truncated": True, "start": text[: MAX_DETAIL - 100]}, ensure_ascii=False)


def _user(user):
    if user is None or not getattr(user, "is_authenticated", False):
        return None, None
    return str(user.uuid), _text(user.username)


def _actor(request, actor):
    """Who did it: given (an account, or a name such as the console's operator), else the
    signed-in account (the admin's own while viewing as someone)."""
    if isinstance(actor, str):
        return None, _text(actor)
    if actor is None and request is not None:
        actor = getattr(request, "real_user", None) or getattr(request, "user", None)
    return _user(actor)


def role_of(user) -> str | None:
    if user is None:
        return None
    return "admin" if user.is_superuser else "staff" if user.is_staff else "student"


def _insert(table: str, row: dict, using: str | None = None) -> bool:
    """INSERT one row; the column names come from this module, never from input."""
    using = using or _using()
    columns = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    try:
        with transaction.atomic(using=using):
            with connections[using].cursor() as cursor:
                cursor.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(row.values()))
        return True
    except DatabaseError:
        log.exception("event not recorded in %s", table)
        return False


# --- one function per table -------------------------------------------------------------------


def login(request, outcome: str, user=None, not_recorded_before: int = 0) -> bool:
    user_uuid, username = _user(user)
    return _insert("login_events", {
        "outcome": outcome, "user_uuid": user_uuid, "username": username, "client_ip": client_ip(request),
        "not_recorded_before": not_recorded_before,
    })  # fmt: skip


def password(kind: str, *, request=None, actor=None, target=None, one_time_password_replaced=None, using=None) -> bool:
    actor_uuid, actor_name = _actor(request, actor)
    target_uuid, target_name = _user(target)
    return _insert("password_events", {
        "kind": kind, "actor_uuid": actor_uuid, "actor_username": actor_name, "client_ip": client_ip(request),
        "target_uuid": target_uuid, "target_username": target_name, "target_role": role_of(target),
        "one_time_password_replaced": one_time_password_replaced,
    }, using)  # fmt: skip


def impersonation(kind: str, *, request, admin, target) -> bool:
    actor_uuid, actor_name = _user(admin)
    target_uuid, target_name = _user(target)
    return _insert("impersonation_events", {
        "kind": kind, "actor_uuid": actor_uuid, "actor_username": actor_name, "client_ip": client_ip(request),
        "target_uuid": target_uuid, "target_username": target_name,
    })  # fmt: skip


def account(kind: str, *, request=None, actor=None, target=None, detail=None, using=None) -> bool:
    actor_uuid, actor_name = _actor(request, actor)
    target_uuid, target_name = _user(target)
    return _insert("account_events", {
        "kind": kind, "actor_uuid": actor_uuid, "actor_username": actor_name, "client_ip": client_ip(request),
        "target_uuid": target_uuid, "target_username": target_name, "detail": _detail(detail),
    }, using)  # fmt: skip


def team(kind: str, *, request=None, actor=None, team=None, team_name=None, member=None, using=None) -> bool:
    actor_uuid, actor_name = _actor(request, actor)
    target_uuid, target_name = _user(member)
    return _insert("team_events", {
        "kind": kind, "actor_uuid": actor_uuid, "actor_username": actor_name, "client_ip": client_ip(request),
        "team_uuid": str(team.uuid) if team is not None else None,
        "team_name": _text(team.name if team is not None else team_name),
        "target_uuid": target_uuid, "target_username": target_name,
    }, using)  # fmt: skip


def credential(kind: str, *, request, team, name=None, public_key=None) -> bool:
    actor_uuid, actor_name = _actor(request, None)
    return _insert("credential_events", {
        "kind": kind, "actor_uuid": actor_uuid, "actor_username": actor_name, "client_ip": client_ip(request),
        "team_uuid": str(team.uuid), "team_name": _text(team.name), "name": _text(name),
        "ssh_fingerprint": ssh_fingerprint(public_key) if public_key else None,
    })  # fmt: skip


def competition(kind: str, *, request, round_id=None, detail=None) -> bool:
    actor_uuid, actor_name = _actor(request, None)
    return _insert("competition_events", {
        "kind": kind, "actor_uuid": actor_uuid, "actor_username": actor_name, "client_ip": client_ip(request),
        "round_id": round_id, "detail": _detail(detail),
    })  # fmt: skip


def platform(kind: str, *, actor=None, detail=None, using=None) -> bool:
    """The console's platform events (the node writes its own: scripts/round_agent/events.py)."""
    return _insert("platform_events", {"kind": kind, "actor": _text(actor), "detail": _detail(detail)}, using)


# --- failed sign-ins, capped ------------------------------------------------------------------


class _Bucket:
    """At most `rate` events a minute per process, so a flood of failed sign-ins cannot
    flood the table; what is dropped is counted into the next row."""

    def __init__(self, rate: int):
        self.rate, self.tokens, self.at, self.dropped = rate, float(rate), time.monotonic(), 0
        self.lock = threading.Lock()

    def take(self) -> int | None:
        """The number dropped since the last event taken, or None when this one must be dropped."""
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.rate, self.tokens + (now - self.at) * self.rate / 60)
            self.at = now
            if self.tokens < 1:
                self.dropped += 1
                return None
            self.tokens -= 1
            dropped, self.dropped = self.dropped, 0
            return dropped


_failed_logins = _Bucket(rate=30)


def login_failed(request, username: str) -> None:
    """A failed sign-in. The name tried is kept only if such an account exists: a password
    typed into the name field must not end up in the table."""
    dropped = _failed_logins.take()
    if dropped is None:
        return
    from core.models.user import User

    known = User.objects.filter(username=username).only("uuid", "username").first() if username else None
    login(request, "failed", user=known, not_recorded_before=dropped)
