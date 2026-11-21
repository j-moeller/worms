"""The live output of a running Ansible task, under it in the terminal, as docker shows a build's.

Ansible prints what a task did only once it is over. A long task writes its output to a file on its
host as it goes, and the callback ansible/callback_plugins/worms_live.py says so on stdout: a line
`\\x1eworms-live start <host> <task> <file>` when the task starts there, `... stop <host> <task>`
when it ends. The run log (runlog.Tee) hands every chunk of output to `Live.output`, which takes these
lines out (neither the terminal nor the file gets them) and, once a task has run DELAY seconds,
follows its file on its host over the management SSH (`tail -F`, as `worms run` reaches a VM):

    ┌ devpi: /var/log/worms/devpi-seed.log · 2m13s · last line 4s ago
    │ Collecting torch
    │ Downloading torch-2.9.1-cp313-cp313-manylinux_2_28_x86_64.whl (899.7 MB)
    ...

the last LINES lines, redrawn in place as they come, and once a second for the clock: "last line
4 min ago" is a task that waits on something. When the task ends the window goes. Ansible's own
output goes above it: the window is taken away, the output written, the window drawn again. It
is drawn only while the cursor is at the start of a line (never into a prompt), and its lines are
cut to the terminal's width, so it always knows its height.

Without a terminal (output into a file or a pipe) there is no window: a plain line every HEARTBEAT
seconds instead, into the run log too. What a host's file says is escaped: a team's VM is assumed
rooted.
"""

from __future__ import annotations

import collections
import os
import shlex
import subprocess
import threading
import time

MARK = b"\x1eworms-live "
LINES = 10
DELAY = 3.0  # a task over sooner shows nothing (and no SSH is started for it)
MAX_FOLLOWED = 4  # hosts followed at once in one window; more are counted
HEARTBEAT = 30.0
APT_LOG = "/var/log/apt/term.log"  # apt's own: its old lines are not this task's
DOMAIN = ".worms.internal"


def _clean(raw: bytes) -> str:
    """One line of a host's file, safe for the terminal: what a progress bar's \\r left, escaped."""
    from worms_cli.rsync import printable

    text = raw.decode(errors="replace").rstrip("\n").rsplit("\r", 1)[-1]
    return printable(text).replace("\t", " ")


def _age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


class Follow:
    """One task's file on one host."""

    def __init__(self, host: str, path: str):
        self.host, self.path = host, path
        self.name = host.removesuffix(DOMAIN)
        self.started = time.monotonic()
        self.last: float | None = None
        self.proc: subprocess.Popen | None = None
        self.problem = ""
        self.following = False


class Row:
    """One VM of the play that runs: what it does now (worms.py's progress lines)."""

    def __init__(self, host: str):
        self.host, self.role, self.task = host, "", "waiting"
        self.state = "running"
        self.since = time.monotonic()
        self.counts: dict[str, int] = {}


