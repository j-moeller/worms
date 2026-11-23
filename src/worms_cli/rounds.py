"""`worms round start <round>`: a round, run here, in this terminal, from its first team to its
last (docs/05-live-environment.md). Nothing else runs a round: no daemon, no schedule, no retry.

    worms round start 3                  a round's first session: every team, in the round's order
    worms round start 3 --resume         every team that has not finished: a new attempt after a failed one
    worms round start 3 --team alpha     one team (its attempt still to come, or a new one after a failure)
    worms round publish 3                its results to the teams and the leaderboard, once every team finished
    worms round unpublish 3              ... hidden again
    worms round teardown                 the round's sandbox (kept, powered off, after a session) destroyed
    worms log round [3] [alpha]          what each session wrote: round.log, and each attempt's steps

Before anything runs it asks, one by one, and says the command that does the same, and the
one that undoes it:

    the orchestrator stopped       worms service stop orchestrator    (worms service start orchestrator)
    the round agent stopped        worms service stop agent           (worms service start agent)
    the queued host jobs cancelled worms jobs cancel <n>...           (the teams ask again)
    the devboxes stopped           worms vm stop devboxes             (worms vm start devboxes)

then checks that each is so, the host's preflight (its memory and disk), and asks once more. A
"no" anywhere: the round does not start, and nothing more is changed.

Then, team after team: the host's steps (prepare, check, the gameplan, teardown), run in this
process as the round agent ran them (scripts/round_agent/handlers.py), with the round agent's
environment and the WORMS key in an ssh-agent of its own (as worms-host-run gives the agent).
Each step's commands and output go to its own file in the session's folder, ~/.worms/rounds/; the
terminal shows what each step does and how it ends. A failed attempt is written down and the
round goes on with the next team; at the end the failed ones may be run again (or later with
--resume), and once every team finished without an error the results may be published; then the
round's sandbox destroyed, the devboxes, the round agent and the orchestrator started again: each
asked. The teams see nothing of the round's attempts until its results are published. Ctrl-C fails
the attempt under way (its VMs stopped, its sandbox kept); twice, exits at once.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from worms_cli import output
from worms_cli.build import BuildError

AGENT_ENV = "/etc/worms/round-agent.env"  # ansible/roles/host/round_agent: the agent's environment
HOST_WORKSPACE = "/opt/worms"  # the round agent's workspace: the sandbox stack's state, the gameplans
log = logging.getLogger("orchestrator")


def on_the_host() -> bool:
    """This machine is the Proxmox host: its cluster file system and its round agent's environment are here."""
    return os.path.isdir("/etc/pve") and os.path.exists(AGENT_ENV)


def local_on_the_host(config) -> str:
    """For a round's commands (manage.py, HOST_ONLY), before the host's lock and the database are
    reached: on the host, a WORMS_SSH (laptop mode: ~/.worms/config, or the environment) is set
    aside, so that everything is reached from here, as the round agent does (its environment
    empties WORMS_SSH too). Elsewhere: why not ("" when it may go on)."""
    if not on_the_host():
        return (f"a round runs on the Proxmox host itself ({AGENT_ENV} is not here): "
                "`worms round start` there, as root")  # fmt: skip
    if config.ssh:
        print(output.color(f"on the host itself: WORMS_SSH={config.ssh} set aside for this command", "dim"), file=sys.stderr)
        config.env["WORMS_SSH"] = ""
        os.environ["WORMS_SSH"] = ""
    return ""


def directory() -> Path:
    return Path(os.path.expanduser("~")) / ".worms" / "rounds"


# -- asking ------------------------------------------------------------------------------------


def ask(question: str, default: bool, yes: bool = False) -> bool:
    """[Y/n] (default yes) or [y/N]. --yes answers yes; without a terminal, no."""
    if yes:
        print(output.color(f"{question} [{'Y/n' if default else 'y/N'}] yes (--yes)", "yellow"))
        return True
    if not sys.stdin.isatty():
        output.error(f"{question}: no terminal to ask in (pass --yes to answer yes)")
        return False
    try:
        answer = input(output.color(f"{question} [{'Y/n' if default else 'y/N'}] ", "yellow")).strip().lower()
    except EOFError:
        return False
    return default if answer == "" else answer in ("y", "yes")


def says(command: str, back: str | None = None) -> None:
    """The command that does the same (and the one that undoes it)."""
    print(output.color(f"    = {command}" + (f"      (back: {back})" if back else ""), "dim"))


# -- the Proxmox API on this host, for power control -------------------------------------------


