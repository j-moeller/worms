"""A round's parameters and its schedule: one source for the dashboard (staff
create rounds, see whether every team fits) and the conductor (runs them).

A round's `sandbox_params` (copied into every RoundRun when its runs are
planned) holds

    {"vms": 8, "networks": 4, "sites": 1, "max_interfaces": 3,   # the sandbox
     "game_plan": {...},                                          # gameplan.py
     "timing": {"worm_start_delay": 300, "run_duration": 3600}}   # one run

The sandbox keys are generator parameters (scripts/generate_sandbox.py); the
round agent validates them again with the same bounds
(scripts/round_agent/validation.py:sandbox_params, tests/test_staff_rounds.py
checks that the two agree). Everything missing falls back to the defaults
below and to settings.ROUND_TIMING.

Runs are sequential (one sandbox fits on the host), so a round needs one slot
per team: the fuse before the worm starts, the run itself, the drain after it
and an allowance for building and tearing down the sandbox (`slot_overhead`).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

# Generator parameters: (default, low, high). Keep the bounds equal to the
# round agent's validator.
SANDBOX_BOUNDS = {
    "vms": (8, 1, 250),
    "networks": (4, 1, 30),
    "sites": (1, 1, 8),
    "max_interfaces": (3, 1, 8),
}

# Game plan parameters (orchestrator/rounds/gameplan.py DEFAULTS): (low, high).
GAME_PLAN_BOUNDS = {
    "initial_fraction": (0.0, 1.0),
    "first_stop_after": (0, 86400),
    "stop_interval": (1, 86400),
    "min_alive": (0, 250),
    "restart_after": (1, 86400),
}

# Timing of one run (seconds): (low, high).
TIMING_BOUNDS = {
    "worm_start_delay": (0, 86400),
    "run_duration": (60, 86400),
}

TIMING_DEFAULTS = {"worm_start_delay": 300, "run_duration": 3600, "end_drain": 120, "slot_overhead": 900}


def timing_defaults() -> dict:
    return {**TIMING_DEFAULTS, **getattr(settings, "ROUND_TIMING", {})}


def run_timing(sandbox_params: dict | None) -> dict:
    """The timing of one run: the round's own values over the defaults."""
    timing = dict(timing_defaults())
    own = (sandbox_params or {}).get("timing") or {}
    for key in TIMING_BOUNDS:
        if isinstance(own.get(key), int) and not isinstance(own.get(key), bool):
            timing[key] = own[key]
    return timing


def slot_seconds(timing: dict) -> int:
    """How long one team's run occupies the host, start to teardown."""
    return int(timing["worm_start_delay"] + timing["run_duration"] + timing["end_drain"] + timing["slot_overhead"])


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


def estimate(round_start, round_end, sandbox_params: dict | None, teams: int) -> Estimate:
    slot = slot_seconds(run_timing(sandbox_params))
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
