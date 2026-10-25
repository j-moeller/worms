"""Create the RoundRuns for a round: one per team, in a deterministic order,
each with the team's latest upload before the round's deadline (round_start).

A team without an upload before the deadline still gets a run, in state
PENDING with no upload; the conductor marks it SKIPPED. This keeps the running
order stable and visible.
"""

from __future__ import annotations

import random

from core.models.rounds import RoundRun
from core.models.team import Team
from core.models.worms import Upload
from django.db import transaction
from django.db.models import OuterRef, Subquery


def plan_round(round, base_seed=None, sandbox_params=None):
    """Create the missing runs for `round`. Idempotent: existing runs are left
    as they are. Returns the runs in running order. Every run gets the round's
    parameters (sandbox size, game plan, timing; set on the dashboard)."""
    base_seed = base_seed if base_seed is not None else round.seed
    if sandbox_params is None:
        sandbox_params = round.sandbox_params or {}
    latest_upload = (
        Upload.objects.filter(
            team=OuterRef("pk"),
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
                # Per-run seed, derived but distinct, so every sandbox differs.
                seed=(base_seed * 1_000_003 + team.id) % (2**63),
                sandbox_params=sandbox_params or {},
            )
    return list(RoundRun.objects.filter(round=round).order_by("position"))