class Pvesh:
    """The Proxmox API of this host, as root, through `pvesh`: the part of proxmoxer that
    orchestrator/rounds/power.py uses (nodes(n).qemu(id).status.current.get(), ...post(), tasks)."""

    def __init__(self, path: str = ""):
        self._path = path

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return Pvesh(f"{self._path}/{name}")

    def __call__(self, *parts):
        return Pvesh("/".join([self._path, *(str(part) for part in parts)]))

    def get(self, **params):
        return self._pvesh("get", params)

    def post(self, **params):
        return self._pvesh("create", params)

    def _pvesh(self, verb, params):
        argv = ["pvesh", verb, self._path, "--output-format", "json"]
        for key, value in params.items():
            argv += [f"--{key}", str(value)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise ConnectionError(f"pvesh {verb} {self._path}: no answer in 60 s") from None  # transient: tried again
        if proc.returncode != 0:
            raise RuntimeError(f"pvesh {verb} {self._path}: {(proc.stderr or proc.stdout).strip()[:300]}")
        text = proc.stdout.strip()
        return json.loads(text) if text else None


# -- the host's steps, run here ----------------------------------------------------------------


class LocalJobs:
    """The host's steps of a round, run in this process, one after another, as the round agent ran
    them: a host_jobs row each, for the record (`worms show job <n>`), its commands and output in
    a file of the session's folder. The conductor's `jobs` (orchestrator/rounds/conductor.py)."""

    def __init__(self, repo_root: str, folder, echo=print):
        from round_agent import agent, handlers, validation
        from round_agent.executor import Executor

        self.handlers, self.validation = handlers, validation
        self.cfg = agent.config(dict(os.environ))
        self.executor = Executor(repo_root, log_dir=str(folder()), latest=False)
        self.folder = folder  # run -> the folder its steps' files go to
        self.echo = echo
        self.count = 0

    def request(self, run, kind, **params):
        """Done when it returns (the watch's observe: AttemptBackend._ask)."""
        return self._do(run, kind, params)

    def run(self, run, kind, within=None, **params):
        from core.models.rounds import HostJob
        from gameplan.definition import RoundError

        job = self._do(run, kind, params)
        if job.state != HostJob.State.DONE:
            raise RoundError(f"{kind} failed: {job.error}"[:1000])
        return job.result or {}

    def _do(self, run, kind, params):
        from core.models.rounds import HostJob
        from django.utils import timezone

        now = timezone.now()
        job = HostJob.objects.create(run=run, kind=kind, params=params, state=HostJob.State.RUNNING, started_at=now,
                                     heartbeat_at=now)  # fmt: skip
        self.count += 1
        self.executor.log_dir = str(self.folder(run))
        name = f"{self.count:03d}-{kind}"
        quiet = kind == "observe"  # every minute of a gameplan: its file only, unless it fails
        state, result, error = HostJob.State.FAILED, {}, ""
        with self.executor.job_log(name, kind, job.id) as job_log:
            if not quiet:
                self.echo(output.color(f"  ▸ {kind} (#{job.id}; {job_log.path or 'no log file'})", "bold"))
                note = job_log.note

                def both(text, note=note):
                    note(text)
                    self.echo(output.color(f"    {text}", "dim"))

                job_log.note = both
            try:
                clean = self.validation.validate(kind, params)
                result = self.handlers.HANDLERS[kind](self.executor, self.cfg, clean) or {}
                state = HostJob.State.DONE
                job_log.note("done")
            except self.validation.Invalid as exc:
                error = f"invalid: {exc}"
                job_log.note(f"rejected: {exc}")
            except Exception as exc:  # noqa: BLE001  (its message and the log say why)
                error = f"{type(exc).__name__}: {exc}"
                job_log.note(f"failed: {error}")
                if quiet:
                    self.echo(output.color(f"  ▸ {kind} (#{job.id}) failed: {error} ({job_log.path})", "red"))
        HostJob.objects.filter(pk=job.pk).update(state=state, result=json.loads(json.dumps(result, default=str)),
                                                 error=error[:4000], finished_at=timezone.now(), heartbeat_at=timezone.now())  # fmt: skip
        job.refresh_from_db()
        return job


def _agent_environment() -> dict:
    """The round agent's environment (AGENT_ENV), as sh reads it (worms-host-run: `set -a; .`)."""
    if not os.path.exists(AGENT_ENV):
        raise BuildError(f"{AGENT_ENV} is not there: `worms apply round-agent` writes it")
    out = subprocess.run(["sh", "-c", f"set -a; . {AGENT_ENV}; set +a; env -0"], capture_output=True, check=True).stdout
    return dict(item.split("=", 1) for item in out.decode().split("\0") if "=" in item)


class KeyAgent:
    """An ssh-agent of this session's own, with the WORMS key alone (what worms-host-run gives the
    round agent: OpenTofu's Proxmox provider uploads snippets over SSH through an agent)."""

    def __init__(self, key: str):
        out = subprocess.run(["ssh-agent", "-s"], capture_output=True, text=True, check=True).stdout
        found = dict(re.findall(r"(SSH_AUTH_SOCK|SSH_AGENT_PID)=([^;]+);", out))
        self.env = found
        os.environ.update(found)
        added = subprocess.run(["ssh-add", "-q", key], capture_output=True, text=True)
        if added.returncode != 0:
            self.stop()
            raise BuildError(f"cannot load the WORMS key {key} into ssh-agent: {added.stderr.strip()[:200]}")

    def stop(self):
        if self.env.get("SSH_AGENT_PID"):
            subprocess.run(["ssh-agent", "-k"], capture_output=True)
            self.env = {}


# -- the session ---------------------------------------------------------------------------------


class Session:
    def __init__(self, config, round_, yes=False):
        self.config, self.round, self.yes = config, round_, yes
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = directory() / f"{round_.id}-{stamp}"
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.dir.parent, 0o700)
        self.conductor = None
        self.jobs = None
        self.interrupts = 0

    # The folder an attempt's steps go to; the session's own steps (gameplans, preflight) in the session's.
    def folder(self, run=None) -> Path:
        if run is None:
            return self.dir
        path = self.dir / f"{run.position + 1:02d}-{run.team.name}-attempt{run.attempt}"
        path.mkdir(mode=0o700, exist_ok=True)
        return path


