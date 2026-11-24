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

`diagnose(stdout, stderr)` -> Cause: `headline` (one line: the job's and the attempt's error, the
tables), `blocks` (the excerpts: the session's summary.log, the dashboard's staff pages), `logs`
(the files the output names: each failed VM's own Ansible log). Used by CommandError
(executor.py) and by the round's session (src/worms_cli/rounds.py).
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


@dataclass
class Cause:
    headline: str
    blocks: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)  # the files the output names (each failed VM's Ansible log)

    def text(self, limit: int = TEXT_LIMIT) -> str:
        """The excerpts, one after the other, cut at `limit` characters."""
        text = "\n".join(block.rstrip() for block in self.blocks if block.strip())
        return text if len(text) <= limit else text[: limit - 40].rstrip() + "\n... (cut: the log has the rest)"

    def as_dict(self) -> dict:
        return {"headline": self.headline, "detail": self.text(), "logs": self.logs[:10]}


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
        if key == "msg" or line.strip() == "msg: |" or line.strip() == "msg: |-":
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


def _ansible(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(headlines, blocks, logs) of the callback's failure blocks, one per task: the first VM's in
    full, the others it failed on named after it (each with its message when it differs)."""
    groups: dict[str, list[tuple[str, list[str]]]] = {}
    for vm, task, block in _ansible_failures(lines):
        groups.setdefault(task, []).append((vm, block))
    headlines, blocks, logs = [], [], []
    for task, failed in groups.items():
        for _vm, block in failed:
            for line in block:
                if line.strip().startswith("its whole log: "):
                    logs.append(line.strip().removeprefix("its whole log: ").strip())
        first_vm, first = failed[0]
        msg = _ansible_msg(first)
        name, _, where = task.rpartition(" · ")
        others = [vm for vm, _ in failed[1:]]
        on = first_vm + (f" and {len(others)} more VM(s)" if others else "")
        headlines.append(f"Ansible: {name or task} failed on {on} ({where or '?'}): {msg}")
        text = _trim(list(first))
        different = [f"{vm}: {_ansible_msg(block)}" for vm, block in failed[1:] if _ansible_msg(block) != msg]
        same = [vm for vm, block in failed[1:] if _ansible_msg(block) == msg]
        if same:
            text.append(f"    the same failure on: {', '.join(same)}")
        for line in different[:5]:
            text.append(f"    also failed on {line}")
        blocks.append("\n".join(text))
    return headlines, blocks, logs


def _ansible_errors(lines: list[str]) -> tuple[list[str], list[str]]:
    """Ansible's own errors (`[ERROR]:`, `ERROR!`), with their Origin and excerpt, the default
    output's `fatal: [vm]: FAILED! => ...`, and any other `fatal:` line (git's, ssh's)."""
    headlines, blocks, i = [], [], 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(("[ERROR]", "ERROR!")):
            block, i = [line], i + 1
            while i < len(lines) and not lines[i].startswith(STARTS) and len(block) < 15:
                block.append(lines[i])
                i += 1
            headlines.append("Ansible: " + line.removeprefix("[ERROR]:").removeprefix("ERROR!").strip()[:300])
            blocks.append("\n".join(_trim(block)))
            continue
        if line.lstrip().startswith(("fatal: ", "failed: [")):
            ansible = line.lstrip().startswith(("fatal: [", "failed: ["))
            headlines.append(("Ansible: " if ansible else "") + line.strip()[:300])
            blocks.append(line.strip()[:1200])
        i += 1
    return headlines, blocks


# -- OpenTofu --------------------------------------------------------------------------------------


def _tofu(lines: list[str]) -> tuple[list[str], list[str]]:
    """OpenTofu's `Error:` diagnostics, boxed (│ ... ╵) or not (-no-color)."""
    headlines, blocks, i = [], [], 0
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
            headlines.append("OpenTofu: " + line.removeprefix("Error: ").strip()[:300] + (f" ({where.rstrip(':')})" if where else ""))
            blocks.append("\n".join(_trim(block)))
            continue
        i += 1
    return headlines, blocks


# -- Python ----------------------------------------------------------------------------------------


def _traceback(lines: list[str]) -> tuple[list[str], list[str]]:
    """The last traceback: its last two frames and the exception."""
    starts = [n for n, line in enumerate(lines) if line.startswith("Traceback (most recent call last):")]
    if not starts:
        return [], []
    body, n = [], starts[-1] + 1
    while n < len(lines) and lines[n].startswith((" ", "\t")):
        body.append(lines[n])
        n += 1
    exception = lines[n].strip() if n < len(lines) else "(the exception is not in the output)"
    frames = [k for k, line in enumerate(body) if line.lstrip().startswith('File "')]
    kept = body[frames[-2]:] if len(frames) >= 2 else body
    kept = [line for line in kept if set(line.strip()) - set("^~ ")]  # no ^^^^ markers
    return [f"Python: {exception[:300]}"], ["\n".join(["Traceback (the last frames):", *kept[-8:], exception])]


def from_exception(exc: BaseException) -> Cause:
    """A handler's or a gameplan's exception: what it says, and where it was raised (the last
    frame, and the last one in a gameplan file if that is another)."""
    cause = getattr(exc, "cause", None)
    if isinstance(cause, Cause):  # a CommandError: its command's output says why
        return cause
    headline = f"{type(exc).__name__}: {exc}".strip()
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    lines = []
    if frames:
        last = frames[-1]
        lines.append(f"raised at {last.filename}:{last.lineno}, in {last.name}" + (f": {last.line.strip()}" if last.line else ""))
        mine = [frame for frame in frames if "/gameplans/" in frame.filename]
        if mine and mine[-1] is not last:
            frame = mine[-1]
            lines.append(f"from the gameplan at {frame.filename}:{frame.lineno}, in {frame.name}"
                         + (f": {frame.line.strip()}" if frame.line else ""))  # fmt: skip
        for frame in frames[-4:-1]:
            lines.append(f"  called from {frame.filename}:{frame.lineno}, in {frame.name}")
    return Cause(headline=headline.splitlines()[0][:400] if headline else type(exc).__name__,
                 blocks=["\n".join([headline[:1500], *lines])])  # fmt: skip


# -- the worms CLI ---------------------------------------------------------------------------------


def _worms(lines: list[str]) -> tuple[list[str], list[str]]:
    """The CLI's own error: lines: the causes first, the "step stopped: command failed" wrappers after."""
    found = []
    for line in lines:
        if line.startswith("error: ") and line not in found:
            found.append(line)
    causes = [line for line in found if not any(w in line for w in WRAPPERS)]
    wrappers = [line for line in found if line not in causes]
    headlines = [line.removeprefix("error: ")[:300] for line in causes]
    return headlines, [line[:600] for line in causes[-3:] + wrappers[-2:]]


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
    headlines, blocks, logs = _ansible(both)
    for finder in (_ansible_errors, _tofu, _traceback):
        more, excerpts = finder(both)
        headlines += more
        blocks += excerpts
    worms_headlines, worms_blocks = _worms(err + out)
    headlines += worms_headlines
    blocks += worms_blocks
    if not blocks:
        tail = _tail(err) or _tail(out)
        if tail:
            headlines.append(tail[-1].strip()[:300])
            blocks.append("\n".join(["the end of its output:", *tail]))
    return Cause(headline=(headlines[0] if headlines else "no output to say why"), blocks=blocks, logs=logs)


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
