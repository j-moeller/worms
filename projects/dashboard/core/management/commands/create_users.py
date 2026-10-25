"""Create student accounts from a CSV and print their one-time passwords.

The dashboard's staff pages do the same (Staff → Users → Import); this command
is the console route. Input CSV (header row required), one student per row,
columns for first name, surname and email (core/accounts.py; English and
German headers are accepted, an optional `team` column is ignored here).

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

Writes go through the `migrator` connection where configured (the orchestrator
has it), otherwise through the default connection.
"""

import csv

from core import accounts
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction


def _write_db():
    return "migrator" if "migrator" in connections else "default"


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
            default=accounts.PASSWORD_LENGTH,
            help=f"Length of the generated one-time passwords (default {accounts.PASSWORD_LENGTH}).",
        )
        parser.add_argument(
            "--staff",
            action="store_true",
            help="Create the accounts with is_staff=True (tutors/admins).",
        )

    def handle(self, csv_file, output, password_length, staff, *args, **options):
        if password_length < 10:
            raise CommandError("--password-length must be at least 10.")
        try:
            with open(csv_file, newline="", encoding="utf-8-sig") as handle:
                text = handle.read()
        except OSError as exc:
            raise CommandError(f"Cannot open {csv_file!r}: {exc}")
        try:
            parsed = accounts.parse_csv(text)
        except accounts.CsvError as exc:
            raise CommandError(f"{csv_file!r}: {exc}")
        for problem in parsed.problems:
            self.stderr.write(problem)
        if not parsed.rows:
            raise CommandError(f"No data rows found in {csv_file!r}.")

        created, skipped = [], []
        using = _write_db()
        with transaction.atomic(using=using):
            for row in parsed.rows:
                result = accounts.create_account(
                    row.email, row.first_name, row.last_name,
                    staff=staff, using=using, password_length=password_length,
                )  # fmt: skip
                if result is None:
                    skipped.append(row.email)
                else:
                    created.append((row.email, result.password))
        self._report(created, skipped, output)

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