def _say_round(round_, runs):
    from django.utils import timezone

    print(output.heading(f"round {round_.id} {round_.name!r}"))
    print(f"  start (the upload deadline): {round_.round_start:%Y-%m-%d %H:%M} UTC"
          + (" (still to come)" if round_.round_start > timezone.now() else ""))  # fmt: skip
    print(f"  gameplan: {round_.gameplan or '(none chosen)'}, {round_.team_duration} s per team")
    rows = []
    for run in runs:
        upload = f"#{run.upload_id} ({run.upload.created_at:%m-%d %H:%M} UTC)" if run.upload_id else "none: skipped"
        rows.append([str(run.position + 1), run.team.name, str(run.attempt), upload, run.state, (run.error or "")[:70]])
    print(output.table(rows, headers=["#", "Team", "Attempt", "Upload", "State", "Error"]))


def _latest_runs(round_):
    """Each team's latest attempt, in the round's order."""
    from core.models.rounds import RoundRun

    latest = {}
    for run in RoundRun.objects.filter(round=round_).select_related("team", "upload").order_by("position", "attempt"):
        latest[run.team_id] = run
    return sorted(latest.values(), key=lambda run: run.position)


def _chosen(session, args):
    """The attempts this session runs, in order: a first session's every one; --resume, every team
    that has not finished; --team, the named teams'. A failed one's team gets a new attempt once
    the round is ready (_run). None: nothing to run, or refused."""
    from core.models.rounds import RoundRun

    S = RoundRun.State
    latest = _latest_runs(session.round)
    unfinished = [run for run in latest if run.state in (S.PENDING, S.FAILED)]
    if args.team:
        chosen, by_name = [], {run.team.name.lower(): run for run in latest}
        for name in args.team:
            run = by_name.get(name.lower()) or next((r for r in latest if f"t{r.team_id}" == name), None)
            if run is None:
                raise BuildError(f"no team {name!r} in round {session.round.name!r} (its teams: {', '.join(r.team.name for r in latest)})")
            if run.state not in (S.PENDING, S.FAILED):
                raise BuildError(f"{run.team.name}'s attempt {run.attempt} is {run.state}: only a pending or failed one runs")
            chosen.append(run)
    elif args.resume:
        chosen = unfinished
    else:
        ran = [run for run in latest if run.state != S.PENDING or run.attempt > 0]
        if ran:
            raise BuildError(f"round {session.round.name!r} ran before ({_tally(latest)}): `worms round start "
                             f"{session.round.id} --resume` runs every team that has not finished, --team <t> one team")  # fmt: skip
        chosen = unfinished
    if not chosen:
        print(f"Every team's attempt has run ({_tally(latest)}): nothing to run."
              + (f" `worms round publish {session.round.id}` publishes the results." if _complete(latest) else ""))  # fmt: skip
        return []
    failed = [run for run in chosen if run.state == S.FAILED]
    for run in failed:
        print(f"{run.team.name}'s attempt {run.attempt} failed: {(run.error or '')[:200]}")
    if failed and not ask(f"Run a new attempt of {', '.join(run.team.name for run in failed)}?", True, session.yes):
        return None
    return chosen


def _tally(runs) -> str:
    counts = {}
    for run in runs:
        counts[run.state] = counts.get(run.state, 0) + 1
    return ", ".join(f"{n} {state}" for state, n in sorted(counts.items()))


def _complete(runs) -> bool:
    """Every team's attempt over without an error: finished cleanly, or skipped (no upload)."""
    from core.models.rounds import RoundRun

    S = RoundRun.State
    return bool(runs) and all((run.state == S.FINISHED and not run.error) or run.state == S.SKIPPED for run in runs)


# -- what must be stopped first ------------------------------------------------------------------


def _host_jobs():
    from core.models.rounds import HostJob
    from django.utils import timezone

    from worms_cli.services import SILENT_AFTER

    now = timezone.now()
    queued = list(HostJob.objects.filter(state=HostJob.State.QUEUED).order_by("id"))
    running, silent = [], []
    for job in HostJob.objects.filter(state=HostJob.State.RUNNING).order_by("id"):
        beat = job.heartbeat_at or job.started_at or job.requested_at
        (silent if beat and (now - beat).total_seconds() > SILENT_AFTER else running).append(job)
    return queued, running, silent


