"""A round session's summary.log: the one file that says what failed, why, and in which file the
rest is, and where each step's log is (worms_cli/rounds.py writes it as the session goes).

    ~/.worms/rounds/<round>-<YYYYmmdd-HHMMSS>/summary.log    the session's
    ~/.worms/rounds/latest -> <round>-<YYYYmmdd-HHMMSS>      the newest session's folder, so
    ~/.worms/rounds/latest/summary.log                       is always the newest summary

`worms log round [<round>] summary` shows it. It is written whole after every step (it is small,
and a reader never sees half of it): first the failures, the newest first, each with what failed
(the team, its attempt, the step and its phase), why (scripts/round_agent/failure.py: Ansible's
failed task with its VM, file:line and msg; OpenTofu's Error:; where an exception was raised),
the step's log and each failed VM's own Ansible log, and what to do next; then every step in
order, one line each: how it ended, how long it took, its log.
"""

from __future__ import annotations

import os
import socket
import threading
from datetime import datetime
from pathlib import Path

NAME = "summary.log"
LATEST = "latest"
RULE = "═" * 100


def _took(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def point_latest(folder: Path) -> None:
    """~/.worms/rounds/latest: the newest session's folder (replaced in one step)."""
    link = folder.parent / LATEST
    try:
        tmp = folder.parent / f".{LATEST}.tmp"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(folder.name)
        tmp.replace(link)
    except OSError:
        pass  # the summary is in the session's folder all the same


class Summary:
    def __init__(self, folder: Path, title: str, command: str):
        self.folder = Path(folder)
        self.path = self.folder / NAME
        self.title, self.command = title, command
        self.began = datetime.now()
        self.status = "running"
        self.steps: list[str] = []
        self.failures: list[dict] = []
        self._lock = threading.Lock()
        point_latest(self.folder)
        self.write()

    # -- what happened ----------------------------------------------------------------------

    def step(self, kind: str, job_id, who: str, ok: bool, took: float, log: str, note: str = "") -> None:
        """A step's end: one line."""
        mark = "✓" if ok else "✗"
        line = f"{datetime.now():%H:%M:%S}  {mark} {kind:<12} #{job_id!s:<6} {who:<34} {_took(took):>7}  {log or '(no log file)'}"
        with self._lock:
            self.steps.append(line + (f"  {note}" if note else ""))
        self.write()

    def note(self, text: str) -> None:
        """A line of the session's own among the steps (an attempt's end, the round stopping)."""
        with self._lock:
            self.steps.append(f"{datetime.now():%H:%M:%S}  {text}")
        self.write()

    def failure(self, what: str, headline: str, detail: str = "", log: str = "", logs=(), next_step: str = "",
                refers: int | None = None, listed: bool = True) -> int:
        """A failure: what failed, why (headline: one line; detail: the excerpts that say it), the
        file with the rest, each failed VM's own log, what to do. `refers`: the number of a failure
        recorded already that is its cause (an attempt failed by a step): named, not repeated.
        `listed`: a line among the steps too (a step's own line names it instead). Returns its number."""
        with self._lock:
            number = len(self.failures) + 1
            self.failures.append({"n": number, "at": datetime.now(), "what": what, "headline": headline,
                                  "detail": detail, "log": log, "logs": list(logs or ()), "next": next_step,
                                  "refers": refers})  # fmt: skip
            if listed:
                self.steps.append(f"{datetime.now():%H:%M:%S}  ✗ {what}: see failure [{number}]")
        self.write()
        return number

    def end(self, status: str) -> None:
        self.status = f"ended {datetime.now():%Y-%m-%d %H:%M:%S}: {status}"
        self.write()

    # -- the file ---------------------------------------------------------------------------

    def render(self) -> str:
        out = [
            f"{self.title}",
            f"  {self.command}  ·  on {socket.gethostname().split('.')[0]}  ·  began {self.began:%Y-%m-%d %H:%M:%S} (local time)",
            f"  status: {self.status}",
            "",
            f"  this session's folder: {self.folder}",
            f"    {NAME:<21} this file (always the newest: {self.folder.parent / LATEST / NAME})",
            "    round.log             everything the terminal showed",
            "    NN-<team>-attemptK/   each attempt's steps: NNN-<step>.log, one file each",
            "    round/<vm>.log        each VM's own Ansible log (every task it ran, a failure in full)",
            "",
            RULE,
        ]
        failures = list(self.failures)
        if not failures:
            out.append("FAILURES: none" + (" so far" if self.status == "running" else ""))
        else:
            out.append(f"FAILURES: {len(failures)}, the newest first")
        out.append(RULE)
        for entry in reversed(failures):
            out.append(f"[{entry['n']}] {entry['at']:%Y-%m-%d %H:%M:%S}  {entry['what']}")
            if entry["refers"]:
                out.append(f"    why:  failure [{entry['refers']}] (below)")
                out.append(f"    in short: {entry['headline']}")
            else:
                out.append(f"    why:  {entry['headline']}")
            if entry["log"]:
                out.append(f"    log:  {entry['log']}")
            logs = entry["logs"]
            if logs:
                out.append(f"    each failed VM's own Ansible log: {logs[0]}"
                           + (f" (and {len(logs) - 1} more there: {', '.join(Path(p).name for p in logs[1:6])})" if len(logs) > 1 else ""))  # fmt: skip
            if entry["detail"] and not entry["refers"]:
                out.append("    what it said:")
                out.extend("      " + line for line in entry["detail"].splitlines())
            if entry["next"]:
                out.append(f"    next: {entry['next']}")
            out.append("")
        out += [RULE, "STEPS (local time): how each ended, how long it took, its log", RULE]
        out += list(self.steps) or ["(none yet)"]
        return "\n".join(out) + "\n"

    def write(self) -> None:
        """The whole file, replaced in one step. A problem with it stops nothing."""
        try:
            with self._lock:
                text = self.render()
            tmp = self.path.with_name(f".{NAME}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
            tmp.replace(self.path)
        except OSError:
            pass
