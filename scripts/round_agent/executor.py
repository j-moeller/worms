"""How the round agent runs things on the Proxmox host.

Everything the agent does to the outside world goes through an Executor, so
the handlers (handlers.py) are pure logic and the whole agent can be tested
with a fake executor. The real one runs commands as argument lists (never a
shell string), with timeouts, and reads OpenTofu outputs as JSON.

The agent runs as root on the Proxmox node, from the repository checkout
(like `make`): tofu, ansible-playbook, qm and ssh are on PATH and the
OpenTofu stacks and scripts are in the tree.

Each job's commands, and the output of its builds, go to a log as they happen
(JobLog): follow it with `tail -F /var/log/worms/jobs/latest.log`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

log = logging.getLogger("round-agent")

DEFAULT_JOB_LOG_DIR = "/var/log/worms/jobs"
JOB_LOG_KEEP_DAYS = 30
# A background child can keep a pipe open after the command itself has ended. The
# job does not wait for it longer than this (seconds).
PIPE_GRACE = 30
_COLOUR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# SSH options for reaching VMs on the management bridges (host -> VM only).
# Host keys are not pinned (lab VMs are rebuilt with fresh keys), matching the
# repo's ansible.cfg (host_key_checking = False). The WORMS key in the agent
# worms-node-run started is the one key offered: never root's ~/.ssh/id_*.
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "IdentityFile=none",
    "-o", "IdentityAgent=SSH_AUTH_SOCK",
]


REASON_PREFIXES = ("fatal:", "failed:", "[ERROR]", "ERROR!", "Error:", "error:")


def known_hosts_problems(path) -> list:
    """The lines of a known_hosts file that OpenTofu's Proxmox provider cannot read: it
    parses the whole file (Go's knownhosts) before every SSH login, and one bad line
    stops every snippet upload ("failed to read .../known_hosts"). ssh itself skips
    such lines, so nothing else notices. [(line number, reason, start of the line)]"""
    import base64
    import binascii

    problems = []
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return problems
    for number, line in enumerate(lines, 1):
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if fields[0] in ("@cert-authority", "@revoked"):
            fields = fields[1:]
        try:
            if len(fields) < 3:
                raise ValueError("fewer than three fields (host, key type, key)")
            base64.b64decode(fields[2], validate=True)
            if fields[0].startswith("|1|"):
                for part in fields[0].split("|")[2:4]:
                    base64.b64decode(part, validate=True)
        except (ValueError, binascii.Error) as exc:
            problems.append((number, str(exc) or "not base64", line[:60]))
    return problems


class CommandError(RuntimeError):
    """The job's `error` (and the dashboard's "Recently failed"): the END of stderr,
    where Ansible, OpenTofu and the worms CLI put the reason (its start is Ansible's
    inventory-plugin warnings), and the last failed task, which Ansible prints on stdout."""

    def __init__(self, argv, returncode, stderr, log_path=None, stdout=""):
        self.argv, self.returncode, self.stderr = argv, returncode, stderr
        where = f" (full output: {log_path})" if log_path else ""
        err = stderr.strip()
        reason = ("..." + err[-500:]) if len(err) > 500 else err
        failed = [line.strip() for line in (stdout or "").splitlines() if line.lstrip().startswith(REASON_PREFIXES)]
        if failed:
            reason += f" | last failure: {failed[-1][:300]}"
        super().__init__(f"{argv[0]} exited {returncode}: {reason}{where}")


class JobLog:
    """One job's commands and build output, in a root-only file per job.

    Written as it happens, so a long build can be followed and a failure read
    afterwards: the job's `error` holds only the end of stderr and the last failed task. Ansible and OpenTofu output can be sensitive, which is why
    the directory is root-only, and why this stays on the node and out of the database.

    A problem with the log never fails a job. Without a file (no job is running, or it
    could not be opened), every call does nothing.
    """

    def __init__(self, path=None, handle=None):
        self.path, self._handle = path, handle
        self._lock = threading.Lock()

    @classmethod
    def open(cls, directory, job_uuid, kind):
        directory = Path(directory)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
            cutoff = time.time() - JOB_LOG_KEEP_DAYS * 86400
            for old in directory.glob("*.log"):
                if not old.is_symlink() and old.stat().st_mtime < cutoff:
                    old.unlink()
            path = directory / f"{job_uuid}.log"
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            handle = os.fdopen(os.open(path, flags, 0o600), "a", buffering=1, errors="replace")
            # Replaced in one step, so `tail -F latest.log` moves on to the next job.
            pointer = directory / ".latest.tmp"
            pointer.unlink(missing_ok=True)
            pointer.symlink_to(path.name)
            pointer.replace(directory / "latest.log")
        except OSError as exc:
            log.warning("job %s: no output log: %s", job_uuid, exc)
            return cls()
        job_log = cls(path, handle)
        job_log.note(f"job {job_uuid} ({kind})")
        return job_log

    def write(self, text):
        with self._lock:
            if self._handle is None:
                return
            try:
                self._handle.write(_COLOUR.sub("", text))
            except (OSError, ValueError) as exc:
                log.warning("output log %s: %s; no more output goes there", self.path, exc)
                self._handle = None

    def note(self, text):
        self.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")

    def close(self):
        with self._lock:
            handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError as exc:
                log.warning("output log %s: %s", self.path, exc)


class Executor:
    def __init__(self, repo_root, ssh_user="debian", log_dir=DEFAULT_JOB_LOG_DIR):
        self.repo_root = Path(repo_root)
        self.ssh_user = ssh_user
        self.log_dir = log_dir
        self._log = JobLog()

    @contextmanager
    def job_log(self, job_uuid, kind):
        """Everything run inside this block is logged for that job."""
        self._log = JobLog.open(self.log_dir, job_uuid, kind)
        try:
            yield self._log
        finally:
            self._log.close()
            self._log = JobLog()

    def run(self, argv, cwd=None, input=None, timeout=1800, check=True, stream=False):
        """Run argv (a list). Returns stdout. Raises CommandError on failure.

        Every command line goes to the job's log, which is safe because secrets travel
        through `input`, never argv (see ssh). Its output goes there only with `stream`,
        for builds. By default output is for the caller alone: `tofu output -json` can
        hold sensitive values, and `base64` on a VM returns a whole worm. A streamed
        command gets no stdin."""
        argv = [str(a) for a in argv]
        self._log.note("$ " + shlex.join(argv))
        started = time.monotonic()
        try:
            if stream:
                returncode, stdout, stderr = self._stream(argv, cwd, timeout)
            else:
                proc = subprocess.run(
                    argv,
                    cwd=str(cwd or self.repo_root),
                    input=input,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            self._log.note(f"timed out after {timeout} s")
            raise
        self._log.note(f"exit {returncode} after {time.monotonic() - started:.0f} s")
        if check and returncode != 0:
            raise CommandError(argv, returncode, stderr, self._log.path, stdout)
        return stdout

    def _stream(self, argv, cwd, timeout):
        """Run argv, capturing its output and copying each line to the log as it comes."""
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd or self.repo_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env={**os.environ, "PYTHONUNBUFFERED": "1"},  # the CLI and Ansible are Python
        )
        captured = {proc.stdout: [], proc.stderr: []}

        def pump(pipe):
            for line in pipe:
                captured[pipe].append(line)
                self._log.write(line)

        pumps = [threading.Thread(target=pump, args=(pipe,), daemon=True) for pipe in captured]
        for thread in pumps:
            thread.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        finally:
            for thread in pumps:
                thread.join(PIPE_GRACE)
        return proc.returncode, "".join(captured[proc.stdout]), "".join(captured[proc.stderr])

    def worms(self, step, timeout=3600, args=None):
        """Run a pipeline step through the worms CLI (src/manage.py, `worms step`: never
        interactive). `args` are passed through to ansible-playbook (after `--`)."""
        import shlex

        argv = ["python3", "src/manage.py", "step", step]
        if args:
            argv += ["--", *shlex.split(args)]
        return self.run(argv, timeout=timeout, stream=True)

    def generate_devbox(self, teams, output_file):
        """Write the devbox OpenTofu definition for `teams` (a list of
        {slug, index, ...}) to output_file (generated_devboxes.tf)."""
        script = self.repo_root / "scripts" / "generate_devbox.py"
        import json as _json

        return self.run(
            ["python3", script, "--output", str(output_file)],
            input=_json.dumps([{"slug": t["slug"], "index": t["index"]} for t in teams]),
            timeout=120,
        )

    def tofu(self, stack, *args, timeout=3600):
        path = self.repo_root / "opentofu" / "stacks" / stack
        if args[:1] in (("apply",), ("destroy",)):
            known_hosts = Path.home() / ".ssh" / "known_hosts"
            problems = known_hosts_problems(known_hosts)
            if problems:
                raise RuntimeError(
                    f"{known_hosts} has lines OpenTofu's Proxmox provider cannot read: "
                    + "; ".join(f"line {n} ({why})" for n, why, _ in problems)
                    + f". Remove them: sed -i '{';'.join(f'{n}d' for n, _, _ in problems)}' {known_hosts}"
                )
        return self.run(["tofu", f"-chdir={path}", *args], timeout=timeout, stream=args[:1] != ("output",))

    def tofu_output(self, stack):
        out = self.tofu(stack, "output", "-json", timeout=120)
        return {k: v.get("value") for k, v in json.loads(out or "{}").items()}

    def write_file(self, path, content):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def note(self, text):
        """A line in the current job's log (on the node only)."""
        self._log.note(text)

    def install_sandbox_definition(self, hcl):
        """Make `hcl` the one active generated sandbox definition: write
        generated_vms.tf and remove any other generated_vms*.tf (they define
        the same locals and would clash). These files are runtime state."""
        sandbox = self.repo_root / "opentofu" / "stacks" / "sandbox"
        for stale in sandbox.glob("generated_vms*.tf"):
            if stale.name != "generated_vms.tf":
                stale.unlink()
        (sandbox / "generated_vms.tf").write_text(hcl)

    def sandbox_definition(self):
        """The generated sandbox definition the stack holds now, or "" if there is none."""
        path = self.repo_root / "opentofu" / "stacks" / "sandbox" / "generated_vms.tf"
        return path.read_text() if path.exists() else ""

    def read_json(self, path):
        return json.loads(Path(path).read_text() or "{}")

    @staticmethod
    def remote_command(argv) -> str:
        """The command line ssh sends for `argv`. ssh does not pass an
        argument list: it joins its arguments with spaces and the VM's login
        shell parses the result again. Quoting every argument makes that shell
        rebuild exactly `argv` (shlex.split(remote_command(argv)) == argv);
        unquoted, `sh -c "base64 -d > f"` would arrive as `sh -c base64 -d > f`."""
        return shlex.join(str(a) for a in argv)

    def ssh(self, host, argv, input=None, timeout=120):
        """Run `argv` on a VM, exactly as given (remote_command quotes it for
        the VM's shell; no local shell is involved either). Secrets go
        through `input` (stdin), never argv."""
        return self.run(
            ["ssh", *SSH_OPTS, f"{self.ssh_user}@{host}", "--", self.remote_command(argv)],
            input=input,
            timeout=timeout,
        )
