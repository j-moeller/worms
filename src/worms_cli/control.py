"""Holding things up and letting them go: the round agent's jobs and the rounds.

    worms pause jobs [--reason TEXT]   the agent finishes the job under way and takes no other
    worms resume jobs                  ... takes jobs again
    worms cancel job <n>...            queued jobs, never run: failed, "cancelled by the operator"
    worms cancel jobs --team <t>       every queued job of a team (its devbox's)
    worms pause round <r>              the conductor starts no attempt of it (one under way ends as
                                       usual); the devboxes it holds are started again meanwhile
    worms resume round <r>             ... starts them again, holding the devboxes again if it wants
    worms start round <r>              a round still to come begins now (its start is moved to now)
    worms cancel round <r>             its attempts still to come are skipped

The pause is a file on the node the agent looks at before each job (scripts/round_agent/agent.py,
claim; handlers.paused_path); the rest are the database's, which the orchestrator reads
(conductor.py: Round.on_hold; devbox_runs.py: a cancelled reset fails its upload, said why).
A running job is not cancelled here: it is a process on the node, half-way through something.
"""

from __future__ import annotations

import getpass
import shlex
from datetime import datetime, timezone

from worms_cli import output
from worms_cli.build import BuildError
from worms_cli.jobs import PAUSED

CANCELLED = "cancelled by the operator (`worms cancel job`)"


def _who() -> str:
    return f"{getpass.getuser()} at {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}"


def pause_jobs(session, reason: str = "") -> int:
    text = f"paused by {_who()}" + (f": {reason}" if reason else "")
    code, _out, err = session.node_run(["sh", "-c", f"install -d -m 755 /var/lib/worms && printf '%s\\n' {shlex.quote(text)} > {PAUSED}"])
    if code != 0:
        raise BuildError(f"could not pause the agent: {err.strip()[:200]}")
    print(output.status_cell("warn", f"the round agent is paused: it finishes the job under way and takes no other "
                                     "(`worms resume jobs`; queued: `worms show jobs`)"))  # fmt: skip
    return 0


def resume_jobs(session) -> int:
    code, _out, err = session.node_run(["rm", "-f", PAUSED])
    if code != 0:
        raise BuildError(f"could not resume the agent: {err.strip()[:200]}")
    print(output.status_cell("ok", "the round agent takes jobs again"))
    return 0


def cancel_jobs(numbers=(), team=None) -> int:
    """Queued jobs failed, never run. A running one is refused: it is half-way through something."""
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
            raise BuildError(f"no host job {', '.join(f'#{n}' for n in missing)} (`worms show jobs`)")
    problems = 0
    for job in jobs:
        if job.state != HostJob.State.QUEUED:
            output.error(f"#{job.id} {job.kind} is {job.state}: only a queued job is cancelled"
                         + (" (a running one: `worms pause jobs`, then it ends; or `worms restart agent`)" if job.state == "running" else ""))
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
    """A round of the current competition, by its id (`worms show rounds`) or its name."""
    from core.models.competition import Round
    from worms_cli.queries import _require_competition

    rounds = Round.objects.filter(competition=_require_competition())
    found = rounds.filter(id=int(text)).first() if text.isdigit() else None
    found = found or rounds.filter(name=text).first() or rounds.filter(name__iexact=text).first()
    if found is None:
        raise BuildError(f"no round {text!r} (`worms show rounds` lists them, id first)")
    return found


def pause_round(round_) -> int:
    from core.models.competition import Round

    Round.objects.filter(pk=round_.pk).update(on_hold=True)
    print(output.status_cell("warn", f"round {round_.name!r} is on hold: no attempt of it starts (one under way ends as "
                                     f"usual); `worms resume round {round_.id}`"))  # fmt: skip
    return 0


def resume_round(round_) -> int:
    from core.models.competition import Round
    from core.models.rounds import RoundRun

    Round.objects.filter(pk=round_.pk).update(on_hold=False)
    # Its devboxes were started again while it waited: shut them down again before its next attempt.
    if round_.devbox_hold == "released" and RoundRun.objects.filter(round=round_, state=RoundRun.State.PENDING).exists():
        Round.objects.filter(pk=round_.pk, devbox_hold="released").update(devbox_hold="wanted")
    print(output.status_cell("ok", f"round {round_.name!r} goes on"))
    return 0


def start_round_now(round_) -> int:
    from core.models.competition import Round
    from django.utils import timezone

    now = timezone.now()
    if round_.round_start and round_.round_start <= now:
        raise BuildError(f"round {round_.name!r} has started already ({round_.round_start:%Y-%m-%d %H:%M} UTC)"
                         + (": it is on hold, `worms resume round`" if round_.on_hold else ""))  # fmt: skip
    if round_.round_end and round_.round_end <= now:
        raise BuildError(f"round {round_.name!r} ended at {round_.round_end:%Y-%m-%d %H:%M} UTC")
    Round.objects.filter(pk=round_.pk).update(round_start=now)
    print(output.status_cell("ok", f"round {round_.name!r} starts now (was {round_.round_start:%Y-%m-%d %H:%M} UTC): its "
                                   "runs are planned with the uploads made until now, then the conductor begins it "
                                   "(`worms logs conductor -f`)"))  # fmt: skip
    return 0


def cancel_round(round_) -> int:
    from core.models.rounds import RoundRun
    from django.utils import timezone

    pending = RoundRun.objects.filter(round=round_, state=RoundRun.State.PENDING)
    if not pending.exists() and not RoundRun.objects.filter(round=round_).exists():
        raise BuildError(f"round {round_.name!r} has not begun: nothing to cancel. To keep it from starting: "
                         f"`worms pause round {round_.id}`, or delete it on /staff/rounds/")  # fmt: skip
    count = pending.update(state=RoundRun.State.SKIPPED, finished_at=timezone.now(), error="cancelled by the operator (`worms cancel round`)")
    print(output.status_cell("ok", f"round {round_.name!r}: {count} attempt(s) still to come skipped; one under way ends as usual"))
    return 0
