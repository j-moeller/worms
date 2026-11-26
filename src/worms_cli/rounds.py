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

Then the host's steps, run in this process as the round agent ran them
(scripts/round_agent/handlers.py), with the round agent's environment and the WORMS key in an
ssh-agent of its own (as worms-host-run gives the agent):

    build, snapshot                    the round's sandbox, once (none when it is there with its snapshot)
    for each team:
        rollback                       back to the snapshot (not for the first team after it)
        prepare, check, the gameplan   (deploy, flags, files, observe), the attempt's end
    delete_snapshot, teardown          once every team's attempt is over without an error (asked)

A failure never starts it over: a failed build is kept (asked) and the next session goes on
after OpenTofu's apply; a failed attempt leaves the sandbox and its snapshot for `--resume`.
Each step's commands and output go to its own file in the session's folder, ~/.worms/rounds/; the
terminal shows what each step does, its log's path when it starts and when it ends, and how it
ends. What failed, why (scripts/round_agent/failure.py: the cause in the step's whole output) and
in which file the rest is: the session's summary.log (worms_cli/summary.py; the newest session's is
always ~/.worms/rounds/latest/summary.log), and each failed attempt's row for the staff pages. An
attempt that ends with an error stops the round there (the next team's would most likely meet it
too): `--resume` runs a new attempt of that team from the snapshot, then the teams still to come.
Once every team finished without an error the results may be published; then the round's sandbox
destroyed, the devboxes, the round agent and the orchestrator started again: each asked. The teams see nothing of the round's attempts until its results are published. Ctrl-C fails
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
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from worms_cli import output
from worms_cli.build import BuildError

