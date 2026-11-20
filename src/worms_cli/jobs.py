"""The round agent's host jobs, from the console: what one is, why it waits, its log as it is written.

    worms show jobs                 the newest jobs (worms_cli/queries.py, job_rows)
    worms show job 812              one job: kind, team, state, times, error, why it waits, its log's end
    worms reset alpha               ... queues jobs and follows them (follow): why each one waits while it
                                    is queued, then its log, live, until it ends; Ctrl-C stops following,
                                    not the jobs (--detach: queue only)

A queued job can wait for reasons nothing else shows (queue_reasons): the agent paused (`worms pause
jobs`), the agent not running, a round holding the devboxes (the agent takes no devbox job then:
scripts/round_agent/agent.py, claim), a job running ahead, jobs queued ahead (a round's first).

Job logs are on the node: /var/log/worms/jobs/<uuid>.log (scripts/round_agent/executor.py, JobLog).
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import threading
import time

from worms_cli import output
from worms_cli.build import BuildError

JOB_LOGS = "/var/log/worms/jobs"
HELD = "/var/lib/worms/held-devboxes.json"  # scripts/round_agent/handlers.py, held_devboxes_path
PAUSED = "/var/lib/worms/agent-paused"  # scripts/round_agent/agent.py, PAUSED: no job is claimed while it exists
AGENT_UNIT = "worms-round-agent"
POLL = 3.0

NODE_PROBE = f"""
systemctl is-active --quiet {AGENT_UNIT} && echo agent=active || echo agent=inactive
test -e {HELD} && echo held=yes || echo held=no
test -e {PAUSED} && echo paused="$(head -c 200 {PAUSED} | tr -d '\\n')" || echo paused=
"""


def _ago(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60} min" if seconds < 3600 else f"{seconds // 3600} h {seconds % 3600 // 60} min"


def node_state(session) -> dict:
    """The agent's side, read on the node: is it running, paused, are the devboxes held."""
    code, out, _err = session.node_run(["sh", "-c", NODE_PROBE])
    state = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {"agent": state.get("agent", "unknown"), "held": state.get("held") == "yes", "paused": state.get("paused", "")}


def team_of(job) -> str:
    from core.models.team import Team

    if job.run_id and job.run and job.run.team_id:
        return job.run.team.name
    uuid = (job.params or {}).get("team_uuid")
    team = Team.objects.filter(uuid=uuid).first() if uuid else None
    return team.name if team else ""


def describe(job) -> str:
    team = team_of(job)
    return f"#{job.id} {job.kind}" + (f" ({team})" if team else "")


def queue_reasons(job, node: dict) -> list[str]:
    """Why a queued job has not started: each reason that holds, the strongest first."""
    from core.models.rounds import HostJob
    from django.utils import timezone

    reasons = []
    if node.get("paused"):
        reasons.append(f"the round agent is paused: it takes no job until `worms resume jobs` ({node['paused']})")
    if node.get("agent") != "active":
        reasons.append(f"the round agent is not running (`worms logs agent`, `worms restart agent`)")
    if node.get("held") and job.kind.startswith("devbox"):
        reasons.append("a round holds the devboxes shut down: the agent takes no devbox job until its release "
                       "(`worms show rounds`)")  # fmt: skip
    now = timezone.now()
    for running in HostJob.objects.filter(state=HostJob.State.RUNNING).order_by("id"):
        since = running.started_at or running.requested_at
        reasons.append(f"behind {describe(running)}, running for {_ago((now - since).total_seconds())}")
    ahead = HostJob.objects.filter(state=HostJob.State.QUEUED, id__lt=job.id)
    if not job.kind.startswith("devbox"):
        ahead = ahead.exclude(kind__startswith="devbox")
    rounds_first = HostJob.objects.filter(state=HostJob.State.QUEUED, id__gt=job.id).exclude(kind__startswith="devbox") \
        if job.kind.startswith("devbox") else HostJob.objects.none()  # fmt: skip
    if ahead.exists() or rounds_first.exists():
        count = ahead.count() + rounds_first.count()
        reasons.append(f"{count} job(s) queued ahead of it" + (" (a round's go first)" if rounds_first.exists() else ""))
    return reasons


