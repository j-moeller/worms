# Accounts: admin, competition and student logins

How the dashboard is initialised with an administrator and a default
competition, and how student accounts are created and first sign in.

## The admin account and the default competition (`manage.py init`)

`core/management/commands/init.py` creates, **idempotently**:

- the **admin account**: username `admin`, `is_staff=True` and `is_superuser=True`
  (so it can use the Django admin at `/admin/` and the staff-only dashboard
  pages). Its password comes from the environment, never from code;
- the **default competition** `WORMS <current year>` (e.g. `WORMS 2026`), and
  the `application_setting` row `current` pointing at it, unless a current
  competition is already set.

It is safe to run repeatedly: existing objects are left as they are (an
existing admin password is never reset; an existing current competition is not
changed). Re-run it after setting the password in the vault, or to create a new
year's competition (`--competition "WORMS 2027"`).

Writes go through the **migrator** database connection when it is configured
(the orchestrator has it; the public dashboard role cannot create users or
competitions), otherwise through the default connection (local DEBUG/sqlite).

### Where the admin password lives — the vault key

Like every other internal secret, the admin password is **generated once** by
`scripts/generate_secrets.py` into the encrypted Ansible vault
(`ansible/group_vars/vault.yml`) under the key:

```
vault_django_admin_password
```

You never type or see it. To read or change it:

```
cd ansible
ansible-vault view --vault-password-file .vault_pass group_vars/vault.yml   # read
ansible-vault edit --vault-password-file .vault_pass group_vars/vault.yml   # change
```

The orchestrator receives it as `DJANGO_ADMIN_PASSWORD` in its root-owned
`0600` env file (`group_vars/orchestrator.yml` → `django_extra_env`), and runs
`manage.py init` once per `worms build provision` (the `django_post_migrate_commands`
hook of the `django_app` role, right after the migrations). If the password is
not set yet, the admin is created with login **disabled**; set the key and
re-provision to enable it.

Override the username/email with `DJANGO_ADMIN_USERNAME` / `DJANGO_ADMIN_EMAIL`
or the `--admin-username` / `--admin-email` flags.

## Student accounts from a CSV (`manage.py create_users`)

`core/management/commands/create_users.py` turns a CSV of students into login
accounts, each with a **one-time random password**.

```
manage.py create_users students.csv                 # prints email,password CSV to stdout
manage.py create_users students.csv --output creds.csv
manage.py create_users students.csv --password-length 16
manage.py create_users tutors.csv --staff           # is_staff accounts
```

**Input** (header row required; columns matched case-insensitively, English and
the German export headers both accepted):

| first name | surname | email |
|---|---|---|
| `first name` / `firstname` / `vorname` | `surname` / `last name` / `name` / `nachname` | `email` / `e-mail` / `e-mail-adresse` |

Only `email` is mandatory. For each email **not already present** a user is
created with:

- `username = email`, `is_active = True`, `is_staff = False` (unless `--staff`);
- a freshly generated password (unambiguous alphabet, no `O/0/l/1/I`);
- `must_change_password = True`.

Existing emails are skipped (no new password), so you can append rows to the
CSV and re-run.

**Output**: an `email,password` CSV on **stdout** (and, with `--output`, to a
file) — the only time the password is shown. A short summary (counts, skipped
emails) goes to stderr, so `create_users students.csv > creds.csv` yields a
clean CSV. Hand each student their line over a secure channel and delete the
file afterwards.

## First login forces a password change

An account created by `create_users` carries `must_change_password = True`
(the `must_change_password` column on `users`, migration `0008`). On login the
dashboard's `ForcePasswordChangeMiddleware` redirects the user to
`/change-password/` for every page until they set their own password; the view
(`dashboard/views/password.py`, Django's `SetPasswordForm`) saves the new
password, keeps the session alive (`update_session_auth_hash`) and clears the
flag.

The public dashboard database role (`django_dashboard`) is granted exactly
`UPDATE (password, must_change_password)` on `users` for this — it still cannot
grant itself staff or touch any other column (checked in
`tests/test_accounts.py`).

## Tests

`tests/test_accounts.py` (offline, part of `worms test`) runs both commands
against a throwaway PostgreSQL with the real migrations and least-privilege
grants: `init` creates the admin and competition and is idempotent;
`create_users` creates one account per row with a one-time password and
`must_change_password`, skips existing emails, and reads the German headers;
the dashboard role can set a password and clear the flag but nothing else.
