"""Staff: Gameplans -- every version of every gameplan, by SHA-256, and what ran it
(docs/05-live-environment.md, "Versions").

What the orchestrator recorded (core/models/rounds.py): the files now, every version it has
seen (never its bytes: only the orchestrator and the admin may read them), the rounds and
attempts that pinned each, and the history of the files. A run pinned before versions were
recorded, whose file has changed since, is listed by its hash: its bytes are not known.
"""

from __future__ import annotations

from collections import defaultdict

from core.models.rounds import GameplanChange, GameplanFile, GameplanVersion, RoundRun
from dashboard.views.decorators import staff_required
from dashboard.views.util import render_with_base_info
from django.db.models import Count, Max, Min
from django.http import HttpRequest

HISTORY = 200  # the latest changes shown


def usage():
    """{sha256: [{round, uuid, attempts, teams, first, last}]}: the rounds whose runs pinned
    each version, oldest first."""
    rows = (
        RoundRun.objects.exclude(gameplan_sha256="")
        .values("gameplan_sha256", "round__uuid", "round__name")
        .annotate(attempts=Count("id"), teams=Count("team", distinct=True), first=Min("created_at"), last=Max("created_at"))
        .order_by("first")
    )
    out = defaultdict(list)
    for row in rows:
        out[row["gameplan_sha256"]].append({
            "round": row["round__name"], "uuid": row["round__uuid"], "attempts": row["attempts"],
            "teams": row["teams"], "first": row["first"], "last": row["last"],
        })  # fmt: skip
    return out


@staff_required
def gameplans(request: HttpRequest):
    used = usage()
    files = list(GameplanFile.objects.order_by("filename").values("filename", "valid", "checked_at", "version_id"))
    current = defaultdict(list)
    for entry in files:
        current[entry["version_id"]].append(entry["filename"])
    # Every column but the bytes (the dashboard may not read them).
    versions = list(
        GameplanVersion.objects.order_by("-first_seen_at", "sha256").values(
            "sha256", "filename", "size", "description", "first_seen_at", "recorded_by"
        )
    )
    recorded = set()
    for version in versions:
        recorded.add(version["sha256"])
        version["rounds"] = used.get(version["sha256"], [])
        version["attempts"] = sum(r["attempts"] for r in version["rounds"])
        version["current_as"] = current.get(version["sha256"], [])
    unrecorded = [{"sha256": sha, "rounds": rounds} for sha, rounds in used.items() if sha not in recorded]
    history = list(GameplanChange.objects.order_by("-seen_at", "-id").values("filename", "version_id", "seen_at", "recorded_by")[:HISTORY])
    return render_with_base_info(request, "staff/gameplans.html", {
        "files": files, "versions": versions, "unrecorded": unrecorded, "history": history, "history_limit": HISTORY,
    })  # fmt: skip