def _describe(job) -> str:
    from worms_cli.jobs import describe

    return describe(job)


def _stop_everything(session) -> bool:
    """Ask for each thing that must be stopped, in order; False at the first no."""
    from worms_cli import control, power, services

    yes = session.yes
    units = services.Units(session.config)
    # 1. The orchestrator: nothing queues a devbox job or counts a submission meanwhile.
    print(output.heading("1. the orchestrator"))
    active = units.active("orchestrator")
    if active:
        print(f"  running on the orchestrator: {', '.join(active)}")
        print("  Stopped, it takes no request from the dashboard (devbox resets, SSH keys, simulations) and counts "
              "nothing; this session counts the round's submissions itself.")  # fmt: skip
        says("worms service stop orchestrator", "worms service start orchestrator")
        if not ask("Stop the orchestrator?", True, yes):
            print("The round does not start while the orchestrator runs.")
            return False
        units.stop(["orchestrator"])
    else:
        print(output.status_cell("ok", "stopped already"))

    # 2. The round agent and its queue.
    print(output.heading("2. the round agent and its host jobs"))
    if units.active("agent"):
        queued, running, _silent = _host_jobs()
        if running:
            print("  running now: " + "; ".join(_describe(job) for job in running) + ": the agent finishes it first")
        print("  Stopped, it takes no host job (devbox resets and rebuilds, key updates) until it is started again.")
        says("worms service stop agent", "worms service start agent")
        if not ask("Stop the round agent?", True, yes):
            print("The round does not start while the round agent runs.")
            return False
        units.stop(["agent"])
    else:
        print(output.status_cell("ok", "the round agent is stopped already"))
    queued, running, silent = _host_jobs()
    if queued:
        print("  queued: " + "; ".join(_describe(job) for job in queued))
        print("  Cancelled, they never run (a team asks again for its devbox reset; the gameplans check runs here).")
        says("worms jobs cancel " + " ".join(str(job.id) for job in queued))
        if not ask(f"Cancel these {len(queued)} queued host job(s)?", True, yes):
            print("The round does not start while host jobs are queued.")
            return False
        control.cancel_jobs(numbers=[job.id for job in queued])
    if silent:
        print("  marked running, but silent (no heartbeat for minutes: the agent that ran them stopped): "
              + "; ".join(_describe(job) for job in silent))  # fmt: skip
        says("worms jobs cancel " + " ".join(str(job.id) for job in silent))
        if not ask(f"Mark these {len(silent)} job(s) failed?", True, yes):
            print("The round does not start while host jobs are marked running.")
            return False
        control.cancel_jobs(numbers=[job.id for job in silent])

    # 3. The devboxes and their gateways.
    print(output.heading("3. the devboxes"))
    machines = power.Power(session.config)
    running_vms = [vm["name"] for vm in machines.runner.vms() if vm.get("pool") == "devbox" and vm.get("status") != "stopped"]
    if running_vms:
        print(f"  running: {', '.join(running_vms)}")
        says("worms vm stop devboxes", "worms vm start devboxes")
        if not ask(f"Stop these {len(running_vms)} devbox VM(s) (a shutdown, forced after 120 s)?", True, yes):
            print("The round does not start while devbox VMs run.")
            return False
        if machines.stop(["devboxes"]) != 0:
            print("Not every devbox VM stopped: the round does not start.")
            return False
    else:
        print(output.status_cell("ok", "every devbox VM is stopped"))
    return True


def _verified(session) -> bool:
    """Each precondition, looked at again (not assumed from the answers above)."""
    from worms_cli import power, services

    print(output.heading("4. checked"))
    units = services.Units(session.config)
    machines = power.Power(session.config)
    vms = machines.runner.vms()
    queued, running, silent = _host_jobs()
    checks = [
        ("the orchestrator's services are stopped", not units.active("orchestrator"), ", ".join(units.active("orchestrator"))),
        ("the round agent is stopped", not units.active("agent"), "worms-round-agent is active"),
        ("no host job is queued or running", not (queued or running or silent),
         "; ".join(_describe(job) for job in queued + running + silent)),
        ("every devbox VM is stopped", all(vm.get("status") == "stopped" for vm in vms if vm.get("pool") == "devbox"),
         ", ".join(vm["name"] for vm in vms if vm.get("pool") == "devbox" and vm.get("status") != "stopped")),
        ("the internet window is closed", not machines.runner.flag_exists(power.INTERNET_FLAG), "`worms internet off`"),
    ]  # fmt: skip
    for label, ok, detail in checks:
        print(output.status_cell("ok" if ok else "error", label + ("" if ok else f": {detail}")))
    return all(ok for _label, ok, _detail in checks)


# -- running it ------------------------------------------------------------------------------------


