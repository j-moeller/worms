"""Why a step failed, for a person: the few lines of a failed command's whole output that say it.

A failed `worms step` (Ansible), `tofu apply` or handler leaves thousands of lines, and its end
says least: on 2026-11-23 a prepare's error was the tail of one of Ansible's deprecation warnings,
while the failed task (which VM, which task, which file and line, Ansible's msg) was on stdout,
in the step's log only. What says why, looked for in this order:

    Ansible's failed tasks     the worms callback's block for each (ansible/callback_plugins/worms.py):
                               `✗ <vm> · <task> · <file:line>`, its msg, rc, stderr, the VM's whole
                               log; one block per task, the other VMs it failed on named after it
    Ansible's own errors       `[ERROR]:` / `ERROR!` (a playbook that does not load, an undefined
                               variable), `fatal: [vm]: FAILED!` (Ansible's default output), any `fatal:`
    OpenTofu's errors          each `Error:` diagnostic, with its resource and file and line
    a Python traceback         where it was raised, and the exception
    the worms CLI's own        its `error:` lines (which command of the step failed, and why)

Nothing of these: the end of stderr (else of stdout), Ansible's warnings left out.

`diagnose(stdout, stderr)` -> Cause: `headline` (one line), `blocks` (the excerpts), `logs` (the
files the output names: each failed VM's own Ansible log), and `public`: the same cause without a
word of what the output said, only where it failed (an Ansible role and its file:line, the VMs;
OpenTofu's module file and line; an exception's type and where it was raised). The host-side
accounts (the terminal, the step's log, the session's summary.log) get all of it; the database,
which the dashboard reads, gets all of it only from a job whose output cannot quote a gameplan,
and `public` alone from one that can (handlers._quiet: the gameplan's content never reaches the
dashboard). `for_database(exc, cause)` decides. Used by CommandError (executor.py), the round
agent (agent.py) and the round's session (src/worms_cli/rounds.py).
"""

from __future__ import annotations

import re
import shlex
import traceback
from dataclasses import dataclass, field

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
WARNINGS = ("[WARNING]", "[DEPRECATION WARNING]", "[WARN]", "Warning: ")
# A line that starts something else: a warning or an error block ends at it.
STARTS = ("[WARNING]", "[DEPRECATION WARNING]", "[ERROR]", "ERROR!", "error:", "Error:", "Traceback", "fatal:", "✗ ", "── ")
WRAPPERS = ("stopped: command failed", "build stopped:", "step stopped:")  # the CLI's "which command": not why
BLOCK_LINES = 30  # at most this many lines of one excerpt
TEXT_LIMIT = 3500  # the excerpts together (the dashboard's, summary.log's)
WITHHELD = "its message is not in the database (it may quote the gameplan): the step's log and summary.log on the host have it"


@dataclass
class Cause:
    headline: str
    blocks: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)  # the files the output names (each failed VM's Ansible log)
    public: str = ""  # the cause without anything the output said: where it failed only

    def text(self, limit: int = TEXT_LIMIT) -> str:
        """The excerpts, one after the other, cut at `limit` characters."""
        text = "\n".join(block.rstrip() for block in self.blocks if block.strip())
        return text if len(text) <= limit else text[: limit - 40].rstrip() + "\n... (cut: the log has the rest)"

    def as_dict(self) -> dict:
        return {"headline": self.headline, "detail": self.text(), "logs": self.logs[:10]}


def for_database(exc: BaseException, cause: Cause) -> dict:
    """What of a failure may go to the database (the dashboard's staff pages read it): all of it,
    unless the job's output may quote a gameplan (a handler of handlers._quiet: it loads a
    gameplan or applies it). Then where it failed only, and where the rest is."""
    if getattr(exc, "original", None) is None:
        return cause.as_dict()
    return {"headline": cause.public or "(where: not known)", "detail": "", "logs": cause.logs[:10], "withheld": WITHHELD}


def _lines(text: str) -> list[str]:
    return [line for line in ANSI.sub("", text or "").replace("\r", "").splitlines() if not line.startswith("\x1e")]


