"""Run logs: every `worms` run that changes something (apply, destroy, internet, vm, jobs, round,
service restart, db restore|reset, key, step) keeps its whole output, the tools' too, in a file of
its own:

    ~/.worms/runs/<YYYYmmdd-HHMMSS>-<command>.log      `worms log run [N]`: the newest, or N-th
    ~/.worms/runs/<YYYYmmdd-HHMMSS>-<command>/<vm>.log  each VM's own Ansible log (`worms log run [N] <vm>`)

The directory is 0700 and each file 0600: Ansible's output can hold secrets. The newest KEEP files
are kept. The terminal sees what it always did; the file gets it without colours.

It works on the file descriptors, not on Python's streams: OpenTofu, Ansible and ssh write to
stdout and stderr themselves. Each of the two goes through a pipe to a thread that writes it to
where it went before and to the file. Not in the round agent's workspace (/opt/worms): the agent
keeps a log of every job itself (/var/log/worms/jobs).

With the CLI's configuration it also shows a long Ansible task's live output under it (live.py):
WORMS_LIVE=1 asks Ansible's callback for it; its lines are taken out of the output, and the window
is drawn on the terminal only, never into the file.
"""

from __future__ import annotations

import os
import re
import shutil
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


def vm_logs(log: Path) -> Path:
    """The directory of a run's VM logs (one Ansible log per VM): the run log's name, without .log."""
    return log.with_suffix("")


def nth(n: int = 1) -> Path:
    """The n-th newest run log (1: the newest); LookupError if there are fewer."""
    found = logs()
    if n < 1 or n > len(found):
        raise LookupError(f"there are {len(found)} run log(s) in {directory()}" + (f", not {n}" if found else ""))
    return found[n - 1]


def name(command: str, args) -> str:
    """What a run is called in its file's name: the command and what it acts on first: `apply`,
    `vm-reset-alpha`, `internet-off`, `step-offline`, `vm-rebuild-devboxes`."""
    words = command.split()
    for attribute in ("targets", "state", "name", "round", "team", "numbers"):
        value = getattr(args, attribute, None)
        if isinstance(value, list):
            value = value[0] if value else None
        if value not in (None, "") and command != "apply":
            words.append(str(value))
            break
    return re.sub(r"[^a-z0-9.-]+", "-", "-".join(words).lower()).strip("-") or "run"


class Tee:
    """stdout and stderr, from now until stop(), also into a run log."""

    def __init__(self, name: str, config=None):
        self.path = directory() / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.log"
        self._threads = []
        self._saved = {}
        self._lock = threading.Lock()
        self._file = None
        self._config = config
        self._live = None

    def start(self) -> "Tee":
        folder = directory()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        for old in logs()[KEEP - 1:]:
            old.unlink(missing_ok=True)
            shutil.rmtree(vm_logs(old), ignore_errors=True)
        path, n = self.path, 1
        while path.exists():  # two runs in one second
            n += 1
            path = self.path.with_name(f"{self.path.stem}-{n}.log")
        self.path = path
        # Each VM's own Ansible log, in a directory next to it (ansible/callback_plugins/worms.py).
        os.environ["WORMS_ANSIBLE_LOGS"] = str(vm_logs(path))
        # A file that cannot be opened stops nothing: the caller runs the command without a log.
        self._file = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            for fd in (1, 2):
                self._saved[fd] = os.dup(fd)
                read, write = os.pipe()
                os.dup2(write, fd)
                os.close(write)
                thread = threading.Thread(target=self._pump, args=(read, self._saved[fd], fd == 1), daemon=True)
                thread.start()
                self._threads.append(thread)
        except OSError:  # out of descriptors, say: back as it was, and no log
            self.stop()
            raise
        if os.isatty(self._saved[1]) and "ANSIBLE_FORCE_COLOR" not in os.environ:
            os.environ["ANSIBLE_FORCE_COLOR"] = "1"  # Ansible sees a pipe now; the terminal still gets colours
        if self._config is not None:
            try:
                from worms_cli.live import Live

                self._live = Live(self._config, self._saved[1], self._emit)
                os.environ["WORMS_LIVE"] = "1"  # ansible/callback_plugins/worms_live.py
            except Exception:  # noqa: BLE001  (no live output is no reason to stop)
                self._live = None
        return self

    def _emit(self, data: bytes) -> None:
        """A line of our own (live.py's heartbeat) to the terminal and the file."""
        try:
            _write_all(self._saved[1], data)
        except (OSError, KeyError):
            pass
        with self._lock:
            if self._file is not None:
                try:
                    _write_all(self._file, ANSI.sub(b"", data))
                except OSError:
                    pass

    def _pump(self, read: int, terminal: int, stdout: bool) -> None:
        """The terminal first and always; the file while it can be written. A log that fails (a
        full disk) is closed, said once, and the command goes on: the pipe never fills up. With a
        live window (live.py) the terminal gets the output around it, and stdout loses the
        callback's lines."""
        while chunk := os.read(read, 65536):
            live = self._live
            try:
                if live is not None:
                    chunk = live.output(terminal, chunk, stdout)
                else:
                    _write_all(terminal, chunk)
            except OSError:  # the terminal is gone: the file still gets it
                pass
            except Exception:  # noqa: BLE001  (a broken window: the output goes on without it)
                self._live = None
                try:
                    _write_all(terminal, chunk)
                except OSError:
                    pass
            with self._lock:
                if self._file is None:
                    continue
                try:
                    _write_all(self._file, ANSI.sub(b"", chunk))
                except OSError as exc:
                    os.close(self._file)
                    self._file = None
                    try:
                        _write_all(self._saved.get(2, terminal), f"\nwarning: the run log stopped ({exc}): {self.path} is "
                                                                   "incomplete; the command goes on\n".encode())  # fmt: skip
                    except OSError:
                        pass
        os.close(read)

    def stop(self) -> None:
        """Back to the terminal alone, once what is in the pipes is written."""
        if self._live is not None:
            try:
                self._live.close()  # its window away, its SSH ended; it still takes markers out
            except Exception:  # noqa: BLE001
                pass
            os.environ.pop("WORMS_LIVE", None)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except OSError:
            pass
        for fd, saved in self._saved.items():
            os.dup2(saved, fd)  # the pipe's last writer is gone: its thread reads to its end
        for thread in self._threads:
            thread.join(timeout=5)  # a child that outlives the run may hold a pipe open
        if not any(thread.is_alive() for thread in self._threads):
            for saved in self._saved.values():  # else a pump still writes to them: left open
                os.close(saved)
            self._saved.clear()
        with self._lock:
            if self._file is not None:
                os.close(self._file)
                self._file = None


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]
