"""Create the RoundRuns for a round: one per team (its first attempt), in a deterministic
order, each with the team's latest upload before the round's deadline (round_start), the
round's gameplan and its duration per team. An upload made in the dev view is not an entry:
it only ever runs on the devbox. The conductor pins the gameplan's version when the first
attempt starts, and adds an attempt when one fails (conductor.py).

A team without an upload before the deadline still gets a run, in state
PENDING with no upload; the conductor marks it SKIPPED. This keeps the running
order stable and visible.
"""

from __future__ import annotations

import random

from core.locking import serialized_resource
from core.models.competition import Round
from core.models.rounds import RoundRun
from core.models.team import Team
from core.models.worms import Upload
from django.db import transaction
from django.db.models import OuterRef, Subquery


def plan_round(round, base_seed=None):
    """Create the missing runs for `round`. Idempotent: existing runs are left
    as they are. Returns the first attempts in running order."""
    # Staff editing/deletion and other planner processes use the same lock.
    # Re-read the round after waiting: its seed/window may have changed.
    with serialized_resource("round-plan", round.pk):
        current = Round.objects.filter(pk=round.pk).first()
        if current is None:
            return []
        return _plan_round(current, base_seed)


def _plan_round(round, base_seed):
    base_seed = base_seed if base_seed is not None else round.seed
    latest_upload = (
        Upload.objects.filter(
            team=OuterRef("pk"),
            devbox=False,
            created_at__lt=round.round_start,
        )
        .order_by("-created_at", "-id")
        .values("id")[:1]
    )
    teams = list(
        Team.objects.filter(competition=round.competition)
        .annotate(latest_upload_id=Subquery(latest_upload))
        .order_by("id")
    )
    # Deterministic running order from the round seed (not alphabetical, so the
    # order cannot be gamed by team name).
    order = list(range(len(teams)))
    random.Random(base_seed).shuffle(order)

    with transaction.atomic():
        existing = set(RoundRun.objects.filter(round=round).values_list("team_id", flat=True))
        for position, team_index in enumerate(order):
            team = teams[team_index]
            if team.id in existing:
                continue
            RoundRun.objects.create(
                round=round,
                team=team,
                upload_id=team.latest_upload_id,
                position=position,
                # Per-run seed, derived but distinct (kept for the record; the sandbox is the
                # gameplan's, the same for every team).
                seed=(base_seed * 1_000_003 + team.id) % (2**63),
                gameplan=round.gameplan,
                duration=round.team_duration,
            )
    return list(RoundRun.objects.filter(round=round, attempt=0).order_by("position"))
