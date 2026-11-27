"""`worms round dev-test`: a whole round, quickly, on the development system (docs/03-manage-py.md).

A few questions, nothing else (and, not on worms-dev, whether to run a development command here):

    1. the gameplan: one of gameplans/ (its round_duration is the round's duration per team)
    2. an upload zip: one of solutions/ (this checkout's, git-ignored), or a path
    3. the list again for the next one, after each: left empty, "Include another upload? [y/N]"
       (yes: the list again; no: these are the uploads)

Then, for each zip, a user (devtest-<time>-<n>@example.org, no password: nobody logs in as it) in a
team of its own (DevTest <time> #<n>), the zip its upload, and a round of those teams only
(Round.entrants), starting now, which runs at once: `worms round start <it> --yes`
(worms_cli/rounds.py), every question of it answered yes. Its session syncs this checkout to the
host's workspace first, every time and uncommitted changes too (rounds._sync_workspace, force):
the gameplan runs as it is on disk, not as an earlier sync left it.

After the round: "Replay the same gameplan (its sandbox kept: no build, no teardown) with another
or an updated zip? [y/N]". Yes: each team's zip, from the same list (empty: as before); each new
one is that team's upload, the round's start moves after them, and the round runs again: its
session finds the teams whose upload changed (conductor.stale), keeps their earlier attempts as
superseded and runs them anew from the sandbox's snapshot, the workspace as it was (no sync).
Then the same question. No: the round agent and the orchestrator start again; the devboxes stay
stopped (`worms vm start devboxes`), and so does the sandbox, for the next dev test of the same
gameplan (`worms round teardown` destroys it). The round, its teams and uploads are kept.

Ctrl-C, whenever: everything it made goes, as if the round never was. The attempt under way fails
(its VMs stopped) and the session ends; the sandbox is destroyed (`worms round teardown`); the
round agent and the orchestrator are started again (not the devboxes); then the round with its
attempts and their records, the uploads and their zips on the dashboard VM, the teams and the
users. Ctrl-C is ignored while that runs, so that it ends complete. The audit logs keep their rows.
"""

from __future__ import annotations

import random
import shlex
import signal
import subprocess
from argparse import Namespace
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from worms_cli import dev_reset, output

ASK = dev_reset._ask  # [Y/n] at a terminal; the tests replace these two
PROMPT = input
DEFAULT_DURATION = 3600  # a gameplan's round_duration when it names none (gameplan/definition.py)


@dataclass
class Made:
    """What the dev test made, for Ctrl-C to take away again."""

    users: list = field(default_factory=list)
    teams: list = field(default_factory=list)
    uploads: list = field(default_factory=list)
    round: object = None
    ran: bool = False  # its session began: a sandbox may be there, something may be stopped


