"""Create student accounts from a CSV and print their one-time passwords.

Input CSV (header row required), one student per row, columns for first name,
surname and email. Column names are matched case-insensitively and both
English and the German export headers are accepted:

    first name | firstname | vorname          -> first name
    surname    | last name | lastname | name | nachname -> surname
    email      | e-mail    | e-mail-adresse   -> email

For every email not already present, a user is created with:
  * username = email, is_staff = False, is_active = True;
  * a freshly generated random one-time password;
  * must_change_password = True (the dashboard forces a change on first login).

Existing emails are left untouched (no new password), so the command is safe
to re-run after adding rows to the CSV. The output is a CSV on stdout (and,
with --output, a file) of `email,password` for the accounts created in this
run -- the only time the password is shown. Hand each student their line over
a secure channel.

    manage.py create_users students.csv
    manage.py create_users students.csv --output credentials.csv
    manage.py create_users students.csv --password-length 16

Writes go through the `migrator` connection where configured (the student
table is owned by the migrator; the application role cannot create users).
"""

import csv
import secrets
import string
import sys

from core.models.user import User
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

# Unambiguous alphabet for a password a person may have to type once (no
# O/0, l/1/I): still 59^len, ~5.9 bits/char.
PASSWORD_ALPHABET = "".join(
    c for c in (string.ascii_letters + string.digits) if c not in "O0lI1"
)

# Accepted header spellings (lower-cased, stripped) for each field.
FIELD_ALIASES = {
    "first_name": ("first name", "firstname", "first", "vorname", "given name"),
    "last_name": ("surname", "last name", "lastname", "last", "name", "nachname", "family name"),
    "email": ("email", "e-mail", "e-mail-adresse", "email address", "e-mail address", "mail"),
}


def _write_db():
    return "migrator" if "migrator" in connections else "default"


def generate_password(length: int) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


class Command(BaseCommand):
    help = "Create student accounts from a CSV; print one-time passwords."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", type=str)
        parser.add_argument(
            "--output",
            type=str,
            default=None,
            help="Also write the email,password lines to this file (created new).",
        )
        parser.add_argument(
            "--password-length",
            type=int,
            default=14,
            help="Length of the generated one-time passwords (default 14).",
        )
        parser.add_argument(
            "--staff",
            action="store_true",
            help="Create the accounts with is_staff=True (tutors/admins).",
        )

    def handle(self, csv_file, output, password_length, staff, *args, **options):
        if password_length < 10:
            raise CommandError("--password-length must be at least 10.")

        rows = self._read_csv(csv_file)
        if not rows:
            raise CommandError(f"No data rows found in {csv_file!r}.")

        created = []  # (email, password)
        skipped = []
        using = _write_db()

        with transaction.atomic(using=using):
            for first_name, last_name, email in rows:
                if User.objects.using(using).filter(username=email).exists():
                    skipped.append(email)
                    continue
                password = generate_password(password_length)
                user = User(
                    username=email,
                    first_name=first_name,
                    last_name=last_name,
                    email=email,
                    is_staff=staff,
                    is_active=True,
                    must_change_password=True,
                )
                user.set_password(password)
                user.save(using=using)
                created.append((email, password))

        self._report(created, skipped, output)

    # -- CSV parsing ---------------------------------------------------------

    def _read_csv(self, path):
        try:
            handle = open(path, newline="", encoding="utf-8-sig")
        except OSError as exc:
            raise CommandError(f"Cannot open {path!r}: {exc}")
        with handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise CommandError(f"{path!r} has no header row.")
            mapping = self._match_columns(reader.fieldnames)
            rows = []
            for line_no, line in enumerate(reader, start=2):
                email = (line.get(mapping["email"]) or "").strip()
                if not email:
                    self.stderr.write(f"row {line_no}: no email, skipped")
                    continue
                rows.append(
                    (
                        (line.get(mapping["first_name"]) or "").strip(),
                        (line.get(mapping["last_name"]) or "").strip(),
                        email,
                    )
                )
        return rows

    def _match_columns(self, fieldnames):
        normalised = {name.strip().lower(): name for name in fieldnames}
        mapping = {}
        for field, aliases in FIELD_ALIASES.items():
            match = next((normalised[a] for a in aliases if a in normalised), None)
            if match is None:
                if field == "email":
                    raise CommandError(
                        f"CSV needs an email column (one of: {', '.join(FIELD_ALIASES['email'])}). "
                        f"Found: {', '.join(fieldnames)}"
                    )
                match = None  # first/last name are optional
            mapping[field] = match
        return mapping

    # -- output --------------------------------------------------------------

    def _report(self, created, skipped, output):
        writer = csv.writer(self.stdout)
        writer.writerow(["email", "password"])
        for email, password in created:
            writer.writerow([email, password])

        if output:
            with open(output, "w", newline="", encoding="utf-8") as handle:
                file_writer = csv.writer(handle)
                file_writer.writerow(["email", "password"])
                file_writer.writerows(created)

        self.stderr.write("")
        self.stderr.write(f"created {len(created)} account(s), skipped {len(skipped)} existing.")
        if output:
            self.stderr.write(f"credentials written to {output} (keep it secret, delete it after use).")
        if skipped:
            self.stderr.write(f"existing (unchanged): {', '.join(skipped)}")
