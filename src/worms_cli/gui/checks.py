"""worms-gui's devbox checks: `worms check <team> --json` (worms_cli/validate.py), the CLI's own
read-only end-to-end check of a team's devbox, run in the background for the Teams tab.

worms-gui never logs in to a team VM itself. What reaches a team's VMs is what `worms check` does
(it asks each sshd whether it would take the team's public keys, and runs its read-only checks
there as root, from the host); what comes back is its JSON's `checks` field: verdicts, and reasons
that may be what a VM printed (escaped by the CLI, shown escaped again and labelled as the VM's).
No flag or secret is in it: only whether /etc/worms/flag is there and well-formed.

    checks = Checks(store, CliCheck(env, teams))   env: the CLI's (actions.Runner's); teams() how many
                                                   teams there are (an every-team run's timeout)
    checks.start()
    checks.want(slugs, auto=False) -> str   a check of these teams, queued ([] = every team, in one
                                            run): "" or why not (queued or running already). auto: the
                                            cursor's (ui.py), not asked for: at most one such waits, the
                                            newest (the cursor moved on), and its end goes to the Log tab
                                            only; one asked for (c, C) is said in the status line
    checks.stop()                           on quit: the one under way ends (Ctrl-C, SIGTERM, SIGKILL)

One at a time (one takes about 50 s and some 40 s of the host's CPU; every team's in one run longer),
the rest queued (every team's drops the teams' own waiting ahead of it); the results kept per team in
the store's `checks` (state.py). With --dry-run they run too: they change nothing. --demo gives Checks
a runner of its own (app.py) that makes results up.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Callable

FRESH = 300.0  # s: a team's result older than this is checked again when the cursor rests on one of its VMs (ui.py)
TIMEOUT = 600.0  # s for one team's `worms check`; every team's: timeout() (more teams, longer)
STATES = ("ok", "warn", "error", "stopped", "unknown", "n/a")
SLUG = re.compile(r"t\d+")
CHILDREN: set = set()  # the `worms check` processes (each in a session of its own): ended at exit


@atexit.register
def _end_children() -> None:
    for proc in list(CHILDREN):
        _end(proc)


def _end(proc) -> None:
    """Ctrl-C to its process group (the CLI then stops its ssh-agent), SIGTERM, SIGKILL: each after 2 s."""
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signum)
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return


def timeout(teams: int) -> float:
    """How long a `worms check` of this many teams may take: the CLI runs its checks on 12 workers, four
    VMs a team and the host's three, each up to 300 s (validate.py), so a batch of 12 at most 300 s; and
    the team's jobs and uploads after them."""
    return max(TIMEOUT, 300.0 * math.ceil((4 * max(1, teams) + 3) / 12) + 300.0)


def _text(value, limit: int) -> str:
    return "" if value is None else str(value)[:limit]


def _value(value):
    """A cell's other fields as plain values: a flag, a number, a list of names, a text."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_text(item, 80) for item in value[:64]]
    return _text(value, 600)


def cell(value) -> dict:
    """One cell of the JSON, in the shape the UI reads, whatever came."""
    if not isinstance(value, dict):
        return {"state": "unknown", "text": "?", "reason": "", "from_vm": False}
    out = {"state": value.get("state") if value.get("state") in STATES else "unknown", "text": _text(value.get("text"), 40),
           "reason": _text(value.get("reason"), 600), "from_vm": bool(value.get("from_vm"))}  # fmt: skip
    for key, item in value.items():
        if key not in out and isinstance(key, str):
            out[key] = _value(item)
    return out


def _lines(value, keys) -> list[dict]:
    return [{key: _value(item.get(key)) for key in keys} for item in (value if isinstance(value, list) else [])[:40]
            if isinstance(item, dict)]  # fmt: skip


def team(value) -> dict | None:
    """One team of the JSON's `checks.teams`, in the shape the UI reads; None without a slug."""
    if not isinstance(value, dict) or not SLUG.fullmatch(str(value.get("slug", ""))):
        return None
    keys = value.get("keys") if isinstance(value.get("keys"), dict) else {}
    vms = value.get("vms") if isinstance(value.get("vms"), dict) else {}
    checks = value.get("checks") if isinstance(value.get("checks"), dict) else {}
    return {
        "slug": str(value["slug"]), "name": _text(value.get("name"), 80), "label": _text(value.get("label"), 90),
        "keys": {"registered": _value(keys.get("registered")), "invalid": _value(keys.get("invalid")),
                 "names": _value(keys.get("names") if isinstance(keys.get("names"), list) else [])},
        "vms": {str(role): ({key: _value(vm.get(key)) for key in ("name", "vmid", "status")} if isinstance(vm, dict) else None)
                for role, vm in vms.items()},
        "checks": {str(check): {str(role): cell(c) for role, c in roles.items()} for check, roles in checks.items()
                   if isinstance(roles, dict)},
        "notes": _lines(value.get("notes"), ("state", "text")),
        "activity": _lines(value.get("activity"), ("kind", "id", "state", "text")),
    }  # fmt: skip