def _gameplan(folder: Path) -> tuple[str, int]:
    """The gameplan chosen (a file of gameplans/) and its round_duration."""
    from gameplan import loader

    names = loader.available(folder)
    if not names:
        raise SystemExit(f"no gameplan in {folder}")
    print(output.heading("the gameplan"))
    for n, name in enumerate(names, 1):
        print(f"  {n}. {name}")
    while True:
        answer = PROMPT(output.color(f"  gameplan [1-{len(names)}, or its name]: ", "yellow")).strip()
        name = names[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(names) else answer
        if name in names:
            break
        output.error(f"{answer!r}: one of 1-{len(names)}, or a name above")
    try:  # its spec's: a gameplan may set it in __init__
        from gameplan.runtime import instantiate

        duration = int(instantiate(loader.load(folder, name).cls).spec()["round_duration"])
    except Exception as exc:  # noqa: BLE001  (it does not load: the round's own check says why)
        output.warn(f"{name} did not load here ({str(exc)[:200]}): {DEFAULT_DURATION} s per team")
        duration = DEFAULT_DURATION
    return name, duration


def _zip(folder: Path, label: str, empty: str = "") -> tuple[str, bytes] | None:
    """A zip: one of `folder` (solutions/) by its number or name, or any path; asked until it is one
    the dashboard takes (dev_reset._zip). Without a zip in `folder`, a path. None: left empty, when
    `empty` says what that means (else it is asked again)."""
    zips = sorted(path.name for path in folder.glob("*.zip")) if folder.is_dir() else []
    for n, name in enumerate(zips, 1):
        print(f"  {n}. {name}")
    choice = f"1-{len(zips)}, its name, or a path" if zips else "its path"
    while True:
        answer = PROMPT(output.color(f"  {label} [{choice}{f'; empty: {empty}' if empty else ''}]: ", "yellow")).strip()
        if not answer and empty:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(zips):
            answer = zips[int(answer) - 1]
        path = str(folder / answer) if answer in zips else answer
        data, problem = dev_reset._zip(path) if path else (None, f"{choice}, please")
        if data is not None:
            return path, data
        output.error(problem)


def _make(config, made: Made, gameplan: str, duration: int, zips: list) -> None:
    from core import events
    from core.models.competition import ApplicationSetting, Round
    from core.models.team import Team, TeamMembership
    from core.models.user import User
    from django.utils import timezone

    competition = ApplicationSetting.get_current_competition()
    stamp = timezone.now().strftime("%H%M%S")
    print(output.heading("the teams and their uploads"))
    for n, (path, data) in enumerate(zips, 1):
        user = User(username=f"devtest-{stamp}-{n}@example.org", email=f"devtest-{stamp}-{n}@example.org", is_active=True,
                    must_change_password=False)  # fmt: skip
        user.set_unusable_password()
        user.save()
        made.users.append(user)
        team = Team.objects.create(name=f"DevTest {stamp} #{n}", competition=competition)
        made.teams.append(team)
        TeamMembership.objects.create(user=user, team=team)
        _upload(config, made, team, user, path, data)
    now = timezone.now()  # after the uploads: each is its team's latest before the start
    window = max(4 * 3600, len(zips) * (duration + 1800) + 7200)  # the build, every team, room to spare
    made.round = Round.objects.create(
        competition=competition, name=f"dev-test {stamp}", seed=random.SystemRandom().randrange(2**62),
        round_start=now, round_end=now + timedelta(seconds=window), gameplan=gameplan, team_duration=duration,
        entrants=[team.id for team in made.teams],
    )  # fmt: skip
    events.platform("dev_test", actor="console", detail={"round": made.round.id, "teams": [t.name for t in made.teams]})
    print(output.status_cell("ok", f"round {made.round.id} {made.round.name!r}: {gameplan}, {duration} s per team, "
                                   f"{len(made.teams)} team(s), starting now"))  # fmt: skip


def _upload(config, made: Made, team, user, path: str, data: bytes) -> None:
    """The zip stored as the dashboard stores an upload (dev_reset._store), the team's latest upload."""
    from core.models.worms import MalwareArtifact, Upload

    content_hash, storage_key = dev_reset._store(config, data)
    artifact, _ = MalwareArtifact.objects.get_or_create(content_hash=content_hash, defaults={"storage_key": storage_key})
    made.uploads.append(Upload.objects.create(artifact=artifact, user=user, team=team, devbox=False))
    print(output.status_cell("ok", f"{team.name}: {user.username}, upload {content_hash[:12]} ({Path(path).name})"))


def _replay(config, made: Made, solutions: Path) -> bool:
    """Each team's zip again (empty: as before); the new ones its uploads, the round's start moved
    after them (its end too, if it would come first). False: no new upload."""
    from core.models.competition import Round
    from django.utils import timezone

    print(output.heading("the replay: the same gameplan, its sandbox kept"))
    new = 0
    for team, user in zip(made.teams, made.users):
        picked = _zip(solutions, f"{team.name}'s zip", empty="as before")
        if picked is not None:
            _upload(config, made, team, user, *picked)
            new += 1
    if not new:
        print("Every team's upload is as before: nothing to replay.")
        return False
    now = timezone.now()  # after the new uploads: each is its team's latest before the start
    end = max(made.round.round_end, now + timedelta(seconds=len(made.teams) * (made.round.team_duration + 1800) + 3600))
    Round.objects.filter(pk=made.round.pk).update(round_start=now, round_end=end)
    made.round.refresh_from_db()
    return True


def finish(config, made: Made) -> None:
    """The dev test's end (no replay): the round agent and the orchestrator started again; the
    devboxes stay stopped, the sandbox kept."""
    from worms_cli import rounds

    print(output.heading("the dev test ends"))
    stopped = rounds.stopped_record().get("stopped", [])
    if stopped:
        session = SimpleNamespace(config=config, yes=True, round=made.round, dev_test=True)
        rounds._start_again(session, stopped)
        rounds._close_record(session)
    print(output.color(f"Round {made.round.id} {made.round.name!r}, its teams and uploads are kept. Its sandbox too, for the "
                       "next dev test of this gameplan: `worms round teardown` destroys it.", "dim"))  # fmt: skip


def _remove_uploads(config, uploads) -> None:
    """The uploads, and each zip on the dashboard VM that no other upload holds."""
    from core.models.worms import MalwareArtifact, Upload
    from django.db import IntegrityError
    from worms_cli.vms import Catalogue

    artifacts = {upload.artifact_id for upload in uploads}
    Upload.objects.filter(pk__in=[upload.pk for upload in uploads]).delete()
    catalogue = None
    for artifact in MalwareArtifact.objects.filter(pk__in=artifacts):
        if Upload.objects.filter(artifact=artifact).exists():
            continue  # another upload holds the same bytes
        catalogue = catalogue or Catalogue(config)
        path = f"{dev_reset.MEDIA_ROOT}/{artifact.storage_key}"
        argv, env = catalogue.ssh_argv(catalogue.resolve("dashboard"), f"sudo -n -u {dev_reset.MEDIA_USER} rm -f {shlex.quote(path)}",
                                       interactive=False)  # fmt: skip
        if subprocess.run(argv, env=env, capture_output=True, timeout=120).returncode != 0:
            output.warn(f"{path} not removed from the dashboard VM")
        try:
            artifact.delete()
        except IntegrityError:
            output.warn(f"artifact {artifact.content_hash[:12]} kept: something else refers to it")


def discard(config, made: Made) -> None:
    """Everything `made` holds taken away again, as if the round never was. Ctrl-C is ignored
    meanwhile: a half-done discard would leave the things it is there to remove."""
    from core import events
    from core.models.competition import Round
    from core.models.team import Team, TeamMembership
    from core.models.user import User
    from django.db import transaction
    from worms_cli import rounds

    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    print(output.heading("Ctrl-C: the dev test goes, as if it never was (Ctrl-C is ignored until it is gone)"))
    try:
        if made.ran:
            try:
                rounds.teardown(config, yes=True)  # the sandbox; an attempt left under way, closed
            except Exception as exc:  # noqa: BLE001  (said; the rest goes on)
                output.error(f"the sandbox: {exc}; later: `worms round teardown`")
            stopped = rounds.stopped_record().get("stopped", [])
            if stopped:  # not the devboxes: a dev test never starts them (rounds._start_again)
                session = SimpleNamespace(config=config, yes=True, round=made.round, dev_test=True)
                rounds._start_again(session, stopped)
                rounds._close_record(session)
        if made.round is not None:
            with transaction.atomic():
                if Round.objects.select_for_update().filter(pk=made.round.pk).exists():
                    made.round.delete_with_records()
            print(output.status_cell("ok", f"round {made.round.id} {made.round.name!r}, with its attempts and their records"))
        if made.uploads:
            _remove_uploads(config, made.uploads)
            print(output.status_cell("ok", f"{len(made.uploads)} upload(s) and their zips"))
        TeamMembership.objects.filter(team__in=made.teams).delete()
        Team.objects.filter(pk__in=[team.pk for team in made.teams]).delete()
        User.objects.filter(pk__in=[user.pk for user in made.users]).delete()
        if made.teams or made.users:
            print(output.status_cell("ok", f"{len(made.teams)} team(s), {len(made.users)} user(s)"))
        if made.round is not None:
            events.platform("dev_test_discarded", actor="console", detail={"round": made.round.id})
    finally:
        signal.signal(signal.SIGINT, previous)


def run(config) -> int:
    from worms_cli import rounds
    from worms_cli.session import for_config

    environment = for_config(config).environment()
    if environment.get("name") != "development":
        where = f"{environment.get('name')} ({environment.get('public_name') or environment.get('host')})"
        if not ASK(f"This is {where}, not worms-dev. Run a development command here?", False):
            return 1
    try:
        gameplan, duration = _gameplan(Path(config.repo_root) / "gameplans")
        print(output.heading("the uploads"))
        solutions = Path(config.repo_root) / "solutions"
        zips = [_zip(solutions, "an upload zip")]
        while True:  # the list again after each; left empty, asked whether another comes
            picked = _zip(solutions, "another upload zip", empty="no more?")
            if picked is not None:
                zips.append(picked)
            elif not ASK("Include another upload?", False):
                break
    except (KeyboardInterrupt, EOFError):
        print("\nnothing made")
        return 130
    made = Made()
    try:
        _make(config, made, gameplan, duration, zips)
        made.ran = True
        args = Namespace(round=str(made.round.id), yes=True, resume=False, team=[], dev_test=True, replay=False)
        code = rounds.start(config, args)
        while code != 130 and ASK("Replay the same gameplan (its sandbox kept: no build, no teardown) with another or an "
                                  "updated zip?", False):  # fmt: skip
            if _replay(config, made, solutions):
                args.replay = True
                code = rounds.start(config, args)
    except KeyboardInterrupt:
        code = 130
    if code == 130:
        discard(config, made)
    else:
        finish(config, made)
    return code
