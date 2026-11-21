"""One change of the platform at a time: every `worms` that changes it (apply, destroy, internet,
vm start|stop|restart|reset|rebuild, service restart, db restore|reset, key sync, step) holds
/run/worms/worms.lock on the node while it runs; a second one is refused, saying who holds it.
Never `vm kill`, `jobs ...` or `round ...`: those must work while a build runs. On 2026-11-18 a
second `worms` closed the internet window under a running build, and its apt hung for 1 h 40 min.
A raw ansible-playbook or tofu does not take the lock.
"""

from __future__ import annotations

import fcntl
import getpass
import os
import shlex
import subprocess
from datetime import datetime, timezone

from worms_cli.build import BuildError

LOCK = "/run/worms/worms.lock"


class Lock:
    """The node's lock for changing the platform, held for this process's life (release() ends it).
    On the node: flock(2) on LOCK. From a laptop: `flock` on the node over SSH, held while its
    stdin is open."""

    def __init__(self, session, what: str):
        self.session, self.what = session, what
        self.fd = None
        self.proc = None

    def info(self) -> str:
        return f"pid {os.getpid()} · {getpass.getuser()} · worms {self.what} · since {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC"

    def holder(self) -> str:
        code, out, _err = self.session.node_run(["cat", f"{LOCK}.info"])
        return out.strip() if code == 0 and out.strip() else "another worms"

    def acquire(self) -> "Lock":
        if self.session.laptop:
            command = (f"mkdir -p /run/worms && exec flock -n {LOCK} sh -c "
                       + shlex.quote(f"printf '%s\\n' {shlex.quote(self.info())} > {LOCK}.info; echo locked; cat >/dev/null"))
            self.proc = subprocess.Popen(self.session.ssh_argv(command), env=self.session.env(), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # fmt: skip
            if self.proc.stdout.readline().strip() != b"locked":
                self.proc.wait()
                self.proc = None
                raise BuildError(self._refused())
            return self
        os.makedirs(os.path.dirname(LOCK), exist_ok=True)
        self.fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise BuildError(self._refused()) from None
        with open(f"{LOCK}.info", "w") as out:
            out.write(self.info() + "\n")
        return self

    def _refused(self) -> str:
        return (f"another worms is changing the platform: {self.holder()}. Wait for it (`worms log run -f` where it "
                "runs), or stop it there; `worms vm kill`, `pause`, `resume` and `cancel` work meanwhile")  # fmt: skip

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001  (the lock goes with the SSH connection anyway)
                self.proc.kill()
            self.proc = None
