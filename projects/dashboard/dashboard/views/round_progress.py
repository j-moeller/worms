"""Staff: where the rounds are, read from what the orchestrator recorded
(docs/05-live-environment.md, "Following a round"). Nothing here writes to the database.

  phase(...)     what a round is doing: scheduled, planning, waiting (no gameplan, or not a valid
                 one), queued (behind the round under way), starting, running, done
  warnings(...)  the rounds not over whose gameplan is missing or not valid: on every staff page
  progress(...)  a round's annotated timeline: every team's attempts on one time axis, the
                 current attempt's state and the gameplan's steps so far, the flags of the teams
                 that finished, and the attempts that failed and ran again

The gameplan's code is never read: what it did is in the run's events and host jobs.
Times of runs are wall-clock times (core/models/rounds.py); a round's window is compared with
the dashboard's clock, as the header does.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta

from core import clock, schedule
from core.models.competition import Round
from core.models.rounds import HostJob, RoundRun, RunEvent
from core.models.scoring import FlagCapture
from django.db.models import Q
from django.utils import timezone

S = RoundRun.State
ACTIVE = (S.PREPARING, S.CHECKING, S.RUNNING, S.ENDING, S.TEARING_DOWN)
OPEN = RoundRun.OPEN_STATES

# An attempt's steps, as the stepper shows them.
STEPS = [
    (S.PREPARING, "Build", "the sandbox built, or rolled back to its snapshot; the run's flags and files"),
    (S.CHECKING, "Checks", "the host's firewalls; every VM and the gateway started in this attempt and came up (cloud-init, "
                           "boot, addresses, route, sshd, users, services; the gateway's rules); then each VM probed from inside"),
    (S.RUNNING, "Gameplan", "the gameplan's execute(): the worm deployed, flags, power, files"),
    (S.ENDING, "Drain", "every VM stopped; the monitor catches up"),
    (S.TEARING_DOWN, "Teardown", "the sandbox destroyed, or kept for the next team"),
    (S.FINISHED, "Done", "the attempt's flags count"),
]

EVENT_TEXT = {
    RunEvent.Kind.WORM_DEPLOYED: "worm deployed on {vm}: the fuse is lit",
    RunEvent.Kind.WORM_STARTED: "worm started",
    RunEvent.Kind.FLAG_REMOVED: "flag removed from {vm}",
    RunEvent.Kind.SECRET_RELEASED: "flag placed on {vm}",
    RunEvent.Kind.VM_STOPPED: "{vm} stopped",
    RunEvent.Kind.VM_STARTED: "{vm} started",
    RunEvent.Kind.VM_SUSPENDED: "{vm} paused",
    RunEvent.Kind.VM_RESUMED: "{vm} resumed",
    RunEvent.Kind.CAPTURED: "{vm} captured",
    RunEvent.Kind.SKIPPED: "step skipped: {message}",
    RunEvent.Kind.ERROR: "error: {message}",
}
STATE_TEXT = {
    S.PREPARING: "attempt {attempt}: building the sandbox (or rolling it back)",
    S.CHECKING: "checks: every VM started in this attempt and came up as it should, then probed from inside",
    S.RUNNING: "the gameplan runs",
    S.ENDING: "run over: every VM stopped, draining",
    S.TEARING_DOWN: "tearing the sandbox down",
    S.FINISHED: "finished",
    S.FAILED: "failed",
    S.SKIPPED: "skipped: no upload before the start",
}


@dataclass
class Phase:
    key: str
    label: str
    text: str
    tone: str = ""  # a badge/alert modifier: ok, warn, bad, accent


def under_way():
    """The round whose attempts run now or are pinned to come (one at a time), or None."""
    run = (
        RoundRun.objects.filter(Q(state__in=ACTIVE) | (Q(state=S.PENDING) & ~Q(gameplan_sha256="")))
        .select_related("round").order_by("round__round_start", "round_id").first()
    )  # fmt: skip
    return run.round if run else None


def phase(round_, runs, window_now, current=None) -> Phase:
    """What the round is doing. `runs`: its runs (state, gameplan_sha256, upload_id);
    `current`: the round under way (under_way())."""
    start = round_.round_start
    if start is None:
        return Phase("undated", "undated", "No dates: it never starts.")
    if not runs:
        if window_now < start:
            return Phase("scheduled", "scheduled", "Nothing is built before its start.")
        return Phase("planning", "planning", "Its start has passed: the orchestrator plans its runs.", "accent")
    if not any(run.state in OPEN for run in runs):
        return Phase("done", "done", "Every team's run is over.", "ok")
    if any(run.state in ACTIVE or (run.state == S.PENDING and run.gameplan_sha256) for run in runs):
        return Phase("running", "running", "Its gameplan is pinned; the teams run one after another.", "accent")
    if not any(run.state == S.PENDING and run.upload_id for run in runs):
        return Phase("running", "running", "No team left has an upload: each is skipped.", "accent")
    if not round_.gameplan:
        return Phase("waiting", "waiting for a gameplan", "It has no gameplan: it waits, and the rounds after it go ahead. "
                     "Choose one on its page; the orchestrator looks every minute or so.", "bad")  # fmt: skip
    if round_.gameplan_status == "invalid":
        return Phase("waiting", "gameplan not valid", f"Its gameplan {round_.gameplan} is not valid: it waits, and the "
                     "rounds after it go ahead. The orchestrator's journal says why.", "bad")  # fmt: skip
    if current is not None and current.pk != round_.pk:
        return Phase("queued", "queued", f"Its gameplan is ready: it begins when {current.name} is over.", "warn")
    return Phase("starting", "starting", "Its gameplan is ready: it begins at the orchestrator's next look.", "accent")


def warnings(competition) -> list[dict]:
    """[{round, phase, text, tone}]: the rounds of the competition not over whose gameplan is
    missing or not valid, earliest first."""
    window_now = clock.now()
    out = []
    rounds = Round.objects.filter(competition=competition, round_start__isnull=False).filter(Q(gameplan="") | Q(gameplan_status="invalid"))
    for round_ in rounds.order_by("round_start", "id"):
        runs = list(round_.runs.only("state", "gameplan_sha256", "upload_id"))
        ph = phase(round_, runs, window_now)
        if ph.key == "scheduled":
            when = f"{round_.round_start:%Y-%m-%d %H:%M} UTC"
            if not round_.gameplan:
                text = (f"has no gameplan. It starts {when}: if it has none by then, its runs are planned with the "
                        "uploads from before the start, and it waits for one, alone.")  # fmt: skip
            else:
                text = f"has a gameplan that is not valid ({round_.gameplan}). Choose a valid one before {when}, or it waits."
            out.append({"round": round_, "phase": ph, "text": text, "tone": "warn"})
        elif ph.key in ("planning", "waiting"):
            since = f"{round_.round_start:%Y-%m-%d %H:%M} UTC"
            what = "has no gameplan" if not round_.gameplan else f"has a gameplan that is not valid ({round_.gameplan})"
            looked = f" The orchestrator last looked at {round_.gameplan_checked_at:%H:%M} UTC." if round_.gameplan_checked_at else ""
            text = (f"{what} and waits since its start ({since}). Its teams' uploads from before the start are kept; "
                    f"the rounds after it go ahead.{looked}")  # fmt: skip
            out.append({"round": round_, "phase": ph, "text": text, "tone": "error"})
    return out


# -- the timeline -------------------------------------------------------------------------------


def _pct(value):
    return f"{max(0.0, min(100.0, value)):.3f}%"


def _clock(seconds):
    """+m:ss or +h:mm:ss."""
    sign = "-" if seconds < 0 else "+"
    seconds = int(abs(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{sign}{hours}:{minutes:02d}:{secs:02d}" if hours else f"{sign}{minutes}:{secs:02d}"


def _span(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} s"
    hours, rest = divmod(seconds, 3600)
    days, hours = divmod(hours, 24)
    minutes = rest // 60
    if days:
        return f"{days} d {hours} h"
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


class Axis:
    """Times to percentages of a time axis, with a few labelled ticks (UTC)."""

    def __init__(self, start, end):
        self.start = start
        self.seconds = max(1.0, (end - start).total_seconds())
        self.end = start + timedelta(seconds=self.seconds)

    def at(self, when):
        return (when - self.start).total_seconds() / self.seconds * 100

    def block(self, start, end, **extra):
        left = max(0.0, self.at(start))
        width = max(0.4, min(100.0, self.at(end)) - left)
        return {"x": _pct(left), "w": _pct(width), "mid": _pct(left + width / 2), "wide": width >= 7, **extra}

    def mark(self, when, **extra):
        return {"x": _pct(self.at(when)), **extra}

    def ticks(self, count=5):
        fmt = "%m-%d %H:%M" if self.seconds > 86400 else "%H:%M"
        out = []
        for i in range(count):
            when = self.start + timedelta(seconds=self.seconds * i / (count - 1))
            anchor = "start" if i == 0 else "end" if i == count - 1 else "middle"
            out.append({"x": _pct(100 * i / (count - 1)), "text": when.strftime(fmt), "anchor": anchor})
        return out


def progress(round_, now=None, window_now=None, current=None):
    """The round's progress, for the staff pages; None before its runs are planned."""
    now = now or timezone.now()
    window_now = window_now or clock.now()
    runs = list(RoundRun.objects.filter(round=round_).select_related("team").order_by("position", "attempt"))
    current = current if current is not None else under_way()
    ph = phase(round_, runs, window_now, current)
    if not runs:
        return None if ph.key in ("scheduled", "undated") else {"phase": ph, "round": round_, "teams": [], "updated": now}
    timing = schedule.timing_defaults()
    ids = [run.pk for run in runs]

    entered = defaultdict(list)  # run -> [(ts, state)]
    for run_id, ts, data in RunEvent.objects.filter(run_id__in=ids, kind=RunEvent.Kind.STATE).order_by("ts", "id").values_list("run_id", "ts", "data"):
        entered[run_id].append((ts, (data or {}).get("state", "")))
    captured = Counter(FlagCapture.objects.filter(round=round_).values_list("vm__run_id", flat=True))

    def began(run):
        for ts, state in entered[run.pk]:
            if state == S.PREPARING:
                return ts
        return None

    # -- every team, in running order ---------------------------------------------------------
    by_team = defaultdict(list)
    for run in runs:
        by_team[run.position].append(run)
    active = next((run for run in runs if run.state in ACTIVE), None)
    begins = ph.key in ("running", "starting")  # the next team is known to be next
    upcoming = next((run for run in runs if run.state == S.PENDING), None) if active is None and begins else None
    teams, flags = [], 0
    for position in sorted(by_team):
        attempts = by_team[position]
        last = attempts[-1]
        finished = next((a for a in attempts if a.state == S.FINISHED), None)
        if finished is not None:
            flags += captured.get(finished.pk, 0)
        teams.append({
            "position": position + 1,
            "team": last.team,
            "state": last.state,
            "flags": captured.get(finished.pk, 0) if finished else None,
            "flags_so_far": captured.get(last.pk, 0) if last.state in ACTIVE else None,
            "attempts": [{"n": a.attempt + 1, "state": a.state, "error": a.error} for a in attempts],
            "restarts": len(attempts) - 1,
            "failed": [a for a in attempts if a.state == S.FAILED],
            "current": last is active,
            "next": last is upcoming,
            "done": last.state not in OPEN,
            "no_upload": last.upload_id is None,
        })  # fmt: skip
    done = sum(1 for team in teams if team["done"])

    # -- the round's time axis: what happened, what is under way, an estimate of what is left ----
    projected = []
    cursor = now
    if active is not None:
        start = began(active) or active.state_changed_at or now
        cursor = max(now, start + timedelta(seconds=schedule.slot_seconds(active.duration)))
    if begins:  # a round that waits, or is queued behind another, has no time to estimate from
        for run in runs:
            if run.state == S.PENDING and run.upload_id:
                slot = timedelta(seconds=schedule.slot_seconds(run.duration or round_.team_duration))
                projected.append((run, cursor, cursor + slot))
                cursor += slot
    ends = [run.finished_at for run in runs if run.finished_at]
    first_start = min((t for t in (began(run) for run in runs) if t), default=None)
    axis_end = max([round_.round_end, *ends, *([now, cursor] if ph.key != "done" else [])])
    axis = Axis(round_.round_start, axis_end)

    blocks, marks = [], []
    waited_until = first_start or (now if ph.key != "done" else None)
    waited = (waited_until - round_.round_start).total_seconds() if waited_until else 0
    if first_start is None and waited > 0 or waited > 300:
        span = _span(waited)
        if first_start is None:
            label, title = f"{ph.label} · {span}", f"Since the round's start, {span}: {ph.text}"
        else:
            label, title = f"waited · {span}", f"{span} from the round's start to its first attempt (no valid gameplan, or another round under way)"
        blocks.append(axis.block(round_.round_start, waited_until, cls="wait", label=label, title=title))
    for run in runs:
        start = began(run)
        if run.state == S.SKIPPED:
            if run.finished_at:
                marks.append(axis.mark(run.finished_at, cls="skip", title=f"#{run.position + 1} {run.team.name}: skipped, no upload"))
            continue
        if start is None:
            continue
        end = run.finished_at or now
        if run.state == S.FINISHED:
            n = captured.get(run.pk, 0)
            cls, title = "finished", f"#{run.position + 1} {run.team.name}, attempt {run.attempt + 1}: finished, {n} flag{'s' if n != 1 else ''}"
        elif run.state == S.FAILED:
            cls, title = "failed", f"#{run.position + 1} {run.team.name}, attempt {run.attempt + 1}: failed ({run.error[:160]})"
        else:
            cls, title = "active", f"#{run.position + 1} {run.team.name}, attempt {run.attempt + 1}: {run.state}"
        label = ("✕ " if run.state == S.FAILED else "") + run.team.name
        blocks.append(axis.block(start, end, cls=cls, label=label, title=f"{title} · {start:%H:%M}–{end:%H:%M} UTC"))
    for run, start, end in projected:
        label = f"{run.team.name}" + (f" (attempt {run.attempt + 1})" if run.attempt else "")
        blocks.append(axis.block(start, end, cls="planned", label=label,
                                 title=f"#{run.position + 1} {label}: to come, about {start:%H:%M}–{end:%H:%M} UTC (an estimate)"))  # fmt: skip
    marks.append(axis.mark(round_.round_end, cls="end", label="end", title=f"The round's end: {round_.round_end:%Y-%m-%d %H:%M} UTC"))
    if ph.key != "done":
        marks.append(axis.mark(now, cls="now", label="now", title=f"Now: {now:%H:%M} UTC"))

    out = {
        "round": round_,
        "phase": ph,
        "updated": now,
        "teams": teams,
        "teams_total": len(teams),
        "teams_done": done,
        "flags": flags,
        "restarts": sum(team["restarts"] for team in teams),
        "skipped": sum(1 for team in teams if team["state"] == S.SKIPPED),
        "timeline": {"blocks": blocks, "marks": marks, "ticks": axis.ticks(),
                     "summary": f"{round_.name}: {done} of {len(teams)} teams done, {ph.label}"},  # fmt: skip
        "estimate_end": cursor if projected or active else None,
        "late": bool(projected or active) and cursor > round_.round_end,
        "attempt": _attempt(active, entered[active.pk], captured.get(active.pk, 0), now, timing) if active else None,
        "upcoming": upcoming if ph.key == "running" else None,
    }
    return out


