"""Holding up the round agent's jobs and letting them go (a round itself: worms_cli/rounds.py).

    worms jobs pause [--reason TEXT]   the agent finishes the job under way and takes no other
    worms jobs resume                  ... takes jobs again
    worms jobs cancel <n>...           queued jobs, never run: failed, "cancelled by the operator";
                                       and a job marked running whose heartbeat stopped (its agent
                                       was killed): it runs nowhere
    worms jobs cancel --team <t>       every queued job of a team (its devbox's)

The pause is a file on the host the agent looks at before each job (scripts/round_agent/agent.py,
claim; handlers.paused_path); the rest are the database's (devbox_runs.py: a cancelled reset fails
its upload, said why). A job that is running is not cancelled here: it is a process on the host,
half-way through something.
"""

from __future__ import annotations

import getpass
import shlex
from datetime import datetime, timezone

from worms_cli import output
from worms_cli.build import BuildError
from worms_cli.jobs import PAUSED

CANCELLED = "cancelled by the operator (`worms jobs cancel`)"


def _who() -> str:
    return f"{getpass.getuser()} at {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}"


def pause_jobs(session, reason: str = "") -> int:
    text = f"paused by {_who()}" + (f": {reason}" if reason else "")
    code, _out, err = session.host_run(["sh", "-c", f"install -d -m 755 /var/lib/worms && printf '%s\\n' {shlex.quote(text)} > {PAUSED}"])
    if code != 0:
        raise BuildError(f"could not pause the agent: {err.strip()[:200]}")
    print(output.status_cell("warn", f"the round agent is paused: it finishes the job under way and takes no other "
                                     "(`worms jobs resume`; queued: `worms jobs`)"))  # fmt: skip
    return 0


def resume_jobs(session) -> int:
    code, _out, err = session.host_run(["rm", "-f", PAUSED])
    if code != 0:
        raise BuildError(f"could not resume the agent: {err.strip()[:200]}")
    print(output.status_cell("ok", "the round agent takes jobs again"))
    return 0


def cancel_jobs(numbers=(), team=None, agent_stopped=False) -> int:
    """Queued jobs failed, never run; one marked running that runs nowhere any more
    (services.left_behind) marked failed. One that may still run is refused: it is half-way
    through something."""
    from core.models.rounds import HostJob
    from django.utils import timezone

    if team is not None:
        jobs = list(HostJob.objects.filter(state=HostJob.State.QUEUED, params__team_uuid=str(team.uuid)).order_by("id"))
        if not jobs:
            print(f"{team.name}: no queued job")
            return 0
    else:
        jobs = list(HostJob.objects.filter(id__in=numbers).order_by("id"))
        missing = sorted(set(numbers) - {job.id for job in jobs})
        if missing:
            raise BuildError(f"no host job {', '.join(f'#{n}' for n in missing)} (`worms jobs`)")
    from worms_cli.services import left_behind

    problems = 0
    for job in jobs:
        why = left_behind(job, agent_stopped) if job.state == HostJob.State.RUNNING else ""
        if why:
            updated = HostJob.objects.filter(id=job.id, state=HostJob.State.RUNNING, heartbeat_at=job.heartbeat_at).update(
                state=HostJob.State.FAILED, finished_at=timezone.now(),
                error=f"marked running, but it ran nowhere any more: {why}; {CANCELLED}")  # fmt: skip
            print(output.status_cell("ok" if updated else "error", f"#{job.id} {job.kind}: " + (
                f"{why}: marked failed" if updated else "it beat again meanwhile, not cancelled")))
            problems += not updated
            continue
        if job.state != HostJob.State.QUEUED:
            output.error(f"#{job.id} {job.kind} is {job.state}: only a queued job is cancelled"
                         + (" (a running one: `worms jobs pause`, then it ends; one that runs nowhere any more: "
                            "once the round agent is stopped)" if job.state == "running" else ""))
            problems += 1
            continue
        updated = HostJob.objects.filter(id=job.id, state=HostJob.State.QUEUED).update(
            state=HostJob.State.FAILED, error=CANCELLED, finished_at=timezone.now())
        if updated:
            print(output.status_cell("ok", f"#{job.id} {job.kind}: cancelled"))
        else:
            output.error(f"#{job.id} {job.kind}: the agent took it meanwhile, not cancelled")
            problems += 1
    return 1 if problems else 0


def find_round(text: str):
    """A round of the current competition, by its id (`worms round`) or its name."""
    from core.models.competition import Round
    from worms_cli.queries import _require_competition

    rounds = Round.objects.filter(competition=_require_competition())
    found = rounds.filter(id=int(text)).first() if text.isdigit() else None
    found = found or rounds.filter(name=text).first() or rounds.filter(name__iexact=text).first()
    if found is None:
        raise BuildError(f"no round {text!r} (`worms round` lists them, id first)")
    return found