class CliCheck:
    """The live runner: `worms check <slugs> --json` (this checkout's CLI, this interpreter); (its
    `checks`, "") or (None, why it gave none)."""

    def __init__(self, env: dict | None = None, teams: Callable | None = None, argv: Callable | None = None):
        from worms_cli.gui import actions

        self.env = {**(env if env is not None else os.environ), "NO_COLOR": "1"}
        self.teams = teams or (lambda: 0)  # how many teams there are: an every-team run's timeout
        self.argv = argv or (lambda slugs: actions.worms("check", *slugs, "--json"))
        self.proc = None
        self.stopping = False

    def command(self, slugs) -> str:
        return "worms check " + " ".join([*slugs, "--json"])

    def __call__(self, slugs: list[str]) -> tuple[dict | None, str]:
        try:
            count = len(slugs) or int(self.teams() or 0)
        except Exception:  # noqa: BLE001  (the team map not known: a dozen teams' time)
            count = 12
        limit = timeout(count)
        proc = subprocess.Popen(self.argv(slugs), env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)  # fmt: skip
        self.proc = proc
        CHILDREN.add(proc)
        try:
            if self.stopping:
                _end(proc)
            try:
                out, err = proc.communicate(timeout=limit)
            except subprocess.TimeoutExpired:
                _end(proc)
                try:
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:  # a child that keeps the pipes: left as it is
                    return None, f"no answer in {limit:.0f} s"
                return None, f"no answer in {limit:.0f} s: ended"
        finally:
            CHILDREN.discard(proc)
            self.proc = None
        try:
            doc = json.loads(out)
        except ValueError:
            doc = None
        found = doc.get("checks") if isinstance(doc, dict) else None
        if isinstance(found, dict):
            return found, ""
        said = [line.strip() for line in err.splitlines() if line.strip() and not line.startswith(("internet access:", "["))]
        why = said[-1] if said else f"exit {proc.returncode}, no answer"
        return None, why[:300]

    def stop(self) -> None:
        self.stopping = True
        proc = self.proc
        if proc is not None:
            _end(proc)


