"""The orchestrator's copy of the gameplans (docs/05-live-environment.md), and the list the dashboard
picks from.

The files are deployed to the orchestrator VM only (WORMS_GAMEPLAN_DIR, with the framework
in WORMS_GAMEPLAN_LIB): the dashboard VM has none. Each check records what the orchestrator's
copy is (core/models/rounds.py):

  gameplan_versions  every version by SHA-256, its bytes, its description for teams; immutable
  gameplan_changes   each time a file appears, becomes another version, or goes
  gameplans          the files now, valid or not, and their versions
  round_runs         the version each run pinned (gameplan_version)

The dashboard reads all of it but a version's bytes, which only the orchestrator and the admin
may read. Why a gameplan is not valid goes to the orchestrator's journal, never to the database.

A gameplan is valid when the orchestrator's copy and the host's (the round agent's
`gameplans` job) are the same file (SHA-256) and both find nothing wrong with it. A round's
gameplan is checked against its duration per team before the round starts, and pinned (its
SHA-256 on every attempt) when its first attempt starts: every team runs the same version.
"""

from __future__ import annotations

import logging
import sys

from core.models.rounds import GameplanChange, GameplanFile, GameplanSimulation, GameplanVersion, HostJob, RoundRun
from django.conf import settings
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

log = logging.getLogger("orchestrator")


class Unavailable(RuntimeError):
    """This machine has no gameplans (the dashboard VM, or a missing deployment)."""


def framework():
    """The gameplan framework's modules (src/gameplan in the repository)."""
    lib = getattr(settings, "GAMEPLAN_LIB", "")
    if lib and lib not in sys.path:
        sys.path.insert(0, lib)
    try:
        from gameplan import loader, runtime
    except ImportError as error:
        raise Unavailable(f"the gameplan framework is not installed here ({error})") from None
    return loader, runtime


def directory():
    path = getattr(settings, "GAMEPLAN_DIR", "")
    if not path:
        raise Unavailable("WORMS_GAMEPLAN_DIR is not set: gameplans exist on the orchestrator and the host only")
    return path


def load(filename, sha256=None):
    loader, _ = framework()
    return loader.load(directory(), filename, sha256)


def problems(filename, duration=None, sha256=None):
    """(sha256, [problems]) of the orchestrator's copy, for this duration per team."""
    loader, runtime = framework()
    try:
        loaded = loader.load(directory(), filename, sha256)
    except Exception as error:  # noqa: BLE001  (a file that does not load is a problem like any other)
        return None, [f"{type(error).__name__}: {error}"]
    return loaded.sha256, runtime.validate(loaded.cls, duration=duration)


def description(filename, source):
    """The description for teams (Gameplan.description) of these bytes of the file; "" if it
    has none, or if they do not describe a sandbox (refresh() logs why it is not valid)."""
    loader, runtime = framework()
    try:
        return runtime.description(loader.load_source(directory(), filename, source).cls)
    except Exception:  # noqa: BLE001  (the gameplan's own code; problems() reports it)
        return ""


def host_view():
    """{file: (sha256, valid)} from the host's latest `gameplans` job, or None if it has none."""
    job = HostJob.objects.filter(kind=HostJob.Kind.GAMEPLANS, state=HostJob.State.DONE).order_by("-id").first()
    if job is None:
        return None
    out = {}
    for entry in (job.result or {}).get("gameplans") or []:
        if isinstance(entry, dict) and isinstance(entry.get("file"), str):
            out[entry["file"]] = (str(entry.get("sha256") or ""), entry.get("valid") is True)
    return out


def refresh(host):
    """Record the orchestrator's copy (its versions, what changed, the files now) and check
    each file against the host's view (`host`: host_view()). Returns {file: sha256} of the
    valid ones. Each file is read once: the version recorded is those bytes, and the check
    loads the file by that hash (a file changed meanwhile fails it until the next check)."""
    loader, _ = framework()
    current = {}
    for name in loader.available(directory()):
        try:
            current[name] = loader.read(directory(), name)
        except Exception as error:  # noqa: BLE001  (too large, not a regular file, gone meanwhile)
            log.warning("gameplan %s: %s", name, error)
    valid = {}
    for name, (_, sha) in current.items():
        _, found = problems(name, sha256=sha)
        theirs = (host or {}).get(name)
        if theirs is None:
            found = found + ["the host has no such file (is the agent's workspace synced?)"]
        elif theirs[0] != sha:
            found = found + ["the host's copy is a different version: deploy one checkout to both"]
        elif not theirs[1]:
            found = found + ["the host finds it invalid: its gameplans job log on the host (/var/log/worms/jobs/) says why"]
        for problem in found:
            log.warning("gameplan %s: %s", name, problem)
        if not found:
            valid[name] = sha
    now = timezone.now()
    with transaction.atomic():
        record(current)
        GameplanFile.objects.exclude(filename__in=current).delete()
        GameplanSimulation.objects.exclude(filename__in=current).delete()
        for name, (_, sha) in current.items():
            GameplanFile.objects.update_or_create(
                filename=name, defaults={"valid": name in valid, "checked_at": now, "version_id": sha}
            )
    return valid


def record(current):
    """Record versions not seen before and what changed ({file: (bytes, sha256)}), and link the
    runs that pinned a version recorded only now. Plain INSERTs of the columns the orchestrator
    fills: the time and the writing account are the database's, and the rows are never changed
    (migration 0019)."""
    known = set(GameplanVersion.objects.filter(sha256__in=[sha for _, sha in current.values()]).values_list("sha256", flat=True))
    new = {}
    for name, (source, sha) in sorted(current.items()):
        if sha not in known and sha not in new:
            new[sha] = (name, source, description(name, source))
    latest = {}
    for filename, sha in GameplanChange.objects.order_by("seen_at", "id").values_list("filename", "version_id"):
        latest[filename] = sha
    changes = [(name, sha) for name, (_, sha) in sorted(current.items()) if name not in latest or latest[name] != sha]
    changes += [(name, None) for name, sha in sorted(latest.items()) if name not in current and sha is not None]
    with connection.cursor() as cursor:
        for sha, (name, source, text) in new.items():
            cursor.execute(
                "INSERT INTO gameplan_versions (sha256, filename, content, size, description) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (sha256) DO NOTHING",
                [sha, name, source, len(source), text],
            )
            log.info("gameplan %s: version %s recorded (%d bytes)", name, sha[:12], len(source))
        for name, sha in changes:
            cursor.execute("INSERT INTO gameplan_changes (filename, sha256) VALUES (%s, %s)", [name, sha])
    # Runs pinned before their version was recorded (before migration 0019): linked once it is.
    RoundRun.objects.filter(gameplan_version__isnull=True, gameplan_sha256__in=GameplanVersion.objects.values("sha256")).update(
        gameplan_version_id=F("gameplan_sha256")
    )


def check_round(round_, valid):
    """'ok' or 'invalid' for a round's gameplan at its duration per team (`valid`: refresh()),
    "" for a round that has none yet."""
    if not round_.gameplan:
        return ""
    if round_.gameplan not in valid:
        log.warning("round %s: its gameplan %s is not valid", round_.name, round_.gameplan)
        return "invalid"
    _, found = problems(round_.gameplan, duration=round_.team_duration, sha256=valid[round_.gameplan])
    for problem in found:
        log.warning("round %s, gameplan %s: %s", round_.name, round_.gameplan, problem)
    return "invalid" if found else "ok"
