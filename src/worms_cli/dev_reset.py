"""`worms db dev-reset`: the development system's dashboard database back to a known start.

    1. Not on the development system (worms-dev): asked first whether to run it here at all [y/N].
    2. Always asked: wipe ALL dashboard data? [y/N] (no --yes: it is never run unasked).
    3. `worms db reset`'s wipe and init (manage.py reset_database), then the accounts and teams:

           admin               password admin   (init's admin account)
           alpha@example.org   password alpha   team TeamA
           beta@example.org    password beta    team TeamB

       Their passwords need no change at the first login.
    4. For each team: an upload zip? [Y/n], then its path. Checked as the dashboard checks an
       upload (a zip with main.sh at its top level, at most 100 MiB), put into the dashboard's media
       folder (as the dashboard's user, where the round agent fetches it) and recorded as the
       team's latest upload: its entry for the next round.

Teams made here have no devbox yet: `worms apply devbox` gives them theirs.
"""

from __future__ import annotations

import hashlib
import io
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path

from worms_cli import output

ACCOUNTS = (  # (email = username, password, team)
    ("alpha@example.org", "alpha", "TeamA"),
    ("beta@example.org", "beta", "TeamB"),
)
ADMIN_PASSWORD = "admin"
MEDIA_ROOT = "/opt/dashboard/media"  # dashboard_media_root (ansible/group_vars/all.yml)
MEDIA_USER = "django"  # django_app_user (role django_app): the dashboard writes and reads its uploads as it
ENTRY = "main.sh"  # what the round agent starts (dashboard/views/uploads.py)
MAX_SIZE = 100 * 1024 * 1024  # MAX_UPLOAD_SIZE (config/settings/base.py)


def _ask(question: str, default: bool) -> bool:
    """[Y/n] or [y/N], asked at a terminal; without one, no."""
    if not sys.stdin.isatty():
        output.error(f"{question}: no terminal to ask in")
        return False
    answer = input(output.color(f"{question} [{'Y/n' if default else 'y/N'}] ", "yellow")).strip().lower()
    return default if answer == "" else answer in ("y", "yes")


def _zip(path_text: str) -> tuple[bytes | None, str]:
    """The zip's bytes, or None and why it cannot be a worm upload."""
    path = Path(path_text).expanduser()
    if not path.is_file():
        return None, f"{path}: no such file"
    data = path.read_bytes()
    if len(data) > MAX_SIZE:
        return None, f"{path}: {len(data)} bytes, more than the dashboard takes ({MAX_SIZE})"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, ValueError):
        return None, f"{path}: not a .zip archive"
    if ENTRY not in names:
        return None, f"{path}: no {ENTRY} at the zip's top level (the round agent starts it)"
    return data, ""


def _store(config, data: bytes) -> tuple[str, str]:
    """The zip into the dashboard's media folder, as the dashboard stores an upload. Returns
    (content_hash, storage_key)."""
    from worms_cli.build import BuildError
    from worms_cli.vms import Catalogue

    content_hash = hashlib.sha256(data).hexdigest()
    storage_key = f"malware-artifacts/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.zip"
    target = f"{MEDIA_ROOT}/{storage_key}"
    # As the dashboard's user, 0644 as its own uploads; complete or not there (the .pending
    # renamed), as the dashboard publishes one. cat reads the pipe it is given: the SSH session's,
    # which another user could not open again by /dev/stdin.
    script = f"umask 022 && mkdir -p {shlex.quote(target.rsplit('/', 1)[0])} && cat > {shlex.quote(target)}.pending && " \
             f"mv {shlex.quote(target)}.pending {shlex.quote(target)}"  # fmt: skip
    catalogue = Catalogue(config)
    argv, env = catalogue.ssh_argv(catalogue.resolve("dashboard"), f"sudo -n -u {MEDIA_USER} sh -c {shlex.quote(script)}",
                                   interactive=False)  # fmt: skip
    done = subprocess.run(argv, input=data, env=env, capture_output=True, timeout=120)
    if done.returncode != 0:
        raise BuildError(f"the zip did not reach the dashboard ({MEDIA_ROOT}/{storage_key}): "
                         f"{done.stderr.decode(errors='replace').strip()[:300]}")  # fmt: skip
    return content_hash, storage_key


def _upload(config, user, team) -> str:
    """Asked for a zip for this team: stored and recorded as its latest upload. Returns what was done."""
    from core.models.worms import MalwareArtifact, Upload

    name = user.username.split("@", 1)[0]
    if not _ask(f"Provide an upload zip for {name} ({team.name})?", True):
        return "no upload"
    while True:
        path = input(output.color(f"  path to {name}'s .zip (empty: none): ", "yellow")).strip()
        if not path:
            return "no upload"
        data, problem = _zip(path)
        if data is not None:
            break
        output.error(problem)
    content_hash, storage_key = _store(config, data)
    artifact, _ = MalwareArtifact.objects.get_or_create(content_hash=content_hash, defaults={"storage_key": storage_key})
    Upload.objects.create(artifact=artifact, user=user, team=team, devbox=False)
    return f"upload {content_hash[:12]} ({len(data)} bytes, {Path(path).name})"


def run(config, console: str) -> int:
    from django.core.management import call_command

    from worms_cli.session import for_config

    environment = for_config(config).environment()
    if environment.get("name") != "development":
        where = f"{environment.get('name')} ({environment.get('public_name') or environment.get('host')})"
        if not _ask(f"This is {where}, not worms-dev. Run a development command here?", False):
            return 1
    if not _ask("Wipe ALL dashboard data (teams, users, uploads, captures, logs) and set up admin, alpha (TeamA) and "
                "beta (TeamB)?", False):  # fmt: skip
        return 1

    call_command("reset_database", "--yes")
    from core import events
    from core.models.competition import ApplicationSetting
    from core.models.team import Team, TeamMembership
    from core.models.user import User

    admin = User.objects.filter(is_superuser=True).order_by("id").first()  # init's, the only account after the wipe
    admin.set_password(ADMIN_PASSWORD)
    admin.must_change_password = False
    admin.save(update_fields=["password", "must_change_password"])
    competition = ApplicationSetting.get_current_competition()
    made = [(admin.username, ADMIN_PASSWORD, "", "")]
    for email, password, team_name in ACCOUNTS:
        user = User(username=email, email=email, is_active=True, must_change_password=False)
        user.set_password(password)
        user.save()
        team = Team.objects.create(name=team_name, competition=competition)
        TeamMembership.objects.create(user=user, team=team)
        made.append((email, password, team_name, _upload(config, user, team)))
    events.platform("dev_reset", actor=console, detail={"accounts": [row[0] for row in made], "teams": [a[2] for a in ACCOUNTS]})

    print(output.heading("the development accounts"))
    print(output.table([[login, password, team or "—", upload or "—"] for login, password, team, upload in made],
                       headers=["Login", "Password", "Team", "Upload"]))  # fmt: skip
    print(output.color("Their devboxes: `worms apply devbox` (the teams made here have none yet).", "dim"))
    return 0