def _trim(block: list[str]) -> list[str]:
    while block and not block[-1].strip():
        block.pop()
    if len(block) > BLOCK_LINES:
        block = block[:BLOCK_LINES] + [f"    ... ({len(block) - BLOCK_LINES} more lines in the log)"]
    return block


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return text


@dataclass
class _Found:
    headlines: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    publics: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    def add(self, headline, block, public):
        self.headlines.append(headline)
        self.blocks.append(block)
        self.publics.append(public)


# -- Ansible ---------------------------------------------------------------------------------------


def _ansible_failures(lines: list[str]) -> list[tuple[str, str, list[str]]]:
    """The callback's failure blocks: [(vm, `task · file:line`, lines)]."""
    found, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("✗ ") and " · " in line:
            block, i = [line], i + 1
            # Its lines are indented; a YAML block scalar inside may have an empty line.
            while i < len(lines) and (lines[i].startswith((" ", "\t"))
                                      or (not lines[i].strip() and i + 1 < len(lines) and lines[i + 1].startswith("      "))):  # fmt: skip
                block.append(lines[i])
                i += 1
            vm, _, rest = line[2:].partition(" · ")
            found.append((vm.strip(), rest.strip(), block))
            continue
        i += 1
    return found


def _ansible_msg(block: list[str]) -> str:
    """The most specific msg of a failure block: the last (a loop item's comes after the task's
    "One or more items failed"); else its stderr's first line, else its rc."""
    msg = ""
    for n, line in enumerate(block):
        key, _, value = line.strip().partition(": ")
        if key == "msg" or line.strip() in ("msg: |", "msg: |-"):
            value = value.strip()
            if value in ("|", "|-", ">", ">-") and n + 1 < len(block):
                value = block[n + 1].strip()
            if value:
                msg = _unquote(value)
    if msg and msg not in ("One or more items failed", "All items completed"):
        return msg
    for n, line in enumerate(block):
        key, _, value = line.strip().partition(": ")
        if key in ("stderr", "module_stderr"):
            value = value.strip()
            if value in ("|", "|-") and n + 1 < len(block):
                value = block[n + 1].strip()
            if value:
                return _unquote(value)
    for line in block:
        if line.strip().startswith(("rc: ", "reason: ")):
            return line.strip()
    return msg or "(no msg: see the VM's log)"


def _ansible(lines: list[str], found: _Found) -> None:
    """The callback's failure blocks, one per task: the first VM's in full, the others it failed
    on named after it (each with its message when it differs). Public: the task's role, its
    file:line and the VMs, never the task's name (Ansible renders it) nor what it said."""
    groups: dict[str, list[tuple[str, list[str]]]] = {}
    for vm, task, block in _ansible_failures(lines):
        groups.setdefault(task, []).append((vm, block))
    for task, failed in groups.items():
        for _vm, block in failed:
            for line in block:
                if line.strip().startswith("its whole log: "):
                    found.logs.append(line.strip().removeprefix("its whole log: ").strip())
        first_vm, first = failed[0]
        msg = _ansible_msg(first)
        name, _, where = task.rpartition(" · ")
        role = (name or task).split(" : ", 1)[0] if " : " in (name or task) else ""
        others = [vm for vm, _ in failed[1:]]
        on = first_vm + (f" and {len(others)} more VM(s)" if others else "")
        text = _trim(list(first))
        different = [f"{vm}: {_ansible_msg(block)}" for vm, block in failed[1:] if _ansible_msg(block) != msg]
        same = [vm for vm, block in failed[1:] if _ansible_msg(block) == msg]
        if same:
            text.append(f"    the same failure on: {', '.join(same)}")
        for line in different[:5]:
            text.append(f"    also failed on {line}")
        found.add(f"Ansible: {name or task} failed on {on} ({where or '?'}): {msg}", "\n".join(text),
                  f"Ansible: a task{f' of the role {role}' if role else ''} failed on {on} ({where or '?'})")  # fmt: skip