AGENT_ENV = "/etc/worms/round-agent.env"  # ansible/roles/host/round_agent: the agent's environment
# The host's steps import these beyond the standard library. The round agent ran them on the host's
# system Python, whose packages its role installs (round_agent_python_packages); a venv may lack one.
HOST_STEP_MODULES = ("bcrypt", "cryptography", "yaml")
SYSTEM_PACKAGES = "/usr/lib/python3/dist-packages"
HOST_WORKSPACE = "/opt/worms"  # the round agent's workspace: the sandbox stack's state, the gameplans
HEARTBEAT = 30  # s: a step's heartbeat while it runs, as the round agent's (scripts/round_agent/agent.py)
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
    a file of the session's folder. The conductor's `jobs` (orchestrator/rounds/conductor.py).

    While a step runs its row names this process (services.this_process) and beats every
    HEARTBEAT seconds; however the step ends (an error, Ctrl-C twice, SIGTERM or SIGHUP: start's
    _end_on_signals) its row is closed. Only a SIGKILL leaves it marked running, and the next
    session knows it for that at once (services.left_behind: its process is gone)."""

    def __init__(self, repo_root: str, folder, echo=print, summary=None):
        from round_agent import agent, failure, handlers, validation
        from round_agent.executor import CommandError, Executor

        self.handlers, self.validation, self.failure, self.CommandError = handlers, validation, failure, CommandError
        self.cfg = agent.config(dict(os.environ))
        self.executor = Executor(repo_root, log_dir=str(folder()), latest=False)
        self.folder = folder  # run -> the folder its steps' files go to
        self.echo = echo
        self.summary = summary  # the session's summary.log (worms_cli/summary.py), or None
        self.last = {}  # the last step's kind, job, log and (failed) its summary.log entry
        self.count = 0
        self._running = set()  # the rows of the steps under way: the heartbeat's
        self._beat = None

    def stop_now(self) -> None:
        """The session ends in an attempt (Conductor.interrupted): what its teardown does on the
        host, at once and with no database (it may be what failed): the sandbox's VMs stopped and
        scoring's and logging's current team cleared. Each part on its own: one that fails does
        not keep the other from running."""
        h, ex = self.handlers, self.executor
        for what, act in (("the sandbox's VMs", lambda: h.snapshots.stop(ex, h.snapshots.pool_vms(ex, "sandbox"))),
                          ("scoring's and logging's current team", lambda: h._clear_current_team(ex, self.cfg))):  # fmt: skip
            try:
                act()
            except Exception as exc:  # noqa: BLE001  (said; the conductor's closing tries again)
                self.echo(output.color(f"  not done at once: {what} ({exc})", "yellow"))

    def _beating(self, job_id, on=True):
        if on:
            self._running.add(job_id)
            if self._beat is None:
                self._beat = threading.Thread(target=self._heartbeat, name="host-step heartbeat", daemon=True)
                self._beat.start()
        else:
            self._running.discard(job_id)

    def _heartbeat(self):
        from core.models.rounds import HostJob
        from django.db import connection
        from django.utils import timezone

        while True:
            time.sleep(HEARTBEAT)
            ids = list(self._running)
            if not ids:
                continue
            try:
                HostJob.objects.filter(pk__in=ids, state=HostJob.State.RUNNING).update(heartbeat_at=timezone.now())
            except Exception as exc:  # noqa: BLE001  (beats again in HEARTBEAT seconds, on a new connection)
                log.warning("the heartbeat of host job(s) %s: %s", ids, exc)
                connection.close()

    def request(self, run, kind, **params):
        """Done when it returns (the watch's observe: AttemptBackend._ask)."""
        return self._do(run, kind, params)

    def run(self, run, kind, within=None, **params):
        from core.models.rounds import HostJob
        from gameplan.definition import RoundError

        job = self._do(run, kind, params)
        if job.state != HostJob.State.DONE:
            error = RoundError(f"{kind} failed: {job.error.removeprefix(f'the {kind} job failed: ')}"[:1000])
            # Why, for the attempt's row (conductor.failure_of): the dashboard's staff pages.
            error.failure = (job.result or {}).get("failure") or {"step": kind, "job": job.id, "headline": job.error[:400]}
            raise error
        return job.result or {}

    def context(self, run):
        """Where the session keeps what it wrote (for a failure's account: conductor._where)."""
        where = {"session": str(self.folder(None))}
        if self.summary is not None:
            where["summary"] = str(self.summary.path)
        return where

    @staticmethod
    def _who(run) -> str:
        return f"{run.team.name}, attempt {run.attempt}" if run is not None else "the round"

    # What each of a round's steps does, said as it starts (the conductor: build_sandbox, run_attempt).
    STEPS = {
        "build": "build(): the round's sandbox, OpenTofu's apply and the configuration (once per round)",
        "snapshot": "snapshot(): every sandbox VM's clean snapshot, before any team's code",
        "rollback": "load_snapshot(): every sandbox VM back to its clean snapshot",
        "prepare": "prepare(): this team's secrets, files and flags",
        "check": "check(): the host, and probes inside every VM",
        "delete_snapshot": "delete_snapshot(): the sandbox's clean snapshots deleted",
    }

    @classmethod
    def _what(cls, kind, params) -> str:
        if kind == "teardown":
            return ("the attempt's end: its secrets gone, its VMs off, the sandbox and its snapshot kept" if params.get("keep")
                    else "teardown(): the round's sandbox destroyed")  # fmt: skip
        return cls.STEPS.get(kind, "")

    def _ended(self, run, kind, job_id, state, result, error, cause, path, quiet, took, caught=None) -> dict:
        """A step's end, said: on the terminal (how it ended, how long it took, its log again, and
        for a failure what of its output says why) and in summary.log, both on the host: all of
        it. A failure's account goes into the job's result (the dashboard's staff pages) and to
        run() for the attempt's row: without what a gameplan's step said (failure.for_database)."""
        from core.models.rounds import HostJob

        from worms_cli.summary import _took

        who = self._who(run)
        if not quiet or state != HostJob.State.DONE:
            self.last = {"kind": kind, "job": job_id, "log": path}
        if state == HostJob.State.DONE:
            if not quiet:
                self.echo(output.status_cell("ok", f"{kind} #{job_id} done in {_took(took)} — its log: {path or 'none'}"))
                if self.summary is not None:
                    self.summary.step(kind, job_id, who, True, took, path)
            return result
        cause = cause or self.failure.Cause(headline=(error or "failed")[:400], public=(error or "failed")[:400])
        told = self.failure.for_database(caught, cause) if caught is not None else cause.as_dict()
        account = {"step": kind, "job": job_id, "log": path, **told, **self.context(run)}
        if self.summary is not None:
            account["entry"] = self.summary.failure(f"{kind} #{job_id} ({who}) failed after {_took(took)}", cause.headline,
                                                    cause.text(), path, cause.logs, listed=False)  # fmt: skip
            self.summary.step(kind, job_id, who, False, took, path, note=f"→ failure [{account['entry']}]")
            self.last["entry"] = account["entry"]
        self.echo(output.status_cell("error", f"{kind} #{job_id} ({who}) failed after {_took(took)}: {cause.headline}"))
        lines = cause.text().splitlines()
        for line in lines[:25]:
            self.echo(output.color(f"      {line}", "red"))
        if len(lines) > 25:
            self.echo(output.color(f"      ... ({len(lines) - 25} more lines: summary.log)", "red"))
        self.echo(output.color(f"    its log:     {path or 'none'}", "yellow"))
        if self.summary is not None:
            self.echo(output.color(f"    summary.log: {self.summary.path}", "yellow"))
        return {**(result or {}), "failure": account}

    def _do(self, run, kind, params):
        from core.models.rounds import HostJob
        from django.utils import timezone

        from worms_cli import services

        now = timezone.now()
        job = HostJob.objects.create(run=run, kind=kind, params=params, state=HostJob.State.RUNNING, started_at=now,
                                     heartbeat_at=now, result={"owner": services.this_process()})  # fmt: skip
        self._beating(job.pk)
        self.count += 1
        name = f"{self.count:03d}-{kind}"
        quiet = kind == "observe"  # every minute of a gameplan: its file only, unless it fails
        state, result, error, interrupted, cause, path, caught = HostJob.State.FAILED, {}, "", None, None, "", None
        began = time.monotonic()
        try:
            self.executor.log_dir = str(self.folder(run))
            with self.executor.job_log(name, kind, job.id) as job_log:
                path = str(job_log.path or "")
                if not quiet:
                    # Where its log goes, absolute, before it starts (and again when it ends: _ended).
                    what = self._what(kind, params)
                    self.echo(output.color(f"  ▸ {kind} #{job.id} ({self._who(run)}){f': {what}' if what else ''} — its log: "
                                           f"{path or 'no log file'}", "bold"))  # fmt: skip
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
                    cause = self.failure.Cause(headline=error[:400], blocks=[error], public=error[:400])
                    job_log.note(f"rejected: {exc}")
                except Exception as exc:  # noqa: BLE001  (its message and the log say why)
                    caught = exc
                    # A quiet handler's (handlers._quiet) names where it failed, not what was said.
                    told = isinstance(exc, self.CommandError) or getattr(exc, "original", None) is not None
                    error = str(exc) if told else f"{type(exc).__name__}: {exc}"
                    cause = self.failure.from_exception(exc)
                    job_log.note(f"failed: {error}")
                    # The file only: the whole traceback (of what a quiet handler hid, too), and
                    # what of the output says why.
                    job_log.write("".join(traceback.format_exception(getattr(exc, "original", None) or exc)))
                    job_log.write(f"what says why (scripts/round_agent/failure.py):\n{cause.text()}\n")
                except BaseException as exc:  # Ctrl-C twice, SIGTERM, SIGHUP (_end_on_signals)
                    job_log.note(f"interrupted: {type(exc).__name__} {exc}".rstrip())
                    raise
        except BaseException as exc:  # the step's own end, or this code's (its terminal gone, ...)
            interrupted = exc
            state, result = HostJob.State.FAILED, {}
            error = error or f"the session ended in the middle of it ({type(exc).__name__}{f': {exc}' if str(exc) else ''})"
            raise
        finally:
            self._beating(job.pk, False)
            try:
                result = self._ended(run, kind, job.id, state, result, error, cause, path, quiet, time.monotonic() - began,
                                     caught)  # fmt: skip
            except Exception:  # noqa: BLE001  (what is said of a step never changes how it ends)
                log.exception("host job #%s: its end not said", job.pk)
            try:
                HostJob.objects.filter(pk=job.pk).update(state=state, result=json.loads(json.dumps(result, default=str)),
                                                         error=error[:4000], finished_at=timezone.now(), heartbeat_at=timezone.now())  # fmt: skip
            except Exception:
                if interrupted is None:
                    raise
                log.warning("host job #%s is left marked running (the database did not take its end): the next "
                            "`worms round start` closes it", job.pk)  # fmt: skip
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


class SessionMark:
    """This session's mark on its round (Round.session_seen_at): while it is fresh, staff cannot
    change the round on the dashboard. Written at once and every SESSION_BEAT seconds by a thread
    of its own, whatever the session waits for; cleared when the session ends. A session killed
    outright leaves a mark that is stale after SESSION_STALE seconds."""

    def __init__(self, round_):
        self.model, self.pk = type(round_), round_.pk
        self.done = threading.Event()
        self.write()
        self.thread = threading.Thread(target=self._beat, name="round-session-mark", daemon=True)
        self.thread.start()

    def write(self, alive=True):
        from django.utils import timezone

        self.model.objects.filter(pk=self.pk).update(session_seen_at=timezone.now() if alive else None)

    def _beat(self):
        from django.db import connection

        try:
            while not self.done.wait(self.model.SESSION_BEAT):
                try:
                    self.write()
                except Exception as exc:  # noqa: BLE001  (the database away a moment: the next beat)
                    log.warning("the round's session mark was not renewed: %s", exc)
        finally:
            connection.close()

    def stop(self):
        self.done.set()
        self.thread.join(timeout=10)
        try:
            self.write(alive=False)
        except Exception as exc:  # noqa: BLE001  (stale in SESSION_STALE seconds all the same)
            log.warning("the round's session mark was not cleared: %s", exc)


class Session:
    def __init__(self, config, round_, yes=False):
        from worms_cli.summary import Summary

        self.config, self.round, self.yes = config, round_, yes
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = directory() / f"{round_.id}-{stamp}"
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.dir.parent, 0o700)
        # The one file that says what failed, why and where the rest is (worms_cli/summary.py).
        self.summary = Summary(self.dir, f"round {round_.id} {round_.name!r}", " ".join(["worms", *sys.argv[1:]]))
        self.conductor = None
        self.jobs = None
        self.interrupts = 0
        self.stopped_on_failure = False  # an attempt ended with an error: the round stopped there (_after_failed_attempt)

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