def _count():
    """The round's flag submissions and log records so far, counted (the orchestrator's processor,
    stopped meanwhile, did this continuously): each counts only inside its attempt's window."""
    from django.conf import settings

    from orchestrator.rounds import captures

    grace = getattr(settings, "ROUND_CONDUCTOR", {}).get("capture_grace", 120)
    try:
        flags = captures.process_flags(grace_seconds=grace) + captures.process_late_releases(grace_seconds=grace)
        logs = captures.process_logs(grace_seconds=grace)
    except Exception:  # noqa: BLE001  (counted again at the next look, and at the attempt's end)
        log.exception("counting the submissions failed")
        return
    if flags or logs:
        log.info("counted %s flag capture(s), %s log record(s)", flags, logs)


def _interrupt(session):
    def handler(signum, frame):
        session.interrupts += 1
        if session.interrupts > 1:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt
        if session.conductor is not None:
            session.conductor.stopping = True
        print(output.color("\nCtrl-C: the attempt under way fails at its next step (its VMs stopped, its sandbox kept), "
                           "and no other starts. Ctrl-C again: out at once.", "yellow"), flush=True)  # fmt: skip

    signal.signal(signal.SIGINT, handler)


def _run(session, runs) -> list:
    """Each attempt in turn; the runs as they ended."""
    from core.models.rounds import RoundRun

    S = RoundRun.State
    conductor, ended = session.conductor, []
    for index, run in enumerate(runs, 1):
        if conductor.stopping:
            print(output.color(f"not run (stopped): {run.team.name}", "yellow"))
            continue
        if run.state == S.FAILED:
            run = conductor.again(run)
        run = RoundRun.objects.select_related("team", "upload", "round").get(pk=run.pk)
        print(output.heading(f"{index}/{len(runs)}: {run.team.name}, attempt {run.attempt} — {session.folder(run)}"))
        if run.upload_id is None:
            run = conductor.attempt(run)
            print(output.status_cell("skipped", f"{run.team.name}: no upload before the round's start"))
            ended.append(run)
            continue
        problem = conductor.preflight(run)
        if problem:
            print(output.status_cell("error", f"before {run.team.name}'s attempt: {problem}"))
            print("The round stops here: no attempt was used up. Mend it, then `worms round start "
                  f"{session.round.id}` runs the teams still to come.")  # fmt: skip
            break
        began = time.monotonic()
        run = conductor.attempt(run)
        _count()
        took = timedelta(seconds=int(time.monotonic() - began))
        if run.state == S.FINISHED:
            print(output.status_cell("ok", f"{run.team.name}: finished in {took}" + (f" ({run.error})" if run.error else "")))
        else:
            print(output.status_cell("error", f"{run.team.name}: {run.state} after {took}: {run.error}"))
            print(output.color(f"    its steps: {session.folder(run)}", "dim"))
        ended.append(run)
    return ended


def _summary(session):
    print(output.heading(f"round {session.round.name!r}: where each team stands"))
    _say_round(session.round, _latest_runs(session.round))
    print(output.color(f"this session's log and every step's: {session.dir} (`worms log round {session.round.id}`)", "dim"))


def _offer_publish(session):
    latest = _latest_runs(session.round)
    if not _complete(latest):
        print(output.status_cell("warn", f"not every team finished without an error ({_tally(latest)}): the results stay "
                                         f"unpublished. `worms round start {session.round.id} --resume`, then "
                                         f"`worms round publish {session.round.id}`"))  # fmt: skip
        return
    print(output.status_cell("ok", "every team's attempt finished without an error"))
    says(f"worms round publish {session.round.id}", f"worms round unpublish {session.round.id}")
    if ask("Publish the results (each team sees its attempt, the leaderboard the points)?", False, False):
        _publish(session.round)


def _standings(round_):
    from core.models.rounds import RoundRun
    from core.models.scoring import FlagCapture
    from django.db.models import Count

    counts = dict(FlagCapture.objects.filter(round=round_, vm__run__state=RoundRun.State.FINISHED)
                  .values("team_id").annotate(n=Count("id")).values_list("team_id", "n"))  # fmt: skip
    rows = []
    for run in _latest_runs(round_):
        rows.append([run.team.name, str(run.attempt), run.state, str(counts.get(run.team_id, 0)) if run.state == "finished" else "—",
                     (run.error or "")[:70]])
    print(output.table(rows, headers=["Team", "Attempt", "State", "Flags", "Error"]))


def _publish(round_):
    from django.utils import timezone

    type(round_).objects.filter(pk=round_.pk).update(results_published_at=timezone.now())
    print(output.status_cell("ok", f"round {round_.name!r}: results published (back: `worms round unpublish {round_.id}`)"))


