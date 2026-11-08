"""A round's schedule: one source for the dashboard (staff create rounds, see whether every
team fits) and the conductor (runs them).

A round names its gameplan (a file the orchestrator and the node hold; docs/05-live-environment.md)
and its duration per team: seconds from a team's worm starting to the end of its run. The
rest of a team's slot is the platform's: the fuse before the worm starts, the drain after
the run, and an allowance for building, checking and tearing down the sandbox
(`slot_overhead`). Runs are sequential (one sandbox fits on the host), so a round needs one
slot per team, more if an attempt fails and the team's worm runs again.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

# Seconds from a team's worm starting to the end of its run: (default, low, high). The same
# bounds as the gameplan framework's (src/gameplan/definition.py, LIMITS["round_duration"]).
TEAM_DURATION = (3600, 60, 7 * 86400)

TIMING_DEFAULTS = {"worm_start_delay": 15, "end_drain": 120, "slot_overhead": 900}


def timing_defaults() -> dict:
    return {**TIMING_DEFAULTS, **{k: v for k, v in getattr(settings, "ROUND_TIMING", {}).items() if k in TIMING_DEFAULTS}}


def slot_seconds(team_duration: int) -> int:
    """How long one team's run occupies the host, start to teardown."""
    timing = timing_defaults()
    return int(timing["worm_start_delay"] + team_duration + timing["end_drain"] + timing["slot_overhead"])


@dataclass
class Estimate:
    teams: int
    slot: int  # seconds per team
    needed: int  # seconds for every team
    available: int | None  # the round's length; None without dates

    @property
    def fits(self) -> bool:
        return self.available is None or self.needed <= self.available

    @property
    def max_teams(self) -> int | None:
        if self.available is None or self.slot <= 0:
            return None
        return self.available // self.slot


def estimate(round_start, round_end, team_duration: int, teams: int) -> Estimate:
    slot = slot_seconds(team_duration)
    available = None
    if round_start is not None and round_end is not None:
        available = max(0, int((round_end - round_start).total_seconds()))
    return Estimate(teams=teams, slot=slot, needed=slot * teams, available=available)


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours} h {minutes} min"
    if hours:
        return f"{hours} h"
    return f"{minutes} min"