def _chosen(session, args, replanned=False):
    """The attempts this session runs, in order: a first session's every one; --resume, every team
    that has not finished; --team, the named teams'; after the round changed (`replanned`), every
    team that has not finished, as --resume. A failed one's team gets a new attempt once the round
    is ready (_run). None: nothing to run, or refused."""
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
    elif args.resume or replanned:
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
        account = run.failure or {}
        print(f"{run.team.name}'s attempt {run.attempt} failed {run.failure_phase()}: {(account.get('headline') or run.error or '')[:300]}")
        if account.get("log") or account.get("summary"):
            print(output.color(f"    its log: {account.get('log') or '—'}; that session's summary.log: {account.get('summary') or '—'}", "dim"))
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


def _host_jobs(agent_stopped=False):
    """(queued, running, left): left, [(job, why)], marked running but run nowhere any more
    (services.left_behind): an earlier session's, or a killed agent's."""
    from core.models.rounds import HostJob

    from worms_cli.services import left_behind

    queued = list(HostJob.objects.filter(state=HostJob.State.QUEUED).order_by("id"))
    running, left = [], []
    for job in HostJob.objects.filter(state=HostJob.State.RUNNING).order_by("id"):
        why = left_behind(job, agent_stopped)
        if why:
            left.append((job, why))
        else:
            running.append(job)
    return queued, running, left


def _describe(job) -> str:
    from worms_cli.jobs import describe

    return describe(job)


def _running_by(job) -> str:
    """A running job, and who runs it."""
    from django.utils import timezone

    owner = job.result.get("owner") if isinstance(job.result, dict) else None
    beat = job.heartbeat_at or job.started_at
    said = _describe(job) + (f", run by `{owner.get('by')}` (pid {owner.get('pid')} on {owner.get('host')})"
                             if isinstance(owner, dict) else ", run by the round agent")  # fmt: skip
    return said + (f", its heartbeat {int((timezone.now() - beat).total_seconds())} s ago" if beat else "")


# What a round session stopped and has not started again, and the session (its pid): written as it
# stops each, each taken off as it is started again, gone at the session's end when nothing is
# left. A session that ends without starting them (Ctrl-C twice, SIGTERM, SIGHUP, an error, a
# SIGKILL) leaves it: `worms status` says what is still stopped and what starts it (manage.py).
STOPPED_RECORD = "/var/lib/worms/round-stopped.json"
START_AGAIN = {  # in the order they are started again
    "devboxes": ("the devboxes", "worms vm start devboxes", "Start the devboxes again?"),
    "agent": ("the round agent", "worms service start agent", "Start the round agent again?"),
    "orchestrator": ("the orchestrator", "worms service start orchestrator", "Start the orchestrator again?"),
}


