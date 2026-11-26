"""Child processes an interrupt never kills mid-operation: OpenTofu, Ansible, the CLI's steps.

Each starts in a session of its own, so a Ctrl-C at the terminal (or a signal to the process
group) reaches only this process, which then stops the child itself (`stop`): one SIGINT, on
which OpenTofu and Ansible finish the operation under way and exit; up to GRACE seconds for
that; then SIGTERM; then SIGKILL, saying what may be left (a `tofu apply` killed half-way can
leave VMs Proxmox has and the state does not: `worms status` looks for them). A Ctrl-C while
it waits moves on to the next signal at once. Before 2026-11-26 the children shared the
terminal's process group, so Ctrl-C hit them at the same moment as the CLI, and the round
agent's executor SIGKILLed the command under way.

Standard library only: the round agent (executor.py) and the CLI (worms_cli/build.py, from this
checkout's scripts/) use it alike.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

GRACE = 120  # s after the SIGINT
TERM_GRACE = 10  # s after the SIGTERM


def _say(text: str) -> None:
    try:
        print(text, file=sys.stderr, flush=True)
    except OSError:  # no terminal any more
        pass


def _name(proc) -> str:
    args = proc.args if isinstance(proc.args, (list, tuple)) else str(proc.args).split()
    words = [os.path.basename(str(word)) for word in args[:4]]
    if words[:1] == ["sh"] and len(args) > 2:  # sh -c "cd ... && tofu apply ...": the command, not sh
        command = str(args[-1]).split("&&")[-1].split()
        words = [os.path.basename(command[0])] if command else words
    return " ".join(words[:2]) if words[:1] in (["python3"], ["python"]) else (words[0] if words else "the command")


def start(argv, **kwargs) -> subprocess.Popen:
    """subprocess.Popen, in a session of its own, its stdin /dev/null unless given. A child in a
    session of its own has no controlling terminal, so the terminal is no stdin for it: Proxmox's
    `qm` takes a terminal on stdin for its own and fails ("failed to tcsetpgrp: Inappropriate
    ioctl for device", no worker started), which stopped `worms round start` at a terminal
    (2026-11-26) from shutting the devboxes down."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.Popen(argv, start_new_session=True, **kwargs)


def stop(proc, grace: float = GRACE, say=_say) -> None:
    """`proc` and its process group stopped: SIGINT once, then SIGTERM, then SIGKILL."""
    name = _name(proc)
    for sig, wait in ((signal.SIGINT, grace), (signal.SIGTERM, TERM_GRACE), (signal.SIGKILL, None)):
        if proc.poll() is not None:
            return
        if sig == signal.SIGINT:
            say(f"stopping {name}: it finishes what it is doing and exits (up to {grace:g} s; Ctrl-C again: at once)")
        elif sig == signal.SIGKILL:
            say(f"{name} was killed: what it was doing may be half-done; `worms status` says what is left (orphan VMs, ...)")
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        if wait is None:
            proc.wait()
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue
        except KeyboardInterrupt:  # Ctrl-C again: the next signal, now
            continue


def run(argv, *, input=None, capture_output: bool = False, timeout=None, grace: float = GRACE, say=_say,
        **kwargs) -> subprocess.CompletedProcess:  # fmt: skip
    """subprocess.run, the child in a session of its own, and stopped by `stop` when this process
    is interrupted or the timeout passes (subprocess.TimeoutExpired raised after, as before)."""
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    proc = start(argv, **kwargs)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except BaseException:
        stop(proc, grace, say)
        try:
            proc.communicate(timeout=5)  # the pipes closed
        except Exception:  # noqa: BLE001
            pass
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