class Checks:
    def __init__(self, store, run: Callable):
        """`run(slugs) -> (the JSON's checks | None, why not)`: CliCheck, or --demo's."""
        self.store, self.run = store, run
        self.lock = threading.Lock()
        self.queue: deque = deque()  # (slugs, auto): slugs a tuple, () = every team; auto: the cursor's, not asked for
        self.running = None  # (slugs, started, auto)
        self.teams: dict[str, dict] = {}
        self.errors: dict[str, dict] = {}  # slug ("*": every team) -> {at, text}
        self.ssh_gateway = None
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="worms-gui checks", daemon=True)

    def start(self) -> None:
        self._publish()
        self.thread.start()

    def want(self, slugs, auto: bool = False) -> str:
        slugs = tuple(dict.fromkeys(str(s) for s in slugs))
        with self.lock:
            if auto:  # the cursor moved on: the one it asked for before need not wait any more
                self.queue = deque(entry for entry in self.queue if not entry[1])
            elif not slugs:  # every team's: it checks those waiting too
                self.queue = deque(entry for entry in self.queue if not entry[0])
            busy = ([self.running[0]] if self.running else []) + [entry[0] for entry in self.queue]
            if () in busy:
                return "every team's check is queued or running already"
            left = tuple(s for s in slugs if not any(s in entry for entry in busy))
            if slugs and not left:
                return f"{', '.join(slugs)}: queued or running already"
            self.queue.append((left, auto))
        self.wake.set()
        self._publish()
        return ""

    def _loop(self) -> None:
        while not self.stopping.is_set():
            with self.lock:
                entry, auto = self.queue.popleft() if self.queue else (None, False)
                self.running = (entry, time.time(), auto) if entry is not None else None
            if entry is None:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            self._publish()
            started = time.time()
            try:
                found, why = self.run(list(entry))
            except Exception as exc:  # noqa: BLE001  (said for those teams; the next one runs)
                found, why = None, f"{type(exc).__name__}: {exc}"
            if self.stopping.is_set():
                break
            try:
                self._took(entry, found, why, started, time.time())
                self._said(entry, found, why, auto)
            except Exception:  # noqa: BLE001  (an answer it cannot read: the next check runs all the same)
                pass
            with self.lock:
                self.running = None
            self._publish()

    def remember(self, found: dict, at: float, took: float = 50.0) -> None:
        """Results that came another way (--demo's, made up), kept as those of a check that ended at `at`."""
        slugs = tuple(str(t.get("slug")) for t in found.get("teams") or [] if isinstance(t, dict))
        self._took(slugs, found, "", at - took, at)
        self._publish()

    def _said(self, entry, found, why, auto) -> None:
        """Its end: in the status line when it was asked for (c, C), else in the Log tab only."""
        names = ", ".join(entry) if entry else "every team"
        if found is None:
            text, level = f"devbox check of {names}: no answer: {why or '?'}", "warn"
        else:
            labels = [_text(t.get("label") or t.get("slug"), 40) for t in found.get("teams") or [] if isinstance(t, dict)]
            problems = found.get("problems") if isinstance(found.get("problems"), int) else None
            verdict = ("every check passed" if problems == 0 else f"{problems} problem(s): the Teams tab says which"
                       if problems else "done")  # fmt: skip
            text, level = f"devbox check of {', '.join(labels) or names}: {verdict}", "ok" if problems == 0 else "warn"
        say = getattr(self.store, "log" if auto else "say", None)
        if not callable(say):  # --demo's store: its `log` is the lines
            say = getattr(self.store, "note", None)
        if callable(say):
            say(" ".join(text.split()), level)  # one line: a team's name, a CLI's message

    def _took(self, entry, found, why, started, now) -> None:
        command = self.run.command(entry) if hasattr(self.run, "command") else ""
        with self.lock:
            if found is None:
                for slug in entry or ("*",):
                    self.errors[slug] = {"at": now, "started": started, "text": why or "no answer"}
                return
            answered = set()
            for value in found.get("teams") or []:
                entry_ = team(value)
                if entry_ is None:
                    continue
                self.teams[entry_["slug"]] = {**entry_, "at": now, "took": now - started, "command": command}
                self.errors.pop(entry_["slug"], None)
                answered.add(entry_["slug"])
            if not entry:
                self.errors.pop("*", None)
            for slug in set(entry) - answered:
                self.errors[slug] = {"at": now, "started": started, "text": "not in its answer (no devbox in the current competition?)"}
            gateway = found.get("ssh_gateway")
            if isinstance(gateway, dict):
                self.ssh_gateway = {"name": _text(gateway.get("name"), 60), "status": _text(gateway.get("status"), 20),
                                    "lines": _lines(gateway.get("lines"), ("state", "text")), "at": now}  # fmt: skip

    def _publish(self) -> None:
        with self.lock:
            value = {"teams": {slug: dict(entry) for slug, entry in self.teams.items()}, "ssh_gateway": self.ssh_gateway,
                     "errors": {slug: dict(error) for slug, error in self.errors.items()},
                     "running": {"slugs": list(self.running[0]), "started": self.running[1]} if self.running else None,
                     "queue": [list(slugs) for slugs, _auto in self.queue]}  # fmt: skip
        self.store.set("checks", value)

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()
        stop = getattr(self.run, "stop", None)
        if stop is not None:
            stop()