def publish(round_, yes=False, anyway=False) -> int:
    """`worms round publish <r>`: the round's results to its teams and the leaderboard."""
    round_.refresh_from_db()
    print(output.heading(f"round {round_.id} {round_.name!r}"))
    _standings(round_)
    if round_.results_published_at:
        print(f"Published already, {round_.results_published_at:%Y-%m-%d %H:%M} UTC (`worms round unpublish {round_.id}`).")
        return 0
    latest = _latest_runs(round_)
    if not _complete(latest):
        message = (f"not every team's attempt finished without an error ({_tally(latest) or 'no attempt'}): "
                   f"`worms round start {round_.id} --resume` runs the others")  # fmt: skip
        if not anyway:
            output.error(message + "; --anyway publishes all the same (asked)")
            return 1
        output.warn(message)
    says(f"worms round publish {round_.id}", f"worms round unpublish {round_.id}")
    if not ask("Publish these results (each team sees its attempt, the leaderboard the points)?", False, yes):
        return 1
    _publish(round_)
    return 0


def unpublish(round_, yes=False) -> int:
    """`worms round unpublish <r>`: the round's results hidden from the teams again."""
    round_.refresh_from_db()
    if not round_.results_published_at:
        print(f"Round {round_.name!r}: its results are not published.")
        return 0
    if not ask(f"Hide round {round_.name!r}'s results from the teams and the leaderboard again?", False, yes):
        return 1
    type(round_).objects.filter(pk=round_.pk).update(results_published_at=None)
    print(output.status_cell("ok", f"round {round_.name!r}: results hidden (`worms round publish {round_.id}` shows them)"))
    return 0


def _afterwards(session):
    """Each thing stopped before, started again: asked."""
    from worms_cli import power, services

    yes = session.yes
    sandbox = [vm["name"] for vm in power.Power(session.config).runner.vms() if vm.get("pool") == "sandbox"]
    if sandbox:
        print(output.heading("the round's sandbox"))
        print(f"  kept, powered off, for another attempt: {len(sandbox)} VM(s). Another `worms round start` of the "
              "same gameplan rolls it back instead of building it (minutes instead of an hour).")  # fmt: skip
        says("worms round teardown")
        if ask("Destroy the round's sandbox now?", False, False):
            try:
                session.conductor.destroy_sandbox()
                print(output.status_cell("ok", "destroyed"))
            except Exception as exc:  # noqa: BLE001
                output.error(f"not destroyed: {exc}; later: `worms round teardown`")
        else:
            print("  later: worms round teardown")
    print(output.heading("start again what was stopped"))
    for question, command, act in (
        ("Start the devboxes again?", "worms vm start devboxes", lambda: power.Power(session.config).start(["devboxes"])),
        ("Start the round agent again?", "worms service start agent", lambda: services.Units(session.config).start(["agent"])),
        ("Start the orchestrator again?", "worms service start orchestrator",
         lambda: services.Units(session.config).start(["orchestrator"])),
    ):
        says(command)
        if ask(question, True, yes):
            try:
                act()
            except BuildError as exc:
                output.error(str(exc))
                print(f"  later: {command}")
        else:
            print(f"  later: {command}")


