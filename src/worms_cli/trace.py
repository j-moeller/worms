"""`worms trace <team>|upload <n>`: where a team's dev upload is, link by link, and why it waits.

    upload 42  alpha  19:12  by erik  (sha256 3f2a…)
      ✓ picked up by the orchestrator   19:12:05  devbox run 9
      ⏸ put on VM A                     waits: a round's attempt is under way (round 'Final', …)
      · runs                            —

Each link is read from the database: the upload, its devbox run (orchestrator/rounds/devbox_runs.py,
the state machine PENDING -> DEPLOYING -> ARMED -> RUNNING -> FINISHED: the devbox as it is, no
reset first since 2026-11-24), its host job (devbox_deploy; devbox_rebuild for an upload from
before) and, for a queued job, why the agent has not taken it (worms_cli/jobs.py). What the
orchestrator waits for before the deploy (a round's attempt under way, the team's own reset) is
its own words (devbox_runs.deploy_waits_for).
"""

from __future__ import annotations

from worms_cli import jobs as host_jobs
from worms_cli import output
from worms_cli.build import BuildError

WORM_DIR = "/home/debian/worm"  # scripts/round_agent/handlers.py: WORM_DIR/WORM_CODE, from WORM_ZIP
MARK = {"ok": ("✓", "green"), "wait": ("⏸", "yellow"), "error": ("✗", "red"), "todo": ("·", "dim"), "run": ("▶", "green")}


def _when(value) -> str:
    return f"{value:%Y-%m-%d %H:%M:%S}" if value else "—"


def _job_link(session, job, host) -> tuple[str, str]:
    """(state, text) of a host job as a link."""
    from django.utils import timezone

    if job is None:
        return "todo", "—"
    if job.state == "queued":
        reasons = host_jobs.queue_reasons(job, host)
        return "wait", f"job #{job.id} queued for {host_jobs._ago((timezone.now() - job.requested_at).total_seconds())}: " + (
            "; ".join(reasons) or "about to start")
    if job.state == "running":
        since = job.started_at or job.requested_at
        return "run", f"job #{job.id} running for {host_jobs._ago((timezone.now() - since).total_seconds())} (`worms log {job.id} -f`)"
    if job.state == "failed":
        return "error", f"job #{job.id} failed: {(job.error or '?')[:300]} (`worms log {job.id}`)"
    return "ok", f"job #{job.id} done {_when(job.finished_at)}"


def chain(session, upload) -> list[tuple[str, str, str]]:
    """(state, link, text) for each link of this upload's way to the devbox."""
    from django.utils import timezone
    from orchestrator.rounds import devbox_runs

    run = getattr(upload, "devbox_run", None)
    host = host_jobs.host_state(session)
    links = []
    if run is None:
        links.append(("wait", "picked up by the orchestrator", "not yet: the attempt processor picks uploads up every few "
                                                               "seconds (`worms log processor`)"))  # fmt: skip
        return links + [("todo", "put on VM A", "—"), ("todo", "runs", "—")]
    links.append(("ok", "picked up by the orchestrator", f"{_when(run.created_at)}: devbox run {run.id} ({run.state})"))
    S = run.State
    # A reset before the deploy: an upload from before 2026-11-24 only (since: the devbox as it is).
    if run.rebuild_job_id:
        state, text = _job_link(session, run.rebuild_job, host)
        links.append((state, "reset of the devbox", text))
    # The deploy onto VM A.
    if run.state == S.PENDING:
        waits = devbox_runs.deploy_waits_for(run.team)
        open_rebuild = waits and run.team and _open_rebuild(run.team)
        text = (f"waits for the reset of its devbox under way: {_job_link(session, open_rebuild, host)[1]}" if open_rebuild
                else f"waits: {waits}" if waits else "about to be requested")  # fmt: skip
        links.append(("wait", "put on VM A", text))
    else:
        deploy_state, deploy_text = _job_link(session, run.deploy_job, host)
        if deploy_state == "ok":
            deploy_text += f": the zip unpacked in {WORM_DIR} on VM A"
        links.append((deploy_state, "put on VM A", deploy_text))
    # Its hour.
    now = timezone.now()
    if run.state == S.ARMED:
        links.append(("wait", "runs", f"armed: starts {_when(run.worm_start_at)} (in {host_jobs._ago((run.worm_start_at - now).total_seconds())})"))
    elif run.state == S.RUNNING:
        links.append(("run", "runs", f"since {_when(run.worm_start_at)}, until {_when(run.ends_at)}"))
    elif run.state == S.FINISHED:
        links.append(("ok", "runs", f"ran {_when(run.worm_start_at)} to {_when(run.ends_at)}"))
    elif run.state == S.FAILED:
        links.append(("error", "runs", f"failed: {run.error or '?'}"))
    elif run.state == S.REPLACED:
        links.append(("error", "runs", "replaced: a newer upload or a manual reset came first"))
    else:
        links.append(("todo", "runs", "—"))
    return links


def _open_rebuild(team):
    from core.models.rounds import HostJob

    return HostJob.objects.filter(kind="devbox_rebuild", state__in=["queued", "running"],
                                  params__team_uuid=str(team.uuid)).order_by("id").first()  # fmt: skip


def uploads_of(words) -> list:
    """The uploads `words` name: `upload <n>`, or a team's dev uploads (the newest first, 3)."""
    from core.models.worms import Upload
    from worms_cli import queries

    base = Upload.objects.select_related("team", "user", "artifact", "devbox_run__rebuild_job", "devbox_run__deploy_job")
    if len(words) == 2 and words[0] == "upload" and words[1].isdigit():
        upload = base.filter(id=int(words[1])).first()
        if upload is None:
            raise BuildError(f"no upload #{words[1]}")
        return [upload]
    if len(words) != 1:
        raise BuildError("trace what? `worms trace <team>` or `worms trace upload <n>`")
    team = queries.team(words[0])
    found = list(base.filter(team=team, devbox=True).order_by("-created_at", "-id")[:3])
    if not found:
        raise BuildError(f"{team.name} has no dev upload (only those go to its devbox; round uploads: `worms round`)")
    return found


def trace(session, words) -> int:
    """Print each upload's chain; 1 if the newest is stuck on an error."""
    uploads = uploads_of(words)
    worst = 0
    for index, upload in enumerate(uploads):
        who = upload.user.username if upload.user_id else "?"
        title = (f"upload {upload.id} · {upload.team.name if upload.team_id else '?'} · {_when(upload.created_at)} · by {who} · "
                 f"sha256 {upload.artifact.content_hash[:12]}…" + ("" if upload.devbox else " · a round upload, not the devbox's"))
        print(output.heading(title if index == 0 else f"{title} (older)"))
        links = chain(session, upload)
        if output.capturing():
            words = {"ok": "done", "wait": "waits", "error": "failed", "todo": "to come", "run": "running"}
            print(output.table([[words[state], link, text] for state, link, text in links], headers=["state", "step", "detail"]))
            worst = worst or int(index == 0 and any(state == "error" for state, _l, _t in links))
            continue
        width = max(len(link) for _state, link, _text in links)
        for state, link, text in links:
            mark, colour = MARK[state]
            print(output.color(f"  {mark} {link:<{width}}", colour) + f"  {text}")
            if index == 0 and state == "error":
                worst = 1
        if index == 0 and len(uploads) > 1:
            print()
    return worst