def stopped_record() -> dict:
    """{round, round_id, pid, since, stopped: [orchestrator|agent|devboxes]}, or {}."""
    try:
        record = json.loads(Path(STOPPED_RECORD).read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _note(session, stopped=(), started=()) -> list:
    """The record, this session's (what an earlier one left stopped stays in it). Returns what
    it says is still stopped."""
    from datetime import timezone

    record = stopped_record()
    names = [name for name in record.get("stopped", []) if name in START_AGAIN and name not in started]
    names += [name for name in stopped if name not in names]
    record = {"round": session.round.name, "round_id": session.round.id, "pid": os.getpid(),
              "since": record.get("since") or datetime.now(timezone.utc).isoformat(timespec="seconds"), "stopped": names}  # fmt: skip
    path = Path(STOPPED_RECORD)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix(".tmp").write_text(json.dumps(record) + "\n")
        os.replace(path.with_suffix(".tmp"), path)
    except OSError as exc:
        output.warn(f"could not write {STOPPED_RECORD} ({exc}): `worms status` will not say what this session stopped")
    return names


def _close_record(session) -> None:
    """At the session's end: the record goes when nothing it lists is still stopped."""
    record = stopped_record()
    if record.get("pid") == os.getpid() and not record.get("stopped"):
        Path(STOPPED_RECORD).unlink(missing_ok=True)


def _say_still_stopped(session) -> None:
    """On the way out without _afterwards: what stays stopped, and what starts it again."""
    names = stopped_record().get("stopped", [])
    if not names:
        return
    lines = [f"{START_AGAIN[name][0]}: {START_AGAIN[name][1]}" for name in START_AGAIN if name in names]
    session.summary.note("still stopped (this session did not start them again): " + "; ".join(lines))
    try:
        print(output.color("still stopped, this session did not start them again (`worms status` says so too):\n  "
                           + "\n  ".join(lines), "red"))  # fmt: skip
    except OSError:
        pass  # no terminal any more: summary.log and round.log have it


def _start_again(session, names) -> None:
    """Each of `names` started again, asked: what this session (or an earlier one) stopped."""
    from worms_cli import power, services

    acts = {"devboxes": lambda: power.Power(session.config).start(["devboxes"]),
            "agent": lambda: services.Units(session.config).start(["agent"]),
            "orchestrator": lambda: services.Units(session.config).start(["orchestrator"])}  # fmt: skip
    for name in [name for name in START_AGAIN if name in names]:
        _what, command, question = START_AGAIN[name]
        says(command)
        if ask(question, True, session.yes):
            try:
                acts[name]()
                _note(session, started=[name])
            except BuildError as exc:
                output.error(str(exc))
                print(f"  later: {command}")
        else:
            print(f"  later: {command}")


def _not_run(session) -> int:
    """The round does not run after all (a no, a failed check, the preflight): what was stopped
    for it is started again, asked."""
    names = stopped_record().get("stopped", [])
    if names:
        print(output.heading("start again what was stopped for the round"))
        _start_again(session, names)
    return 1


def _stop_everything(session) -> bool:
    """Ask for each thing that must be stopped, in order; False at the first no."""
    from worms_cli import control, power, services

    yes = session.yes
    _note(session)  # this session's from now: `worms status` sees it run
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
        _note(session, stopped=["orchestrator"])  # first: a stop cut short may have stopped part of it
        units.stop(["orchestrator"])
    else:
        print(output.status_cell("ok", "stopped already"))

    # 2. The round agent and its queue.
    print(output.heading("2. the round agent and its host jobs"))
    if units.active("agent"):
        queued, running, _left = _host_jobs()
        if running:
            print("  running now: " + "; ".join(_describe(job) for job in running) + ": the agent finishes it first")
        print("  Stopped, it takes no host job (devbox resets and rebuilds, key updates) until it is started again.")
        says("worms service stop agent", "worms service start agent")
        if not ask("Stop the round agent?", True, yes):
            print("The round does not start while the round agent runs.")
            return False
        _note(session, stopped=["agent"])
        units.stop(["agent"])
    else:
        print(output.status_cell("ok", "the round agent is stopped already"))
    queued, running, left = _host_jobs(agent_stopped=units.stopped("agent"))
    if queued:
        print("  queued: " + "; ".join(_describe(job) for job in queued))
        print("  Cancelled, they never run (a team asks again for its devbox reset; the gameplans check runs here).")
        says("worms jobs cancel " + " ".join(str(job.id) for job in queued))
        if not ask(f"Cancel these {len(queued)} queued host job(s)?", True, yes):
            print("The round does not start while host jobs are queued.")
            return False
        control.cancel_jobs(numbers=[job.id for job in queued])
    if left:
        print("  marked running, but run nowhere any more (what ran them stopped half-way):")
        for job, why in left:
            print(f"    {_describe(job)}: {why}")
        says("worms jobs cancel " + " ".join(str(job.id) for job, _why in left))
        if not ask(f"Mark these {len(left)} job(s) failed?", True, yes):
            print("The round does not start while host jobs are marked running.")
            return False
        control.cancel_jobs(numbers=[job.id for job, _why in left], agent_stopped=units.stopped("agent"))
    if running:
        print("  running now: " + "; ".join(_running_by(job) for job in running))
        print("The round does not start while another host job runs: let it end (or stop what runs it).")
        return False

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
        _note(session, stopped=["devboxes"])
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
    queued, running, left = _host_jobs(agent_stopped=units.stopped("agent"))
    jobs = [_describe(job) for job in queued] + [_running_by(job) for job in running]
    jobs += [f"{_describe(job)}: {why} (`worms jobs cancel {job.id}`)" for job, why in left]
    checks = [
        ("the orchestrator's services are stopped", not units.active("orchestrator"), ", ".join(units.active("orchestrator"))),
        ("the round agent is stopped", not units.active("agent"), "worms-round-agent is active"),
        ("no host job is queued or running", not jobs, "; ".join(jobs)),
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


def _end_on_signals():
    """SIGTERM, or SIGHUP (the terminal or its SSH connection gone): out through every `finally`,
    as Ctrl-C twice is: the step under way marked failed (LocalJobs), its command stopped, the
    ssh-agent too. A SIGKILL cannot be; the next session closes what it leaves."""

    def ended(signum, frame):
        raise SystemExit(f"ended by {signal.Signals(signum).name}")

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, ended)


def _run(session, runs) -> list:
    """Each attempt in turn, on the round's sandbox (built and snapshotted before: _build): the
    first after the snapshot as it is, every other one rolled back to it first (the conductor's
    run_attempt). The runs as they ended."""
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
        # Its preflight: the session's, before the build, covers the first attempt (after a new
        # snapshot its sandbox runs, as the snapshot left it); every other one has its own.
        problem = None if conductor.fresh or conductor.checked == run.team_id else conductor.preflight(run, ask=_without(session))
        if problem:
            then = f"mend it, then `worms round start {session.round.id} --resume` runs the teams still to come"
            last = session.jobs.last if session.jobs.last.get("kind") == "preflight" else {}
            session.summary.failure(f"before {run.team.name}'s attempt {run.attempt}: the preflight; the round stopped "
                                    "there (no attempt used up)", problem, "", last.get("log", ""), (), then,
                                    refers=last.get("entry"))  # fmt: skip
            print(output.status_cell("error", f"before {run.team.name}'s attempt: {problem}"))
            print(f"The round stops here: no attempt was used up. {then[0].upper()}{then[1:]}.")
            print(output.color(f"    the preflight's log: {last.get('log') or '(none)'}\n    summary.log: {session.summary.path}", "yellow"))
            break
        began = time.monotonic()
        run = conductor.attempt(run)
        _count()
        took = timedelta(seconds=int(time.monotonic() - began))
        _attempt_ended(session, run, took)
        ended.append(run)
        if run.state == S.FAILED or run.error:
            # Any error stops the round: the next team's attempt would most likely meet it too.
            _after_failed_attempt(session, run, runs[index:])
            break
    return ended


def _without(session):
    """The preflight's question when parts of the private internet are off (all its other checks
    passed): run the round without them? Asked once a session; its yes holds for the later
    preflights (the conductor's `accepted`). Never DNS, apt-cache or devpi: those fail it."""

    def ask_it(missing):
        names = [check.removeprefix("services:").removesuffix(":running") for check, _detail in missing]
        print(output.status_cell("warn", f"the private internet: {', '.join(names)} not running ("
                                 + "; ".join(f"{name}: {detail or 'off'}" for name, (_c, detail) in zip(names, missing)) + ")"))
        print("  The round can run without them: teams' worms cannot reach what they serve. They stay off: the "
              "sandbox's build rolls them back to their snapshot with the other service VMs and turns them off again.")  # fmt: skip
        says(f"worms vm start {' '.join(names)}")
        yes = ask(f"Run the round without {', '.join(names)}?", False, session.yes)
        session.summary.note(f"the preflight: {', '.join(names)} off, " + ("accepted by the operator" if yes else "not accepted"))
        return yes

    return ask_it


def _attempt_ended(session, run, took):
    """An attempt's end, said: on the terminal (how, why, the log with the rest, summary.log) and
    in summary.log. A failure a host step caused names that step's entry instead of repeating it."""
    from core.models.rounds import RoundRun

    S = RoundRun.State
    who = f"{run.team.name}, attempt {run.attempt}"
    failure = run.failure or {}
    phase = run.failure_phase() or "(its phase not recorded)"
    log_file = failure.get("log") or str(session.dir / "round.log")
    if run.state == S.FINISHED and not run.error:
        print(output.status_cell("ok", f"{run.team.name}: finished in {took}"))
        session.summary.note(f"✓ {who}: finished in {took}")
        return
    if run.state == S.FINISHED:
        what = f"{who}: finished in {took}, its results stand, but its teardown failed"
        then = "the host may hold leftovers of it: `worms vm` lists them; `worms round teardown` destroys the sandbox"
        print(output.status_cell("warn", f"{run.team.name}: finished in {took}, but its teardown failed: {failure.get('headline') or run.error}"))
    else:
        what = f"{who}: {run.state} after {took}, {phase}"
        then = (f"mend the cause, then `worms round start {session.round.id} --resume` runs a new attempt (a change to "
                "Ansible, the gameplans or the host's code goes to the host with `worms apply round-agent` first)")  # fmt: skip
        print(output.status_cell("error", f"{run.team.name}: {run.state} after {took}, {phase}"))
        print(output.color(f"    why:  {failure.get('headline') or run.error}", "red"))
    print(output.color(f"    log:  {log_file}" + (f" (step {failure['step']} #{failure.get('job')})" if failure.get("step") else ""), "yellow"))
    print(output.color(f"    summary.log: {session.summary.path}", "yellow"))
    session.summary.failure(what, failure.get("headline") or run.error, failure.get("detail") or run.error, log_file,
                            failure.get("logs") or (), then, refers=failure.get("entry"))  # fmt: skip


def _after_failed_attempt(session, run, left):
    """Any error of an attempt stops the round there: the next team's attempt would most likely
    meet it too. The sandbox and its snapshot stay: `--resume` runs a new attempt of this team
    from the snapshot (not a build), then the teams still to come."""
    waiting = [other.team.name for other in left if other.pk != run.pk]
    print(output.heading(f"{run.team.name}'s attempt {run.attempt} ended with an error: the round stops here"))
    print(f"  Not run: {', '.join(waiting) or 'no other team'}. The sandbox and its snapshot are kept: `--resume` starts "
          "from the snapshot, it is not built again.")  # fmt: skip
    print(f"  Mend the cause (what failed and why: {session.summary.path}; its steps: {session.folder(run)}). A change "
          "to Ansible, the gameplans or the host's code goes to the host with `worms apply round-agent`. Then: "
          f"`worms round start {session.round.id} --resume`.")  # fmt: skip
    says(f"worms round start {session.round.id} --resume")
    session.summary.note(f"the round stopped after {run.team.name}'s attempt {run.attempt}: its error would most likely "
                         f"stop the next team's too; not run: {', '.join(waiting) or 'no other team'}")  # fmt: skip
    session.stopped_on_failure = True


def _build(session, run) -> bool:
    """build(), then snapshot(): once, before the first attempt (the conductor's build_sandbox).
    True once the sandbox is there with its snapshot; else the round stops here, no attempt
    used up, and an unfinished build is kept (asked) for the next session to continue."""
    from gameplan.definition import RoundError

    print(output.heading("7. the round's sandbox: build, then its snapshot (once; every team starts from the snapshot)"))
    try:
        built = session.conductor.build_sandbox(run)
    except RoundError as error:
        session.summary.failure("the round's sandbox: its build or its snapshot; the round did not start (no attempt "
                                "used up)", str(error), log=session.jobs.last.get("log", ""), refers=session.jobs.last.get("entry"),
                                next_step=f"mend the cause, then `worms round start {session.round.id} --resume`: it goes on "
                                          "after OpenTofu's apply (asked)")  # fmt: skip
        _after_failed_build(session)
        return False
    print(output.status_cell("ok", f"the round's sandbox: {built.get('sandbox', 'ready')}"
                             + ("; its snapshot taken" if session.conductor.fresh else "")))  # fmt: skip
    session.summary.note(f"✓ the round's sandbox: {built.get('sandbox', 'ready')}" + ("; its snapshot taken" if session.conductor.fresh else ""))
    return True


def _after_failed_build(session):
    """The build or the snapshot failed: the VMs it made are kept (asked), for the next session to
    go on after OpenTofu's apply once the cause is mended; else destroyed."""
    record = session.jobs.handlers.unfinished_build(session.jobs.cfg)
    print(output.heading("the round's sandbox was not finished: the round does not start"))
    if record:
        done = "configured, its snapshot not taken" if record.get("configured") else "not configured to its end"
        print(f"  The build begun {record['began']} is {done}; no worm has run on it. Its VMs are kept. Kept, "
              "`--resume` goes on with it: OpenTofu's apply (nothing to change) and the configuration again, or "
              "only its snapshot, never a build from nothing.")  # fmt: skip
        if ask("Keep them, to go on with the build once the cause is mended?", True, session.yes):
            print(output.color(f"    later: worms round start {session.round.id} --resume (goes on with it); worms round "
                               "teardown (destroys it)", "dim"))  # fmt: skip
        else:
            try:
                session.conductor.destroy_sandbox(session.round.id)
                print(output.status_cell("ok", "the unfinished sandbox is destroyed: the next session builds it anew"))
            except Exception as exc:  # noqa: BLE001
                output.error(f"not destroyed: {exc}; later: `worms round teardown`")
    print(f"  Mend the cause (what failed and why: {session.summary.path}). A change to Ansible, the gameplans or the "
          f"host's code goes to the host with `worms apply round-agent`. Then: `worms round start {session.round.id} --resume`.")  # fmt: skip


def _summary(session):
    print(output.heading(f"round {session.round.name!r}: where each team stands"))
    latest = _latest_runs(session.round)
    _say_round(session.round, latest)
    session.summary.end(_tally(latest) or "nothing ran")
    print(output.color(f"this session's folder: {session.dir} (`worms log round {session.round.id}`)", "dim"))
    _say_summary(session)


def _say_summary(session):
    """Where summary.log is, and how many failures it holds."""
    count = len(session.summary.failures)
    print(output.status_cell("error" if count else "ok", f"summary.log: {session.summary.path} "
                             f"({count} failure(s); `worms log round {session.round.id} summary`)"))  # fmt: skip


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
    from worms_cli import power

    yes = session.yes
    sandbox = [vm["name"] for vm in power.Power(session.config).runner.vms() if vm.get("pool") == "sandbox"]
    record = session.jobs.handlers.unfinished_build(session.jobs.cfg) if session.jobs else None
    if record:
        print(output.heading("the round's sandbox"))
        print(f"  an unfinished build (begun {record['began']}), kept: `worms round start {session.round.id} --resume` "
              "continues it, `worms round teardown` destroys it")  # fmt: skip
    elif sandbox:
        # delete_snapshot(), teardown(): once every team's attempt is over without an error. Before,
        # the sandbox and its snapshot stay for `--resume`, which starts from the snapshot.
        print(output.heading("the round's sandbox: its snapshot deleted, then destroyed"))
        complete = _complete(_latest_runs(session.round))
        if complete:
            print(f"  Every team's attempt is over without an error: the sandbox ({len(sandbox)} VMs) and its snapshot are "
                  "not needed any more.")  # fmt: skip
        else:
            print(f"  Not every team's attempt is over without an error: the sandbox ({len(sandbox)} VMs, powered off) and "
                  f"its snapshot are kept, `worms round start {session.round.id} --resume` starts from the snapshot "
                  "(minutes), not from a new build (an hour).")  # fmt: skip
        says("worms round teardown")
        if ask("Delete the sandbox's snapshot and destroy the sandbox now?", complete, yes and complete):
            try:
                session.conductor.destroy_sandbox(session.round.id)
                print(output.status_cell("ok", "the snapshot deleted, the sandbox destroyed"))
                session.summary.note("✓ the round's sandbox: its snapshot deleted, the sandbox destroyed")
            except Exception as exc:  # noqa: BLE001
                output.error(f"not destroyed: {exc}; later: `worms round teardown`")
        else:
            print("  kept; later: worms round teardown")
    print(output.heading("start again what was stopped"))
    _start_again(session, list(START_AGAIN))


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
    if round_.session_running(timezone.now()):
        raise BuildError(f"another `worms round start` runs round {round_.name!r} (its mark is from "
                         f"{round_.session_seen_at:%H:%M:%S} UTC): one session per round")  # fmt: skip
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
    key = mark = None
    _end_on_signals()
    try:
        # From here until it ends, staff cannot change the round on the dashboard.
        mark = SessionMark(round_)
        print(output.color(f"this session's folder: {session.dir} (round.log: what this terminal shows)", "dim"))
        print(output.color(f"what failed, why, and in which file the rest is: {session.summary.path} (always the "
                           f"newest: {directory() / 'latest' / 'summary.log'})", "bold"))  # fmt: skip
        if not (os.environ.get("TMUX") or os.environ.get("STY")):
            print(output.color("not inside tmux or screen: a dropped SSH connection ends this session, and the attempt "
                               "under way fails (`tmux new -s round`, then this command, keeps it)", "yellow"))  # fmt: skip
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

        # This checkout's code in the host's workspace, before anything is stopped (T10); then the
        # round agent's environment and code, as the service has them.
        _sync_workspace(config)
        repo_root, session.jobs, key = _host_steps(config, session.folder, session.summary)
        settings.PROXMOX["node"] = config.proxmox_node()
        session.conductor = Conductor(jobs=session.jobs, power=ProxmoxPower(api=Pvesh()), on_watch=_count)

        left = list(RoundRun.objects.filter(round=round_, state__in=ACTIVE).select_related("team", "round"))
        for run in left:
            print(f"{run.team.name}'s attempt {run.attempt} was left {run.state} by an earlier session (it was stopped).")
            if not ask("Fail it and stop its VMs (its sandbox kept)?", True, args.yes):
                return 1
            session.conductor.abandon(run)
        # Staff may have changed the round since its attempts (its gameplan, the gameplan's version,
        # the duration per team, the start): the teams whose attempt no longer matches, asked.
        replanned = False
        stale = session.conductor.stale(round_)
        if stale:
            print(output.heading("the round changed since these attempts"))
            for run, why in stale:
                print(f"  {run.team.name}, attempt {run.attempt} ({run.state}): {'; '.join(why)}")
            ran = sum(1 for run, _why in stale if run.state != RoundRun.State.PENDING)
            if not ask("Run them as the round is now?"
                       + (f" The {ran} attempt(s) that ran or were skipped are kept as superseded (no longer counted); "
                          "their teams get a new attempt." if ran else "")
                       + " Attempts still to come take the round's gameplan and the team's latest upload.", False, args.yes):  # fmt: skip
                print(output.color("Nothing changed, nothing runs. Set the round back on Staff -> Rounds, or answer yes.", "yellow"))
                return 1
            session.conductor.replan(round_, stale)
            replanned = True
            _say_round(round_, _latest_runs(round_))
        runs = _chosen(session, args, replanned)
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
            session.summary.failure("the gameplan: not fixed for the round's attempts, nothing ran", problem,
                                    log=session.jobs.last.get("log", ""), refers=session.jobs.last.get("entry"))  # fmt: skip
            return 1
        pinned = (RoundRun.objects.filter(round=round_).exclude(gameplan_sha256="").exclude(state=RoundRun.State.SUPERSEDED)
                  .values_list("gameplan_sha256", flat=True).first())  # fmt: skip
        print(output.status_cell("ok", f"{round_.gameplan}, version {pinned[:12]}: valid at {round_.team_duration} s per team"))
        record = session.jobs.handlers.unfinished_build(session.jobs.cfg)
        if record:
            print(output.heading("an unfinished sandbox build"))
            print(f"  begun {record['began']} for {record['gameplan']}"
                  + (f", configured {record['configured']}" if record.get("configured") else "")
                  + "; no worm has run on it. Continued: its VMs are kept, "
                  + ("only its snapshot is taken" if record.get("configured") else
                     "OpenTofu's apply and the configuration run again (minutes, not the whole build)")
                  + ". Not: it is destroyed and built anew.")  # fmt: skip
            if ask("Continue that build?", True, args.yes):
                session.conductor.resume_build = True

        if not _stop_everything(session) or not _verified(session):
            return _not_run(session)
        print(output.heading("5. the host's preflight"))
        first = next((run for run in runs if run.upload_id is not None), None)
        if first is not None:
            problem = session.conductor.preflight(RoundRun.objects.select_related("round", "team").get(pk=first.pk),
                                                 ask=_without(session))
            if problem:
                output.error(problem)
                last = session.jobs.last if session.jobs.last.get("kind") == "preflight" else {}
                session.summary.failure("the host's preflight: the round did not start", problem, log=last.get("log", ""),
                                        next_step="mend it (memory, disk: its log says which check), then the same command again",
                                        refers=last.get("entry"))  # fmt: skip
                print(output.color(f"    its log: {last.get('log') or '(none)'}\n    summary.log: {session.summary.path}", "yellow"))
                return _not_run(session)
            print(output.status_cell("ok", "the host is ready (memory, disk, the sandbox's needs)"))
        minutes = sum(run.duration or round_.team_duration for run in runs if run.upload_id) // 60
        print(output.heading("6. the round"))
        print(f"  {len(runs)} team(s): {', '.join(run.team.name for run in runs)}; the gameplan about {minutes} min, "
              "plus the sandbox (built once, about an hour; a rollback to its snapshot before every team but the "
              "first, minutes) and about 15 min per team")  # fmt: skip
        if not ask(f"Run round {round_.name!r} now? This terminal stays with it to the end.", False, args.yes):
            return _not_run(session)
        _interrupt(session)
        if not _build(session, RoundRun.objects.select_related("round", "team").get(pk=first.pk)):  # pinned since
            signal.signal(signal.SIGINT, signal.default_int_handler)
            _summary(session)
            _afterwards(session)
            _say_summary(session)
            return 1
        _run(session, runs)  # it stops at the first attempt that ends with an error (_after_failed_attempt)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        _summary(session)
        _offer_publish(session)
        _afterwards(session)
        _say_summary(session)
        return 1 if session.stopped_on_failure else 0
    except KeyboardInterrupt:
        print(output.color("\nout at once: an attempt under way is marked failed, not counted against its team (its VMs "
                           f"stopped, unless Ctrl-C cut that short). `worms round start {round_.id} --resume` goes on; "
                           "`worms round teardown` destroys the sandbox.", "red"))  # fmt: skip
        _say_still_stopped(session)
        session.summary.end("out at once (Ctrl-C twice): the attempt under way marked failed")
        return 130
    except SystemExit as exc:  # _end_on_signals
        try:
            print(output.color(f"\n{exc}: the step and the attempt under way are marked failed (not counted against "
                               f"the team). `worms round start {round_.id} --resume` goes on.", "red"))  # fmt: skip
        except OSError:
            pass  # no terminal any more: round.log has it
        _say_still_stopped(session)
        session.summary.end(f"{exc}: the step and the attempt under way marked failed")
        raise
    except Exception as exc:
        # The session's own error (a BuildError says what to do; anything else, where it was raised).
        headline, detail = str(exc), ""
        if not isinstance(exc, BuildError):
            try:
                from round_agent import failure

                cause = failure.from_exception(exc)
                headline, detail = cause.headline, cause.text()
            except ImportError:  # before the host's steps were set up
                headline, detail = f"{type(exc).__name__}: {exc}", traceback.format_exc()[-3000:]
        session.summary.failure("the session itself stopped", headline, detail, str(session.dir / "round.log"))
        _say_still_stopped(session)
        session.summary.end("stopped by an error")
        print(output.color(f"summary.log: {session.summary.path}", "yellow"), file=sys.stderr)
        raise
    finally:
        if session.summary.status == "running":
            session.summary.end("ended before the round ran: a question answered no, or a check failed (round.log has it)")
        _close_record(session)
        if mark is not None:
            mark.stop()
        if key is not None:
            key.stop()
        tee.stop()


def _python_for_host_steps() -> None:
    """Before anything is stopped: every module the host's steps import is here. One this venv lacks
    is taken from the host's system packages (the round agent's own: bcrypt, for a gameplan user's
    encrypted SSH key, was missing on 2026-11-23 and failed a prepare after its build); one missing
    there too is a BuildError that names it."""
    import importlib.util

    missing = [name for name in HOST_STEP_MODULES if importlib.util.find_spec(name) is None]
    if missing and os.path.isdir(SYSTEM_PACKAGES) and SYSTEM_PACKAGES not in sys.path:
        sys.path.append(SYSTEM_PACKAGES)  # last: this venv's own packages come first
        importlib.invalidate_caches()
        found = [name for name in missing if importlib.util.find_spec(name) is not None]
        if found:
            print(output.color(f"from the host's system packages ({SYSTEM_PACKAGES}), as the round agent has them: "
                               f"{', '.join(found)}", "dim"))  # fmt: skip
        missing = [name for name in missing if name not in found]
    if missing:
        raise BuildError(f"the round's steps need the Python module(s) {', '.join(missing)}, in neither this venv nor "
                         f"{SYSTEM_PACKAGES}: `apt install python3-{missing[0]}` (the round agent's role installs them)")  # fmt: skip


def _system_path() -> None:
    """The host's steps start python3 (`worms step`), ansible-playbook and tofu by name: the host's
    own, as the round agent has them, never an active venv's. A venv's newer Ansible (core 2.21)
    refused a task the host's (2.19) runs, and failed a prepare on 2026-11-23. Says which are used."""
    import shutil

    venvs = {os.path.realpath(os.path.join(prefix, "bin")) for prefix in (sys.prefix, os.environ.get("VIRTUAL_ENV", ""))
             if prefix and os.path.realpath(prefix) != os.path.realpath(sys.base_prefix)}  # fmt: skip
    parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part and os.path.realpath(part) not in venvs]
    os.environ["PATH"] = os.pathsep.join(parts) or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    os.environ.pop("VIRTUAL_ENV", None)
    tools = {name: shutil.which(name) for name in ("python3", "ansible-playbook", "tofu")}
    print(output.color("the host's steps run " + ", ".join(f"{name} {path or '(missing)'}" for name, path in tools.items()), "dim"))
    missing = [name for name, path in tools.items() if not path]
    if missing:
        raise BuildError(f"the host lacks {', '.join(missing)} on its own PATH (the round agent's role installs them)")