class Live:
    def __init__(self, config, terminal: int, emit):
        """`terminal`: the fd the window is drawn on; `emit(bytes)`: a plain line to the terminal and
        the run log (the heartbeat without a terminal)."""
        self.config = config
        self.terminal = terminal
        self.tty = os.isatty(terminal)
        self.emit = emit
        self.lock = threading.RLock()
        self.follows: dict[tuple[str, str], Follow] = {}
        self.lines: collections.deque[str] = collections.deque(maxlen=LINES)
        self.drawn = 0  # the window's lines on the screen now
        self.at_line_start = True  # the terminal's cursor, after what was written to it
        self.stream_line_start = True  # stdout's text, for the markers
        self.pending = b""
        self.remote = None
        self.beat = time.monotonic()
        self.closed = False
        self.play: str | None = None
        self.play_started = time.monotonic()
        self.rows: dict[str, Row] = {}
        self.ticker = threading.Thread(target=self._tick, daemon=True)
        self.ticker.start()

    # -- the output -------------------------------------------------------------------------------

    def output(self, fd: int, chunk: bytes, stdout: bool) -> bytes:
        """Write `chunk` (from stdout, or not) to `fd` around the window; what the run log gets back."""
        if stdout:
            chunk = self._markers(chunk)
        if chunk:
            with self.lock:
                self._erase()
                _write_all(fd, chunk)
                self.at_line_start = chunk.endswith(b"\n")
                self._draw()
        return chunk

    def _markers(self, chunk: bytes) -> bytes:
        """`chunk` without the callback's lines, each of which is acted on."""
        data, self.pending = self.pending + chunk, b""
        out = bytearray()
        while data:
            if self.stream_line_start and data[:1] == b"\x1e":
                end = data.find(b"\n")
                if end < 0:
                    if len(data) > 4096:  # not ours after all
                        out += data
                        self.stream_line_start = False
                    else:
                        self.pending = data
                    break
                line, data = data[:end], data[end + 1:]
                if line.startswith(MARK):
                    self._event(line[len(MARK):].decode(errors="replace"))
                else:
                    out += line + b"\n"
                continue
            end = data.find(b"\n")
            if end < 0:
                out += data
                self.stream_line_start = False
                break
            out += data[:end + 1]
            data = data[end + 1:]
            self.stream_line_start = True
        return bytes(out)

    def _event(self, text: str) -> None:
        words = text.split()
        with self.lock:
            if words[:1] == ["progress"]:
                self._progress(text[len("progress "):])
            elif len(words) == 4 and words[0] == "start":
                _, host, task, path = words
                self.follows.setdefault((host, task), Follow(host, path))
            elif len(words) == 3 and words[0] == "stop":
                follow = self.follows.pop((words[1], words[2]), None)
                if follow is not None:
                    self._stop(follow)
                if not self.follows:
                    self.lines.clear()
                    self._erase()

    # -- every VM of a play, at once (ansible/callback_plugins/worms.py) --------------------------

    def _progress(self, payload: str) -> None:
        import json

        try:
            event = json.loads(payload)
        except ValueError:
            return
        kind, host = event.get("event"), event.get("host", "")
        if kind == "play":
            self.play = event.get("name", "")
            self.play_started = time.monotonic()
            self.rows = {h: Row(h) for h in event.get("hosts", [])}
        elif kind == "end":
            self.play, self.rows = None, {}
            self._erase()
        elif host:
            row = self.rows.setdefault(host, Row(host))
            if kind == "task":
                row.role, row.task, row.since = event.get("role", ""), event.get("task", ""), time.monotonic()
                row.state = row.state if row.state == "failed" else "running"
            elif kind == "result":
                status = event.get("status")
                row.counts[status] = row.counts.get(status, 0) + 1
                if status in ("failed", "unreachable"):
                    row.state = "failed"
            elif kind == "done":
                row.state = row.state if row.state == "failed" else "done"

    def _progress_rows(self, width: int) -> list[str]:
        """The play's VMs: one line each while it runs or after it failed, the finished ones on one."""
        if not self.rows or len(self.rows) < 2:
            return []
        now = time.monotonic()
        done = [r.host for r in self.rows.values() if r.state == "done"]
        busy = [r for r in self.rows.values() if r.state in ("running", "failed")]
        name = max((len(r.host) for r in busy), default=0)
        lines = [f"{self.play} · {len(done)}/{len(self.rows)} VMs done · {_age(now - self.play_started)}"]
        for row in sorted(busy, key=lambda r: (r.state != "failed", r.host)):
            mark = "✗" if row.state == "failed" else "▸"
            where = f"{row.role} : {row.task}" if row.role else row.task
            took = "failed" if row.state == "failed" else _age(now - row.since)
            lines.append(f"{mark} {row.host:<{name}}  {where[:max(10, width - name - 16)]}  {took}")
        if done:
            lines.append(f"✓ {', '.join(done)}")
        return lines

    # -- following --------------------------------------------------------------------------------

    def _start(self, follow: Follow) -> None:
        """`tail -F` of its file on its host, its lines into the window."""
        try:
            from worms_cli.remote import Remote, Target

            if self.remote is None:
                self.remote = Remote(self.config)
            lines = 0 if follow.path == APT_LOG else LINES
            command = f"tail -n {lines} -F {shlex.quote(follow.path)} 2>/dev/null"
            if follow.name in ("localhost", "proxmox.local", "host"):
                target = Target("host", None)
            else:
                vm = self.remote.catalogue.resolve(follow.name)
                target = Target(vm["name"], vm, vm.get("pool") in self.remote.untrusted_pools)
            argv, env = self.remote.argv(target, command, root=True)
            proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)  # fmt: skip
        except Exception as exc:  # noqa: BLE001  (a window that cannot follow says so; the run goes on)
            follow.problem = f"cannot follow: {str(exc).splitlines()[0][:80] if str(exc) else type(exc).__name__}"
            return
        with self.lock:
            if self.closed or follow not in self.follows.values():  # the task ended meanwhile
                proc.terminate()
                return
            follow.proc = proc
        threading.Thread(target=self._read, args=(follow, proc), daemon=True).start()

    def _read(self, follow: Follow, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stdout.readline, b""):
            line = _clean(raw)
            with self.lock:
                if follow.proc is not proc:
                    break
                follow.last = time.monotonic()
                several = sum(1 for f in self.follows.values() if f.following) > 1
                self.lines.append(f"[{follow.name}] {line}" if several else line)
                if self.tty:
                    self._erase()
                    self._draw()

    def _stop(self, follow: Follow) -> None:
        proc, follow.proc = follow.proc, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _tick(self) -> None:
        while not self.closed:
            time.sleep(1)
            try:
                with self.lock:
                    now = time.monotonic()
                    due = [f for f in self.follows.values() if not f.following and now - f.started >= DELAY]
                    due = due[:max(0, MAX_FOLLOWED - sum(1 for f in self.follows.values() if f.following))]
                    for follow in due:
                        follow.following = True
                for follow in due:  # outside the lock: resolving a VM may ask Proxmox, output must not wait
                    self._start(follow)
                with self.lock:
                    if self.tty:
                        self._erase()
                        self._draw()
                    elif (self.follows or self._progress_rows(200)) and now - self.beat >= HEARTBEAT:
                        self.beat = now
                        self.emit(self._heartbeat().encode())
            except Exception:  # noqa: BLE001  (the window never stops the run)
                pass

    # -- the window -------------------------------------------------------------------------------

    def _shown(self) -> list[Follow]:
        now = time.monotonic()
        return [f for f in self.follows.values() if now - f.started >= DELAY]

    def _header(self, shown: list[Follow]) -> str:
        now = time.monotonic()
        first = min(shown, key=lambda f: f.started)
        last = max((f.last for f in shown if f.last), default=None)
        where = f"{first.name}: {first.path}" if len(shown) == 1 else f"{len(shown)} hosts: {', '.join(f.name for f in shown[:3])}" + (" …" if len(shown) > 3 else "")
        quiet = "no line yet" if last is None else f"last line {_age(now - last)} ago"
        problem = next((f.problem for f in shown if f.problem), "")
        return f"{where} · {_age(now - first.started)} · {problem or quiet}"

    def _heartbeat(self) -> str:
        rows = self._progress_rows(200)
        if rows and not self.follows:
            busy = [r for r in rows[1:] if r.startswith("▸")]
            return f"  … {rows[0]}" + (f"; running: {'; '.join(r[2:] for r in busy[:4])}" if busy else "") + "\n"
        shown = self._shown() or list(self.follows.values())
        text = f"  … {self._header(shown)}"
        return text + (f": {self.lines[-1]}" if self.lines else "") + "\n"

    def _draw(self) -> None:
        if not self.tty or self.drawn or not self.at_line_start or self.closed:
            return
        try:
            columns = os.get_terminal_size(self.terminal).columns
        except OSError:
            columns = 0
        width = columns - 1 if columns >= 20 else 100  # 0: a terminal that does not say
        shown = self._shown()
        progress = self._progress_rows(width - 2)
        if not shown and not progress:
            return
        rows = [f"┌ {progress[0]}", *(f"│ {line}" for line in progress[1:])] if progress else []
        if shown:
            rows += [f"{'├' if rows else '┌'} {self._header(shown)}", *(f"│ {line}" for line in self.lines)]
        text = "".join(f"\x1b[2m{row[:width]}\x1b[0m\n" for row in rows)
        _write_all(self.terminal, text.encode())
        self.drawn = len(rows)

    def _erase(self) -> None:
        if self.drawn:
            _write_all(self.terminal, f"\x1b[{self.drawn}F\x1b[J".encode())
            self.drawn = 0

    def close(self) -> None:
        with self.lock:
            self.closed = True
            for follow in self.follows.values():
                self._stop(follow)
            self.follows.clear()
            try:
                self._erase()
            except OSError:
                pass


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]