def _attempt(run, states, flags, now, timing):
    """The attempt under way: its steps, its own time axis with the gameplan's steps on it, and
    the log of what it did (events and file jobs, oldest first)."""
    began = next((ts for ts, state in states if state == S.PREPARING), run.state_changed_at or now)
    reached = {state: ts for ts, state in states}
    order = [step for step, _, _ in STEPS]
    at = order.index(run.state) if run.state in order else 0
    steps = [{"key": key, "label": label, "help": help_, "at": reached.get(key),
              "status": "done" if i < at else "current" if i == at else "todo"}
             for i, (key, label, help_) in enumerate(STEPS)]  # fmt: skip

    if run.ends_at:
        expected_end = run.ends_at + timedelta(seconds=timing["end_drain"] + 120)
    else:
        expected_end = began + timedelta(seconds=schedule.slot_seconds(run.duration))
    axis = Axis(began, max(now, expected_end))
    segments = []
    for i, (ts, state) in enumerate(states):
        if state not in reached or ts < began:
            continue
        end = states[i + 1][0] if i + 1 < len(states) else now
        label = next((label for key, label, _ in STEPS if key == state), state)
        segments.append(axis.block(ts, end, cls=f"step-{state}", label=label, title=f"{label}: {ts:%H:%M:%S}–{end:%H:%M:%S} UTC"))
    marks = [axis.mark(now, cls="now", label="now", title=f"Now: {now:%H:%M:%S} UTC")]
    if run.worm_start_at:
        marks.append(axis.mark(run.worm_start_at, cls="worm", label="worm", title=f"The worm started: {run.worm_start_at:%H:%M:%S} UTC"))
    if run.ends_at:
        marks.append(axis.mark(run.ends_at, cls="end", label="run ends", title=f"The run ends: {run.ends_at:%H:%M:%S} UTC"))

    def offset(ts):
        return _clock((ts - run.worm_start_at).total_seconds()) if run.worm_start_at else _clock((ts - began).total_seconds())

    log = []
    for ts, state in states:
        log.append({"ts": ts, "kind": "state", "text": STATE_TEXT.get(state, state).format(attempt=run.attempt + 1)})
    events = RunEvent.objects.filter(run=run).exclude(kind=RunEvent.Kind.STATE).select_related("vm").order_by("ts", "id")
    ticks = []
    for event in events:
        data = event.data or {}
        text = EVENT_TEXT.get(event.kind, event.kind).format(vm=event.vm.vm_identifier if event.vm else "a VM",
                                                             message=str(data.get("message", ""))[:200])  # fmt: skip
        if event.kind == RunEvent.Kind.VM_STOPPED and data.get("how") == "guest":
            text += " from inside: the worm's doing (the host says)"  # the watch's, conductor.py
        log.append({"ts": event.ts, "kind": event.kind, "text": text})
        ticks.append(axis.mark(event.ts, cls=f"ev ev-{event.kind}", title=f"{event.ts:%H:%M:%S} {text}"))
    waiting_on = None
    for job in run.jobs.order_by("id").only("kind", "state", "params", "requested_at", "finished_at", "error"):
        if job.kind in (HostJob.Kind.WRITE_FILE, HostJob.Kind.READ_FILE):
            what = "written" if job.kind == HostJob.Kind.WRITE_FILE else "read"
            number = (job.params or {}).get("file")
            outcome = {"done": "", "failed": f" (failed: {job.error[:120]})"}.get(job.state, f" ({job.state})")
            when = job.finished_at or job.requested_at
            log.append({"ts": when, "kind": job.kind, "text": f"file #{number} {what}{outcome}"})
            ticks.append(axis.mark(when, cls=f"ev ev-{job.kind}", title=f"{when:%H:%M:%S} file #{number} {what}{outcome}"))
        if job.state in (HostJob.State.QUEUED, HostJob.State.RUNNING):
            waiting_on = job
    log.sort(key=lambda entry: entry["ts"])
    for entry in log:
        entry["offset"] = offset(entry["ts"])

    run_time = None
    if run.worm_start_at and run.ends_at:
        total = max(1, int((run.ends_at - run.worm_start_at).total_seconds()))
        elapsed = int(min(total, max(0, (now - run.worm_start_at).total_seconds())))
        run_time = {"elapsed": elapsed, "total": total, "percent": round(100 * elapsed / total),
                    "elapsed_text": _clock(elapsed)[1:], "total_text": _clock(total)[1:]}  # fmt: skip
    return {
        "run": run,
        "steps": steps,
        "since": reached.get(run.state) or run.state_changed_at,
        "segments": segments,
        "marks": marks,
        "ticks": ticks,
        "axis_ticks": axis.ticks(),
        "log": log[-40:],
        "log_cut": max(0, len(log) - 40),
        "run_time": run_time,
        "flags": flags,
        "waiting_on": waiting_on,
    }