def _host_steps(config, folder, summary=None):
    """The round agent's environment and code, as the service has them, and an ssh-agent with the
    WORMS key: (repo_root, LocalJobs, KeyAgent). The KeyAgent is the caller's to stop. `summary`:
    the session's summary.log, which each step's end goes to."""
    _python_for_host_steps()
    environment = _agent_environment()
    os.environ.update({k: v for k, v in environment.items() if k not in ("PWD", "SHLVL", "_")})
    _system_path()
    repo_root = os.environ.get("REPO_ROOT", HOST_WORKSPACE)
    scripts = str(Path(config.repo_root) / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    key = KeyAgent(os.environ.get("SSH_KEY", "/etc/worms/ssh/worms"))
    return repo_root, LocalJobs(repo_root, folder, summary=summary), key


def orphan_sandbox_vms(vms, state_text) -> list:
    """The sandbox's VMs (its pool, or named `sandbox-`) that its OpenTofu state does not hold:
    left by an interrupted build, no firewall or record follows them (consistency.orphan_rows)."""
    from worms_cli import consistency

    managed = consistency.state_vms(state_text)
    return [vm for vm in consistency.orphans(vms, managed) if vm.get("pool") == "sandbox" or vm["name"].startswith("sandbox-")]


def _destroy_orphans(jobs, yes, summary, ask_each=None, state_path=None) -> list:
    """Each orphan of the sandbox (orphan_sandbox_vms), destroyed when the operator says so, one by
    one, by name: stopped, then `qm destroy --purge`. Returns the names destroyed."""
    from worms_cli.session import HOST_WORKSPACE

    ask_each = ask_each or (lambda question: ask(question, False, yes))
    try:
        state = Path(state_path or Path(HOST_WORKSPACE) / "opentofu" / "stacks" / "sandbox" / "terraform.tfstate").read_text()
    except OSError:
        state = ""
    destroyed = []
    for vm in orphan_sandbox_vms(jobs.handlers.snapshots.pool_vms(jobs.executor, "sandbox"), state):
        says(f"qm stop {vm['vmid']}; qm destroy {vm['vmid']} --purge")
        if not ask_each(f"{vm['name']} (vmid {vm['vmid']}) is in no OpenTofu state (an interrupted build's): destroy it?"):
            print(f"  kept: {vm['name']}")
            continue
        jobs.executor.run(["qm", "stop", str(vm["vmid"])], check=False, timeout=300)
        jobs.executor.run(["qm", "destroy", str(vm["vmid"]), "--purge"], timeout=600)
        summary.note(f"✓ {vm['name']} (vmid {vm['vmid']}), in no OpenTofu state, destroyed")
        destroyed.append(vm["name"])
    return destroyed


def teardown(config, yes=False) -> int:
    """`worms round teardown`: the round's sandbox destroyed: one a session kept (powered off, for
    another attempt), one whose build stopped half-way, or what OpenTofu still holds of one whose
    VMs were deleted by hand."""
    from core.models.rounds import RoundRun
    from django.conf import settings

    from orchestrator.rounds.conductor import ACTIVE, Conductor
    from orchestrator.rounds.power import ProxmoxPower
    from worms_cli import power
    from worms_cli.summary import Summary

    if os.geteuid() != 0:
        raise BuildError("on the host itself, as root")
    sandbox = sorted(vm["name"] for vm in power.Power(config).runner.vms() if vm.get("pool") == "sandbox")
    folder = directory() / f"teardown-{datetime.now():%Y%m%d-%H%M%S}"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    summary = Summary(folder, "the round's sandbox destroyed", "worms round teardown")
    print(output.color(f"its steps: {folder}; what failed and why: {summary.path}", "dim"))
    _end_on_signals()
    _sync_workspace(config)  # the sandbox stack it destroys, at this checkout's commit
    _repo_root, jobs, key = _host_steps(config, lambda run=None: folder, summary)
    try:
        settings.PROXMOX["node"] = config.proxmox_node()
        conductor = Conductor(jobs=jobs, power=ProxmoxPower(api=Pvesh()))
        # An attempt a session left under way (a SIGKILL: nothing closed it) holds up every dev
        # upload (devbox_runs.deploy_waits_for) until it is closed: here, or by the next session.
        for run in RoundRun.objects.filter(state__in=ACTIVE).select_related("team", "round"):
            print(f"{run.team.name}'s attempt {run.attempt} in round {run.round.name!r} was left {run.state} by a "
                  "session that ended.")  # fmt: skip
            if ask("Close it (failed, not counted against the team; its VMs stopped)?", True, yes):
                conductor.abandon(run)
                summary.note(f"✓ {run.team.name}'s attempt {run.attempt} closed (left {run.state})")
        # No round runs after this: scoring and logging name no team (a session that ended
        # without its teardown left one there).
        try:
            jobs.handlers._clear_current_team(jobs.executor, jobs.cfg)
        except Exception as exc:  # noqa: BLE001  (said; `worms status` names it)
            output.warn(f"scoring's and logging's current team not cleared: {exc}")
        record = jobs.handlers.unfinished_build(jobs.cfg)
        try:
            held = bool(jobs.executor.tofu_output("sandbox"))
        except Exception:  # noqa: BLE001  (unreadable: destroying is the safe side)
            held = True
        if not (sandbox or held or record):
            print("No sandbox: nothing to destroy.")
            # What a sandbox destroyed earlier may have left (before 2026-11-26 every destroy did):
            # its definition, which an apply there would build again; the runs' secrets.
            jobs.handlers._forget_sandbox(jobs.executor, jobs.cfg)
            print(output.status_cell("ok", "its definition empty, no run's secrets, no reset record"))
            _destroy_orphans(jobs, yes, summary)
            return 0
        print(f"the round's sandbox: {', '.join(sandbox) or 'no VM, but OpenTofu still holds it'}"
              + (f" (an unfinished build, begun {record['began']})" if record else ""))  # fmt: skip
        print("  Destroyed, the next `worms round start` builds it again (about an hour) instead of rolling it back.")
        if not ask(f"Destroy it{f' ({len(sandbox)} VMs)' if sandbox else ''}?", False, yes):
            return 1
        conductor.destroy_sandbox()
        _destroy_orphans(jobs, yes, summary)
    except BuildError:
        summary.end("stopped by an error")
        raise
    except Exception as exc:  # noqa: BLE001  (its step's file says more)
        summary.end("not destroyed")
        raise BuildError(f"not destroyed: {exc} (what failed and why: {summary.path})") from None
    finally:
        if summary.status == "running":
            summary.end("nothing destroyed")
        key.stop()
    summary.end("destroyed")
    print(output.status_cell("ok", f"destroyed (its steps: {folder}; {summary.path})"))
    return 0


def _sync_workspace(config, runner=None, check=None, record=None) -> None:
    """The host's workspace (playbooks, gameplans, the sandbox stack: /opt/worms) at this checkout's
    commit, before anything is stopped: synced here when it is not (`SetupRoundAgent.yml --tags
    agent-sync`, which leaves the generated definitions and OpenTofu's state alone), then the
    host's gameplans checked, and the workspace recorded in deployed.json. A round runs committed
    code only: a checkout with changes to tracked files is refused. Until 2026-11-26 the session
    asked whether to run on mixed code, and `worms apply round-agent gameplans` was yours to
    remember."""
    from worms_cli import changes

    runner = runner or build_runner(config)
    here = runner.commit()
    if here.endswith("-dirty"):
        raise BuildError(f"this checkout has uncommitted changes ({here}): commit or stash them first; a round runs committed "
                         "code only")  # fmt: skip
    marker = Path(HOST_WORKSPACE) / ".worms-commit"
    there = marker.read_text().strip() if marker.exists() else "(unknown)"
    if there == here:
        print(output.status_cell("ok", f"the host's workspace runs this checkout's code ({here})"))
        return
    says("worms apply gameplans")  # what does the same
    runner.ansible("SetupRoundAgent.yml", "--tags", "agent-sync")
    (check or check_gameplans)(config)
    try:
        (record or changes.record_deployed)(runner.session, ["workspace"], here)
    except Exception as exc:  # noqa: BLE001  (the record is no reason to stop)
        output.warn(f"the record of what runs did not get the workspace: {exc}")
    print(output.status_cell("ok", f"workspace synced: {there} → {here}"))


def build_runner(config):
    from worms_cli import build

    return build.Runner(config)


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


def _attempt_folders(session: Path) -> list[Path]:
    """A session's attempts' folders (NN-<team>-attemptK), not its VMs' Ansible logs (round/)."""
    return sorted(p for p in session.iterdir() if p.is_dir() and re.fullmatch(r"\d\d-.+-attempt\d+", p.name))


def _failures_in(session: Path) -> str:
    """How many failures a session's summary.log holds ("—" without one: a session from before it)."""
    try:
        with open(session / "summary.log") as handle:
            for _ in range(20):
                found = re.match(r"FAILURES: (\d+|none)", handle.readline())
                if found:
                    return "0" if found.group(1) == "none" else found.group(1)
    except OSError:
        pass
    return "—"


def show_logs(round_word=None, team=None, lines=100, follow=False) -> int:
    """`worms log round [<r>] [<team>|summary]`; `worms log round summary`: the newest session's."""
    if round_word == "summary" and team is None:
        latest = directory() / "latest"  # the newest session's, a teardown's too (worms_cli/summary.py)
        found = [latest.resolve()] if latest.is_symlink() and latest.resolve().is_dir() else sessions()
        if not found:
            raise BuildError(f"no session here ({directory()})")
        return _show_summary(found[0])
    if round_word is None:
        found = sessions()
        if not found:
            print(f"No round has run here yet ({directory()}).")
            return 0
        rows = []
        for path in found[:30]:
            round_id, stamp = path.name.split("-", 1)
            rows.append([round_id, datetime.strptime(stamp, "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M"),
                         str(len(_attempt_folders(path))), _failures_in(path), str(path)])  # fmt: skip
        print(output.table(rows, headers=["Round", "Session (local time)", "Attempts", "Failures", "Folder"]))
        print(output.color("one: `worms log round <round> [<team>|summary]`; the newest session's summary: "
                           f"`worms log round summary` ({directory() / 'latest' / 'summary.log'})", "dim"))  # fmt: skip
        return 0
    found = sessions(round_word if round_word.isdigit() else None)
    if not found:
        raise BuildError(f"no session of round {round_word} here ({directory()}); `worms log round` lists them")
    session = found[0]
    if team == "summary":
        return _show_summary(session)
    if team is None:
        target = [str(session / "round.log")]
        print(output.color(f"what failed and why: {session / 'summary.log'} (`worms log round {round_word} summary`)", "dim"))
    else:
        attempts = [p for p in _attempt_folders(session) if p.name.split("-", 1)[1].lower().startswith(f"{team.lower()}-")]
        if not attempts:
            raise BuildError(f"no attempt of {team!r} in {session}")
        target = sorted(str(f) for f in attempts[-1].glob("*.log"))
        print(output.color(f"{attempts[-1]}: {', '.join(Path(t).name for t in target)}", "dim"))
    argv = ["tail", "-n", str(int(lines)), *(["-F"] if follow else []), *target]
    return subprocess.call(argv)


def _show_summary(session: Path) -> int:
    """A session's summary.log, whole: its failures come first."""
    path = session / "summary.log"
    if not path.exists():
        raise BuildError(f"{session} has no summary.log (a session from before 2026-11-24): its round.log, "
                         "and each attempt's step files, say what happened")  # fmt: skip
    print(output.color(str(path), "dim"))
    sys.stdout.write(path.read_text(errors="replace"))
    return 0


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