class _Tail:
    """`tail -F` of a job's log on the node, its lines to this terminal, until stop()."""

    def __init__(self, session, job):
        command = f"tail -n +1 -F {shlex.quote(f'{JOB_LOGS}/{job.uuid}.log')} 2>/dev/null"
        argv, env = (session.ssh_argv(command), session.env()) if session.laptop else (["sh", "-c", command], None)
        self.proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.thread = threading.Thread(target=self._relay, daemon=True)
        self.thread.start()

    def _relay(self):
        for raw in iter(self.proc.stdout.readline, b""):
            sys.stdout.write("  │ " + raw.decode(errors="replace"))
            sys.stdout.flush()

    def stop(self):
        time.sleep(1.5)  # the last lines the job wrote before it ended
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.thread.join(timeout=2)


def follow(session, jobs) -> int:
    """Each of these jobs in turn (the agent runs one at a time): why it waits while it is queued,
    then its log, live, until it ends. 1 if one failed, 130 if Ctrl-C (the jobs go on)."""
    from core.models.rounds import HostJob

    ids = sorted({job.id for job in jobs if job is not None})
    if not ids:
        return 0
    failed = []
    current = None
    tail = None
    try:
        for number in ids:
            current = number
            said = None
            while True:
                job = HostJob.objects.select_related("run__team").get(id=number)
                if job.state == HostJob.State.QUEUED:
                    reasons = queue_reasons(job, node_state(session))
                    text = "; ".join(reasons) or "about to start"
                    if text != said:
                        print(output.status_cell("pending", f"{describe(job)} queued: {text}"), flush=True)
                        said = text
                elif job.state == HostJob.State.RUNNING and tail is None:
                    print(output.status_cell("running", f"{describe(job)} running · its log ({JOB_LOGS}/{job.uuid}.log):"),
                          flush=True)  # fmt: skip
                    tail = _Tail(session, job)
                elif job.state in (HostJob.State.DONE, HostJob.State.FAILED):
                    if tail is not None:
                        tail.stop()
                        tail = None
                    took = (job.finished_at - (job.started_at or job.requested_at)).total_seconds() if job.finished_at else 0
                    ok = job.state == HostJob.State.DONE
                    print(output.status_cell("ok" if ok else "error", f"{describe(job)} {job.state} after {_ago(took)}")
                          + (f": {job.error[:300]}" if job.error else ""), flush=True)  # fmt: skip
                    if not ok:
                        failed.append(job.id)
                    break
                time.sleep(POLL)
    except KeyboardInterrupt:
        if tail is not None:
            tail.proc.terminate()
        output.error(f"stopped following; the job(s) go on: `worms show job {current}`, `worms logs {current} -f`")
        return 130
    if failed:
        output.error(f"{len(failed)} job(s) failed: `worms logs {failed[0]}` for the whole log")
        return 1
    return 0


def show(session, number: int, lines: int = 30) -> int:
    """`worms show job <n>`: what it is, where it stands, why it waits, its log's end."""
    from core.models.rounds import HostJob
    from django.utils import timezone

    job = HostJob.objects.select_related("run__team").filter(id=number).first()
    if job is None:
        raise BuildError(f"no host job #{number}: `worms show jobs` lists them")
    now = timezone.now()
    rows = [
        ["job", f"#{job.id} {job.kind} · {job.uuid}"],
        ["team", team_of(job) or "—"],
        ["round run", f"#{job.run_id} ({job.run.state})" if job.run_id else "—"],
        ["state", output.status_cell({"queued": "pending", "running": "running", "done": "ok", "failed": "error"}.get(job.state, "n/a"),
                                     job.state)],  # fmt: skip
        ["requested", f"{job.requested_at:%Y-%m-%d %H:%M:%S} ({_ago((now - job.requested_at).total_seconds())} ago)"],
        ["started", f"{job.started_at:%H:%M:%S}" if job.started_at else "—"],
        ["heartbeat", f"{_ago((now - job.heartbeat_at).total_seconds())} ago" if job.heartbeat_at and job.state == "running" else "—"],
        ["finished", f"{job.finished_at:%H:%M:%S}" if job.finished_at else "—"],
        ["params", ", ".join(f"{k}={str(v)[:60]}" for k, v in (job.params or {}).items()) or "—"],
    ]
    if job.error:
        rows.append(["error", job.error[:600]])
    if job.state == HostJob.State.QUEUED:
        rows.append(["waiting", "; ".join(queue_reasons(job, node_state(session))) or "about to start"])
    print(output.table(rows))
    command = f"tail -n {int(lines)} {shlex.quote(f'{JOB_LOGS}/{job.uuid}.log')} 2>/dev/null || echo '(no log yet)'"
    code, out, _err = session.node_run(["sh", "-c", command])
    print()
    print(output.heading(f"its log, the last {lines} lines (`worms logs {job.id} -f` follows it)"))
    print(out.rstrip() or "(empty)")
    return 0
