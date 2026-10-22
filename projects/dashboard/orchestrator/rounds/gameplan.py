"""Game plans: when which sandbox VM is stopped and when which secret appears.

The point of a game plan is to tell worms from remote-controlled scripts: a
program that sits on its start VM and drives everything from there loses
when that VM is switched off, and VMs whose secrets only appear later can only
be captured by a worm that is still alive somewhere else by then.

The default plan (generate()):

  1. A start VM is chosen; the worm is deployed there.
  2. Before the worm starts, the secrets of the start VM and a share of the
     other VMs (`initial_fraction`) are released.
  3. `first_stop_after` seconds after the start, the start VM is switched
     off; then every `stop_interval` seconds another VM, as long as more than
     `min_alive` VMs are running.
  4. The remaining ("delayed") secrets are released right after those stops,
     spread over them: a delayed secret appears only once a VM has been
     stopped (e.g. VM B's secret after VM A's stop). VMs with a secret still
     to come are never stopped before it appears.
  5. Optionally (`restart_after`), stopped VMs are started again later.

Deterministic: the same VMs, seed and parameters give the same plan. Pure
Python, no database: tests/test_rounds.py.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

DEFAULTS = {
    "initial_fraction": 0.5,
    "first_stop_after": 600,
    "stop_interval": 600,
    "min_alive": 2,
    "restart_after": None,
}


@dataclass
class Step:
    position: int
    action: str  # release_secret | stop_vm | start_vm
    vm: str  # VM name
    at_seconds: int | None  # after the worm's start; None = before it
    after: list[int] = field(default_factory=list)


@dataclass
class Plan:
    start_vm: str
    steps: list[Step]


def generate(vms: list[str], seed: int, duration: int, params: dict | None = None) -> Plan:
    if not vms:
        raise ValueError("a game plan needs at least one VM")
    p = {**DEFAULTS, **(params or {})}
    rng = random.Random(seed)
    names = sorted(vms)
    start = rng.choice(names)
    others = [name for name in names if name != start]
    rng.shuffle(others)

    initial_count = round(len(others) * float(p["initial_fraction"]))
    initial = [start, *others[:initial_count]]
    delayed = others[initial_count:]

    steps: list[Step] = []

    def add(action, vm, at, after=()):
        step = Step(len(steps), action, vm, at, list(after))
        steps.append(step)
        return step

    for name in initial:
        add("release_secret", name, None)

    # How many stops fit, to spread the delayed secrets over them.
    first, interval, min_alive = int(p["first_stop_after"]), int(p["stop_interval"]), int(p["min_alive"])
    possible_stops = max(0, min(len(names) - min_alive, (duration - first - 1) // interval + 1 if duration > first else 0))
    per_stop = math.ceil(len(delayed) / possible_stops) if possible_stops else 0

    alive = set(names)
    pending = list(delayed)
    stops: list[Step] = []
    victim, at = start, first
    while at < duration and len(alive) > min_alive and victim is not None:
        stop = add("stop_vm", victim, at)
        stops.append(stop)
        alive.discard(victim)
        for _ in range(per_stop):
            if pending:
                add("release_secret", pending.pop(0), at, [stop.position])
        if p["restart_after"] is not None and at + int(p["restart_after"]) < duration:
            add("start_vm", victim, at + int(p["restart_after"]), [stop.position])
        at += interval
        candidates = sorted(name for name in alive if name not in pending)
        victim = rng.choice(candidates) if candidates else None

    # Not enough stops for every delayed secret: the rest after the last stop,
    # or half-way through if nothing is ever stopped.
    for name in pending:
        if stops:
            add("release_secret", name, stops[-1].at_seconds, [stops[-1].position])
        else:
            add("release_secret", name, duration // 2)

    return Plan(start, steps)
