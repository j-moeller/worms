"""Run logs: every `worms` run that changes something (build, deploy, reset, internet, keys, step,
restart, power, destroy) keeps its whole output, the tools' too, in a file of its own:

    ~/.worms/runs/<YYYYmmdd-HHMMSS>-<verb>.log      `worms log build [N]`: the newest, or N-th

The directory is 0700 and each file 0600: Ansible's output can hold secrets. The newest KEEP files
are kept. The terminal sees what it always did; the file gets it without colours.

It works on the file descriptors, not on Python's streams: OpenTofu, Ansible and ssh write to
stdout and stderr themselves. Each of the two goes through a pipe to a thread that writes it to
where it went before and to the file. Not in the round agent's workspace (/opt/worms): the agent
keeps a log of every job itself (/var/log/worms/jobs).
"""

from __future__ import annotations

import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

RUNS = "~/.worms/runs"
KEEP = 200
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
NAME = re.compile(r"\d{8}-\d{6}-[a-z0-9.-]+\.log")


def directory() -> Path:
    return Path(RUNS).expanduser()


def logs() -> list[Path]:
    """The run logs there are, newest first."""
    if not directory().is_dir():
        return []
    return sorted((p for p in directory().iterdir() if NAME.fullmatch(p.name)), key=lambda p: p.name, reverse=True)


def nth(n: int = 1) -> Path:
    """The n-th newest run log (1: the newest); LookupError if there are fewer."""
    found = logs()
    if n < 1 or n > len(found):
        raise LookupError(f"there are {len(found)} run log(s) in {directory()}" + (f", not {n}" if found else ""))
    return found[n - 1]


def verb(args) -> str:
    """What a run is called in its file's name: `build`, `reset-devbox`, `internet-disable`, `step-offline`."""
    words = [args.command]
    for name in ("what", "state", "name", "action"):
        value = getattr(args, name, None)
        if isinstance(value, str) and value and args.command in ("reset", "internet", "step", "power", "keys"):
            words.append(value)
            break
    return re.sub(r"[^a-z0-9.-]+", "-", "-".join(words).lower()).strip("-") or "run"


class Tee:
    """stdout and stderr, from now until stop(), also into a run log."""

    def __init__(self, name: str):
        self.path = directory() / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.log"
        self._threads = []
        self._saved = {}
        self._lock = threading.Lock()
        self._file = None

    def start(self) -> "Tee":
        folder = directory()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        for old in logs()[KEEP - 1:]:
            old.unlink(missing_ok=True)
        path, n = self.path, 1
        while path.exists():  # two runs in one second
            n += 1
            path = self.path.with_name(f"{self.path.stem}-{n}.log")
        self.path = path
        self._file = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        if sys.stdout.isatty() and "ANSIBLE_FORCE_COLOR" not in os.environ:
            os.environ["ANSIBLE_FORCE_COLOR"] = "1"  # Ansible sees a pipe now; the terminal still gets colours
        sys.stdout.flush()
        sys.stderr.flush()
        for fd in (1, 2):
            self._saved[fd] = os.dup(fd)
            read, write = os.pipe()
            os.dup2(write, fd)
            os.close(write)
            thread = threading.Thread(target=self._pump, args=(read, self._saved[fd]), daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def _pump(self, read: int, terminal: int) -> None:
        while chunk := os.read(read, 65536):
            try:
                os.write(terminal, chunk)
            except OSError:  # the terminal is gone: the file still gets it
                pass
            with self._lock:
                os.write(self._file, ANSI.sub(b"", chunk))
        os.close(read)

    def stop(self) -> None:
        """Back to the terminal alone, once what is in the pipes is written."""
        sys.stdout.flush()
        sys.stderr.flush()
        for fd, saved in self._saved.items():
            os.dup2(saved, fd)  # the pipe's last writer is gone: its thread reads to its end
        for thread in self._threads:
            thread.join(timeout=5)  # a child that outlives the run may hold a pipe open
        for saved in self._saved.values():
            os.close(saved)
        self._saved.clear()
        if self._file is not None:
            os.close(self._file)
            self._file = None
