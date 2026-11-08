"""The orchestrator's copy of the gameplans (docs/05-live-environment.md), and the list the dashboard
picks from.

The files are deployed to the orchestrator VM only (WORMS_GAMEPLAN_DIR, with the framework
in WORMS_GAMEPLAN_LIB): the dashboard VM has none, and the dashboard reads nothing but the
`gameplans` table (file names, valid or not) and a round's gameplan_status. Why a gameplan
is not valid goes to the orchestrator's journal, never to the database.

A gameplan is valid when the orchestrator's copy and the node's (the round agent's
`gameplans` job) are the same file (SHA-256) and both find nothing wrong with it. A round's
gameplan is checked against its duration per team before the round starts, and pinned (its
SHA-256 on every attempt) when its first attempt starts: every team runs the same version.
"""

from __future__ import annotations

import logging
import sys

from core.models.rounds import GameplanFile, HostJob
from django.conf import settings
from django.db import transaction
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
        raise Unavailable("WORMS_GAMEPLAN_DIR is not set: gameplans exist on the orchestrator and the node only")
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


def node_view():
    """{file: (sha256, valid)} from the node's latest `gameplans` job, or None if it has none."""
    job = HostJob.objects.filter(kind=HostJob.Kind.GAMEPLANS, state=HostJob.State.DONE).order_by("-id").first()
    if job is None:
        return None
    out = {}
    for entry in (job.result or {}).get("gameplans") or []:
        if isinstance(entry, dict) and isinstance(entry.get("file"), str):
            out[entry["file"]] = (str(entry.get("sha256") or ""), entry.get("valid") is True)
    return out


def refresh(node):
    """Rewrite the `gameplans` table from the orchestrator's copy and the node's view (`node`:
    node_view()). Returns {file: sha256} of the valid ones."""
    loader, _ = framework()
    valid = {}
    for name in loader.available(directory()):
        sha, found = problems(name)
        theirs = (node or {}).get(name)
        if theirs is None:
            found = found + ["the node has no such file (is the agent's workspace synced?)"]
        elif theirs[0] != sha:
            found = found + ["the node's copy is a different version: deploy one checkout to both"]
        elif not theirs[1]:
            found = found + ["the node finds it invalid: its gameplans job log on the node (/var/log/worms/jobs/) says why"]
        for problem in found:
            log.warning("gameplan %s: %s", name, problem)
        if not found:
            valid[name] = sha
    now = timezone.now()
    with transaction.atomic():
        GameplanFile.objects.exclude(filename__in=loader.available(directory())).delete()
        for name in loader.available(directory()):
            GameplanFile.objects.update_or_create(filename=name, defaults={"valid": name in valid, "checked_at": now})
    return valid


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