def _ansible_errors(lines: list[str], found: _Found) -> None:
    """Ansible's own errors (`[ERROR]:`, `ERROR!`), with their Origin and excerpt, the default
    output's `fatal: [vm]: FAILED! => ...`, and any other `fatal:` line (git's, ssh's)."""
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(("[ERROR]", "ERROR!")):
            block, i = [line], i + 1
            while i < len(lines) and not lines[i].startswith(STARTS) and len(block) < 15:
                block.append(lines[i])
                i += 1
            origin = next((b.strip().removeprefix("Origin:").strip() for b in block if b.startswith("Origin:")), "")
            found.add("Ansible: " + line.removeprefix("[ERROR]:").removeprefix("ERROR!").strip()[:300],
                      "\n".join(_trim(block)), "Ansible: an error of its own" + (f" (at {origin})" if origin else ""))  # fmt: skip
            continue
        if line.lstrip().startswith(("fatal: ", "failed: [")):
            ansible = line.lstrip().startswith(("fatal: [", "failed: ["))
            vm = re.match(r"\s*(?:fatal|failed): \[([^\]]+)\]", line)
            found.add(("Ansible: " if ansible else "") + line.strip()[:300], line.strip()[:1200],
                      f"Ansible: a task failed on {vm.group(1)}" if vm else "a command said fatal:")  # fmt: skip
        i += 1


# -- OpenTofu --------------------------------------------------------------------------------------


def _tofu(lines: list[str], found: _Found) -> None:
    """OpenTofu's `Error:` diagnostics, boxed (│ ... ╵) or not (-no-color). Public: the module
    file and line it names."""
    i = 0
    while i < len(lines):
        raw = lines[i]
        boxed = raw.startswith("│")
        line = raw[2:] if raw.startswith("│ ") else raw
        if line.startswith("Error: "):
            block, i = [line], i + 1
            while i < len(lines):
                raw = lines[i]
                if boxed:
                    if not raw.startswith("│"):
                        break
                    block.append(raw[2:] if raw.startswith("│ ") else "")
                else:
                    if raw.startswith(("Error: ", "Warning: ")) or (not raw.strip() and block and not block[-1].strip()):
                        break
                    block.append(raw)
                i += 1
            where = next((b.strip() for b in block if re.match(r"\s*on \S+ line \d+", b)), "")
            at = re.match(r"on (\S+ line \d+)", where)
            found.add("OpenTofu: " + line.removeprefix("Error: ").strip()[:300] + (f" ({where.rstrip(':')})" if where else ""),
                      "\n".join(_trim(block)), "OpenTofu: an error" + (f" (on {at.group(1)})" if at else ""))  # fmt: skip
            continue
        i += 1


# -- Python ----------------------------------------------------------------------------------------


def _traceback(lines: list[str], found: _Found) -> None:
    """The last traceback: its last two frames and the exception."""
    starts = [n for n, line in enumerate(lines) if line.startswith("Traceback (most recent call last):")]
    if not starts:
        return
    body, n = [], starts[-1] + 1
    while n < len(lines) and lines[n].startswith((" ", "\t")):
        body.append(lines[n])
        n += 1
    exception = lines[n].strip() if n < len(lines) else "(the exception is not in the output)"
    frames = [k for k, line in enumerate(body) if line.lstrip().startswith('File "')]
    kept = body[frames[-2]:] if len(frames) >= 2 else body
    kept = [line for line in kept if set(line.strip()) - set("^~ ")]  # no ^^^^ markers
    last = re.match(r'\s*File "([^"]+)", line (\d+), in (\S+)', body[frames[-1]]) if frames else None
    found.add(f"Python: {exception[:300]}", "\n".join(["Traceback (the last frames):", *kept[-8:], exception]),
              f"Python: {exception.split(':', 1)[0][:80]}" + (f" at {last.group(1)}:{last.group(2)}, in {last.group(3)}" if last else ""))  # fmt: skip