def start(config, args) -> int:
    from core.models.rounds import RoundRun
    from django.conf import settings
    from django.utils import timezone

    from orchestrator.rounds.conductor import ACTIVE, Conductor
    from orchestrator.rounds.planner import plan_round
    from orchestrator.rounds.power import ProxmoxPower
    from worms_cli import control, runlog

    if os.geteuid() != 0:
        raise BuildError("a round runs as root on the host")
    round_ = control.find_round(args.round)
    session = Session(config, round_, yes=args.yes)
    tee = runlog.Tee("round")
    tee.path = session.dir / "round.log"
    tee.start()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    for name in ("orchestrator", "round-agent"):
        logging.getLogger(name).addHandler(handler)
        logging.getLogger(name).setLevel(logging.INFO)
        logging.getLogger(name).propagate = False
    key = None
    try:
        print(output.color(f"this session's log: {session.dir}/round.log", "dim"))
        # The round's start is the teams' upload deadline: before it, starting moves it to now.
        now = timezone.now()
        if round_.round_start is None:
            raise BuildError(f"round {round_.name!r} has no start: set its window on Staff -> Rounds")
        if round_.round_end <= now:
            print(output.status_cell("warn", f"round {round_.name!r} ended at {round_.round_end:%Y-%m-%d %H:%M} UTC: its "
                                             "attempts may run past it (as retries always could)"))  # fmt: skip
        if round_.round_start > now:
            print(f"Round {round_.name!r} starts at {round_.round_start:%Y-%m-%d %H:%M} UTC: the teams may upload until then.")
            if not ask("Start it now (its start moved to now: uploads after now do not count)?", False, args.yes):
                return 1
            type(round_).objects.filter(pk=round_.pk).update(round_start=now)
            round_.refresh_from_db()
        if not RoundRun.objects.filter(round=round_).exists():
            runs = plan_round(round_)
            print(f"planned {len(runs)} team(s): each one's latest upload before {round_.round_start:%Y-%m-%d %H:%M} UTC")
        _say_round(round_, _latest_runs(round_))

        # The round agent's environment and code, as the service has them.
        repo_root, session.jobs, key = _host_steps(config, session.folder)
        _same_code(config, repo_root, args.yes)
        settings.PROXMOX["node"] = config.proxmox_node()
        session.conductor = Conductor(jobs=session.jobs, power=ProxmoxPower(api=Pvesh()), on_watch=_count)

        left = list(RoundRun.objects.filter(round=round_, state__in=ACTIVE).select_related("team", "round"))
        for run in left:
            print(f"{run.team.name}'s attempt {run.attempt} was left {run.state} by an earlier session (it was stopped).")
            if not ask("Fail it and stop its VMs (its sandbox kept)?", True, args.yes):
                return 1
            session.conductor.abandon(run)
        runs = _chosen(session, args)
        if not runs:
            return 0 if runs == [] else 1
        if not any(run.upload_id for run in runs):
            # No team to run has an upload: each is skipped, nothing is built, nothing needs stopping.
            for run in runs:
                session.conductor.attempt(RoundRun.objects.select_related("team", "round").get(pk=run.pk))
            print(output.status_cell("skipped", f"no team to run has an upload: {', '.join(run.team.name for run in runs)} skipped"))
            _summary(session)
            _offer_publish(session)
            return 0

        print(output.heading("the gameplan"))
        problem = session.conductor.pin(round_)
        if problem:
            output.error(f"round {round_.name!r}: {problem}")
            return 1
        pinned = RoundRun.objects.filter(round=round_).exclude(gameplan_sha256="").values_list("gameplan_sha256", flat=True).first()
        print(output.status_cell("ok", f"{round_.gameplan}, version {pinned[:12]}: valid at {round_.team_duration} s per team"))

        if not _stop_everything(session) or not _verified(session):
            return 1
        print(output.heading("5. the host's preflight"))
        first = next((run for run in runs if run.upload_id is not None), None)
        if first is not None:
            problem = session.conductor.preflight(RoundRun.objects.select_related("round", "team").get(pk=first.pk))
            if problem:
                output.error(problem)
                return 1
            print(output.status_cell("ok", "the host is ready (memory, disk, the sandbox's needs)"))
        minutes = sum(run.duration or round_.team_duration for run in runs if run.upload_id) // 60
        print(output.heading("6. the round"))
        print(f"  {len(runs)} team(s): {', '.join(run.team.name for run in runs)}; the gameplan about {minutes} min, "
              "plus the sandbox (about an hour to build, minutes to roll back) and about 15 min per team")  # fmt: skip
        if not ask(f"Run round {round_.name!r} now? This terminal stays with it to the end.", False, args.yes):
            return 1
        _interrupt(session)
        ended = _run(session, runs)
        failed = [run for run in ended if run.state == RoundRun.State.FAILED]
        if failed and not session.conductor.stopping:
            print(f"failed: {', '.join(run.team.name for run in failed)} (each attempt's steps: {session.dir})")
            says(f"worms round start {round_.id} --resume")
            if ask("Run a new attempt of each of them now (the sandbox is kept: a rollback, not a build)?", False, False):
                _run(session, failed)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        _summary(session)
        _offer_publish(session)
        _afterwards(session)
        return 0
    except KeyboardInterrupt:
        print(output.color("\nout at once: what was under way may be left as it was. `worms round` and `worms vm` say "
                           "what is; `worms round start` again fails an attempt left under way.", "red"))  # fmt: skip
        return 130
    finally:
        if key is not None:
            key.stop()
        tee.stop()


