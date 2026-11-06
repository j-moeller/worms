"""Student and staff accounts: one-time passwords, the CSV import and who may
manage whom. Shared by the dashboard's staff pages (dashboard/views/staff_users.py)
and `manage.py create_users`.

Every account is created with a random one-time password and
`must_change_password`, so the dashboard holds the user on the
change-password page at the first login (docs/accounts.md). The password is
returned once, to be shown once; only its hash is stored.

Writes use `update_fields`: the public dashboard's database role may only
update a handful of user columns (setup_db_permissions), and a full-row save
would be refused.
"""

from __future__ import annotations

import csv
import io
import secrets
import string
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction

# Unambiguous alphabet for a password a person may have to type once (no
# O/0, l/1/I): still 59^len, ~5.9 bits/char.
PASSWORD_ALPHABET = "".join(c for c in (string.ascii_letters + string.digits) if c not in "O0lI1")
PASSWORD_LENGTH = 14

# Accepted header spellings (lower-cased, stripped) for each CSV column.
FIELD_ALIASES = {
    "first_name": ("first name", "firstname", "first", "vorname", "given name"),
    "last_name": ("surname", "last name", "lastname", "last", "name", "nachname", "family name"),
    "email": ("email", "e-mail", "e-mail-adresse", "email address", "e-mail address", "mail"),
    "team": ("team", "team name", "teamname"),
}

MAX_IMPORT_ROWS = 2000
MAX_NAME = 150
MAX_USERNAME_LENGTH = 150


class CsvError(ValueError):
    """The CSV cannot be read at all (no header, no email column)."""


@dataclass
class Row:
    line: int
    first_name: str
    last_name: str
    email: str
    team: str = ""


@dataclass
class Parsed:
    rows: list[Row] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # "row 3: ..." per skipped row


@dataclass
class Created:
    user: object
    password: str


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def normalise_email(value: str) -> str:
    """The email as typed, trimmed. It is the username, which Django compares
    exactly at login, so the case is kept; duplicates are found ignoring case."""
    return (value or "").strip()


def parse_csv(text: str) -> Parsed:
    """Report malformed CSV as a user-facing validation error."""
    try:
        return _parse_csv(text)
    except csv.Error as exc:
        raise CsvError(f"The CSV cannot be read: {exc}.") from exc


def _parse_csv(text: str) -> Parsed:
    """Parse a student list: a header row, then one student per row (email
    required; first name, surname and team optional)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise CsvError("The CSV has no header row.")
    normalised = {name.strip().lower(): name for name in reader.fieldnames if name}
    mapping = {
        key: next((normalised[a] for a in aliases if a in normalised), None)
        for key, aliases in FIELD_ALIASES.items()
    }
    if mapping["email"] is None:
        raise CsvError(
            f"The CSV needs an email column (one of: {', '.join(FIELD_ALIASES['email'])}); "
            f"found: {', '.join(reader.fieldnames)}."
        )

    def cell(line, key):
        column = mapping[key]
        return (line.get(column) or "").strip() if column else ""

    parsed = Parsed()
    seen = set()
    for number, line in enumerate(reader, start=2):
        if len(parsed.rows) >= MAX_IMPORT_ROWS:
            parsed.problems.append(f"rows after {MAX_IMPORT_ROWS} were not read")
            break
        email = normalise_email(cell(line, "email"))
        if not email:
            parsed.problems.append(f"row {number}: no email, skipped")
            continue
        try:
            validate_email(email)
        except ValidationError:
            parsed.problems.append(f"row {number}: {email!r} is not an email address, skipped")
            continue
        if len(email) > MAX_USERNAME_LENGTH:
            parsed.problems.append(f"row {number}: email exceeds {MAX_USERNAME_LENGTH} characters, skipped")
            continue
        if email.casefold() in seen:
            parsed.problems.append(f"row {number}: {email} appears twice, skipped")
            continue
        seen.add(email.casefold())
        parsed.rows.append(
            Row(number, cell(line, "first_name")[:MAX_NAME], cell(line, "last_name")[:MAX_NAME], email,
                cell(line, "team"))
        )  # fmt: skip
    return parsed


def create_account(email, first_name="", last_name="", *, staff=False, using="default",
                   password_length=PASSWORD_LENGTH):
    """Create one account with a one-time password. Returns Created, or None
    if the email (= username) already exists."""
    from core.models.user import User

    email = normalise_email(email)
    if User.objects.using(using).filter(username__iexact=email).exists():
        return None
    password = generate_password(password_length)
    user = User(
        username=email,
        email=email,
        first_name=first_name[:MAX_NAME],
        last_name=last_name[:MAX_NAME],
        is_staff=staff,
        is_active=True,
        must_change_password=True,
    )
    user.set_password(password)
    try:
        # A savepoint keeps an enclosing import transaction usable on duplicates.
        with transaction.atomic(using=using):
            user.save(using=using)
    except IntegrityError:
        if User.objects.using(using).filter(username__iexact=email).exists():
            return None
        raise
    return Created(user, password)


def import_rows(rows, *, staff=False, competition=None, using="default"):
    """Create the accounts of `rows`; existing emails are left unchanged.
    With a competition, a row's team (by name) is joined -- an unknown or full
    team is reported, never created. Returns (created, problems)."""
    from core.models.team import Team, TeamMembership
    from core.membership import membership_change

    created, problems = [], []
    teams = {}
    if competition is not None:
        teams = {team.name.casefold(): team for team in Team.objects.using(using).filter(competition=competition)}
    for row in rows:
        # Commit each student independently; do not retain locks across teams.
        with transaction.atomic(using=using):
            result = create_account(row.email, row.first_name, row.last_name, staff=staff, using=using)
            if result is None:
                problems.append(f"row {row.line}: {row.email} already has an account (unchanged)")
                continue
            created.append(result)
            if not row.team:
                continue
            team = teams.get(row.team.casefold())
            if team is None:
                problems.append(f"row {row.line}: no team named {row.team!r}; {row.email} has no team")
            else:
                with membership_change(result.user, team, using=using) as team:
                    if TeamMembership.objects.using(using).filter(team=team).count() >= team.max_members:
                        problems.append(f"row {row.line}: {team.name} is full; {row.email} has no team")
                    else:
                        TeamMembership.objects.using(using).create(user=result.user, team=team)
    return created, problems


def role_of(user) -> str:
    if user.is_superuser:
        return "admin"
    return "staff" if user.is_staff else "student"


def reset_password(user, *, by):
    """A new one-time password (returned once). Ends the user's sessions
    (Django ties a session to the password hash). `by` is the signed-in account
    asking for it: the reset and its row in password_resets commit together, so
    neither can exist without the other."""
    from core.models.user import PasswordReset

    password = generate_password()
    user.set_password(password)
    user.must_change_password = True
    with transaction.atomic():
        user.save(update_fields=["password", "must_change_password"])
        PasswordReset.objects.create(
            actor_uuid=by.uuid,
            actor_username=by.username,
            actor_role=role_of(by),
            target_uuid=user.uuid,
            target_username=user.username,
            target_role=role_of(user),
        )
    return password


def can_manage(actor, target) -> bool:
    """Staff manage student accounts; only a superuser manages staff. Nobody
    manages a superuser here (the admin's password is `worms admin
    reset-password`), and nobody manages themselves through these pages."""
    if not actor.is_authenticated or not actor.is_staff:
        return False
    if target.pk == actor.pk or target.is_superuser:
        return False
    if target.is_staff:
        return actor.is_superuser
    return True