def from_exception(exc: BaseException) -> Cause:
    """A handler's or a gameplan's exception: what it says, and where it was raised (the last
    frame, and the last one in a gameplan file if that is another). One a quiet handler raised
    (handlers._quiet) is told by the exception it hid (`original`)."""
    original = getattr(exc, "original", None)
    if isinstance(original, BaseException):
        return from_exception(original)
    cause = getattr(exc, "cause", None)
    if isinstance(cause, Cause):  # a CommandError: its command's output says why
        return cause
    headline = f"{type(exc).__name__}: {exc}".strip()
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    lines, public = [], type(exc).__name__
    if frames:
        last = frames[-1]
        public += f" raised at {last.filename}:{last.lineno}, in {last.name}"
        lines.append(f"raised at {last.filename}:{last.lineno}, in {last.name}" + (f": {last.line.strip()}" if last.line else ""))
        mine = [frame for frame in frames if "/gameplans/" in frame.filename]
        if mine and mine[-1] is not last:
            frame = mine[-1]
            lines.append(f"from the gameplan at {frame.filename}:{frame.lineno}, in {frame.name}"
                         + (f": {frame.line.strip()}" if frame.line else ""))  # fmt: skip
        for frame in frames[-4:-1]:
            lines.append(f"  called from {frame.filename}:{frame.lineno}, in {frame.name}")
    return Cause(headline=headline.splitlines()[0][:400] if headline else type(exc).__name__,
                 blocks=["\n".join([headline[:1500], *lines])], public=public)  # fmt: skip


# -- the worms CLI ---------------------------------------------------------------------------------


def _worms(lines: list[str], found: _Found) -> None:
    """The CLI's own error: lines: the causes first, the "step stopped: command failed" wrappers
    after (public: the wrappers alone, which name a command and an exit code)."""
    seen = []
    for line in lines:
        if line.startswith("error: ") and line not in seen:
            seen.append(line)
    causes = [line for line in seen if not any(w in line for w in WRAPPERS)]
    wrappers = [line for line in seen if line not in causes]
    for line in causes:
        found.headlines.append(line.removeprefix("error: ")[:300])
        found.publics.append("the worms CLI stopped" + (f": {wrappers[-1].removeprefix('error: ')[:200]}" if wrappers else ""))
    if wrappers and not causes:
        found.headlines.append(wrappers[-1].removeprefix("error: ")[:300])
        found.publics.append(wrappers[-1].removeprefix("error: ")[:300])
    found.blocks += [line[:600] for line in causes[-3:] + wrappers[-2:]]


def _tail(lines: list[str], count: int = 12) -> list[str]:
    """The end of the output without Ansible's warnings (each up to what starts something else)."""
    kept, warning = [], False
    for line in lines:
        if line.startswith(WARNINGS):
            warning = True
            continue
        if warning and not line.startswith(STARTS):
            continue
        warning = False
        kept.append(line)
    kept = [line for line in kept if line.strip()]
    return (kept or [line for line in lines if line.strip()])[-count:]


def diagnose(stdout: str, stderr: str) -> Cause:
    out, err = _lines(stdout), _lines(stderr)
    both = out + err
    found = _Found()
    _ansible(both, found)
    _ansible_errors(both, found)
    _tofu(both, found)
    _traceback(both, found)
    _worms(err + out, found)
    if not found.blocks:
        tail = _tail(err) or _tail(out)
        if tail:
            found.add(tail[-1].strip()[:300], "\n".join(["the end of its output:", *tail]),
                      "no error the output's known formats name: the end of its output is in the step's log")  # fmt: skip
    return Cause(headline=(found.headlines[0] if found.headlines else "no output to say why"), blocks=found.blocks,
                 logs=found.logs, public=(found.publics[0] if found.publics else "no output to say why"))  # fmt: skip


def command_label(argv) -> str:
    """A command as a person names it: `worms step sandbox-configure`, `tofu apply (sandbox)`."""
    argv = [str(a) for a in argv]
    if len(argv) >= 3 and argv[0].startswith("python") and argv[1].endswith("manage.py"):
        return "worms " + " ".join(argv[2:4])
    if argv and argv[0] == "tofu":
        stack = next((a.rsplit("/", 1)[-1] for a in argv if a.startswith("-chdir=")), "")
        verb = next((a for a in argv[1:] if not a.startswith("-")), "")
        return f"tofu {verb}" + (f" ({stack})" if stack else "")
    if len(argv) >= 2 and argv[0] == "sh" and argv[1].endswith("tofu-init"):
        return f"tofu init ({argv[-1].rsplit('/', 1)[-1]})"
    if argv and argv[0] == "ssh":
        host = next((a for a in argv[1:] if "@" in a), "")
        return f"ssh {host}".strip()
    text = shlex.join(argv)
    return text if len(text) <= 100 else text[:97] + "..."