def _host_steps(config, folder):
    """The round agent's environment and code, as the service has them, and an ssh-agent with the
    WORMS key: (repo_root, LocalJobs, KeyAgent). The KeyAgent is the caller's to stop."""
    environment = _agent_environment()
    os.environ.update({k: v for k, v in environment.items() if k not in ("PWD", "SHLVL", "_")})
    repo_root = os.environ.get("REPO_ROOT", HOST_WORKSPACE)
    scripts = str(Path(config.repo_root) / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    key = KeyAgent(os.environ.get("SSH_KEY", "/etc/worms/ssh/worms"))
    return repo_root, LocalJobs(repo_root, folder), key


def teardown(config, yes=False) -> int:
    """`worms round teardown`: the sandbox a session kept (powered off, for another attempt) destroyed."""
    from django.conf import settings

    from orchestrator.rounds.conductor import Conductor
    from orchestrator.rounds.power import ProxmoxPower
    from worms_cli import power

    if os.geteuid() != 0:
        raise BuildError("on the host itself, as root")
    sandbox = sorted(vm["name"] for vm in power.Power(config).runner.vms() if vm.get("pool") == "sandbox")
    if not sandbox:
        print("No sandbox VM: nothing to destroy.")
        return 0
    print(f"the round's sandbox: {', '.join(sandbox)}")
    print("  Destroyed, the next `worms round start` builds it again (about an hour) instead of rolling it back.")
    if not ask(f"Destroy these {len(sandbox)} VM(s)?", False, yes):
        return 1
    folder = directory() / f"teardown-{datetime.now():%Y%m%d-%H%M%S}"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    _repo_root, jobs, key = _host_steps(config, lambda run=None: folder)
    try:
        settings.PROXMOX["node"] = config.proxmox_node()
        Conductor(jobs=jobs, power=ProxmoxPower(api=Pvesh())).destroy_sandbox()
    except Exception as exc:  # noqa: BLE001  (its step's file says more)
        raise BuildError(f"not destroyed: {exc} (its steps: {folder})") from None
    finally:
        key.stop()
    print(output.status_cell("ok", f"destroyed (its steps: {folder})"))
    return 0


def _same_code(config, repo_root, yes):
    """The round runs this checkout's code against the host's workspace (its OpenTofu state, its
    gameplans): both must be the same commit, or it is said and asked."""
    marker = Path(repo_root) / ".worms-commit"
    there = marker.read_text().strip() if marker.exists() else "(unknown)"
    here = subprocess.run(["git", "-C", str(config.repo_root), "rev-parse", "--short", "HEAD"], capture_output=True,
                          text=True).stdout.strip()  # fmt: skip
    dirty = subprocess.run(["git", "-C", str(config.repo_root), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True).stdout.strip()  # fmt: skip
    here += "-dirty" if dirty else ""
    if there.removesuffix("-dirty")[:7] == here.removesuffix("-dirty")[:7] and not dirty:
        print(output.status_cell("ok", f"the host's workspace runs this checkout's code ({here})"))
        return
    print(output.status_cell("warn", f"the host's workspace ({repo_root}) is at {there}, this checkout at {here}: "
                                     "`worms apply round-agent gameplans` brings them together"))  # fmt: skip
    if not ask("Run the round all the same?", False, yes):
        raise BuildError("not started: the host's workspace and this checkout differ")


# -- the logs ------------------------------------------------------------------------------------


def sessions(round_id=None) -> list[Path]:
    """The sessions' folders, newest first (of one round, given its id)."""
    folder = directory()
    if not folder.is_dir():
        return []
    found = [path for path in folder.iterdir() if path.is_dir() and re.fullmatch(r"\d+-\d{8}-\d{6}", path.name)]
    if round_id is not None:
        found = [path for path in found if path.name.split("-", 1)[0] == str(round_id)]
    return sorted(found, key=lambda path: path.name.split("-", 1)[1], reverse=True)


def show_logs(round_word=None, team=None, lines=100, follow=False) -> int:
    """`worms log round [<r>] [<team>]`."""
    if round_word is None:
        found = sessions()
        if not found:
            print(f"No round has run here yet ({directory()}).")
            return 0
        rows = []
        for path in found[:30]:
            round_id, stamp = path.name.split("-", 1)
            attempts = sorted(p.name for p in path.iterdir() if p.is_dir())
            rows.append([round_id, datetime.strptime(stamp, "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M"), str(len(attempts)), str(path)])
        print(output.table(rows, headers=["Round", "Session (local time)", "Attempts", "Folder"]))
        print(output.color("one: `worms log round <round> [<team>]`", "dim"))
        return 0
    found = sessions(round_word if round_word.isdigit() else None)
    if not found:
        raise BuildError(f"no session of round {round_word} here ({directory()}); `worms log round` lists them")
    session = found[0]
    if team is None:
        target = [str(session / "round.log")]
    else:
        attempts = sorted(p for p in session.iterdir() if p.is_dir() and p.name.split("-", 1)[1].lower().startswith(f"{team.lower()}-"))
        if not attempts:
            raise BuildError(f"no attempt of {team!r} in {session}")
        target = sorted(str(f) for f in attempts[-1].glob("*.log"))
        print(output.color(f"{attempts[-1]}: {', '.join(Path(t).name for t in target)}", "dim"))
    argv = ["tail", "-n", str(int(lines)), *(["-F"] if follow else []), *target]
    return subprocess.call(argv)


def node_name() -> str:
    return socket.gethostname().split(".")[0]


def check_gameplans(config) -> None:
    """After `worms apply` of the gameplans: the host's copy checked here and the dashboard's list
    brought up to date (staff choose a round's gameplan from it), as the conductor did when it
    started. On the host only: a laptop leaves it to the next `worms round start`."""
    if not on_the_host() or not os.path.isdir(os.path.join(HOST_WORKSPACE, "gameplans")):
        print(output.color("gameplans: checked at the next `worms round start` (on the host)", "dim"))
        return
    scripts = str(Path(config.repo_root) / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from orchestrator.rounds.conductor import Conductor

    jobs = LocalJobs(HOST_WORKSPACE, lambda run=None: Path("/var/log/worms/jobs"), echo=lambda text: None)
    valid = Conductor(jobs=jobs).check_gameplans()
    print(output.status_cell("ok" if valid else "warn", "gameplans checked on the host: "
                             + (", ".join(f"{name} {sha[:12]}" for name, sha in sorted(valid.items())) or "none valid")
                             + " (`worms show gameplans`)"))  # fmt: skip
