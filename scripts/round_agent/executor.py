"""How the round agent runs things on the Proxmox host.

Everything the agent does to the outside world goes through an Executor, so
the handlers (handlers.py) are pure logic and the whole agent can be tested
with a fake executor. The real one runs commands as argument lists (never a
shell string), with timeouts, and reads OpenTofu outputs as JSON.

The agent runs as root on the Proxmox node, from the repository checkout
(like `make`): tofu, ansible-playbook, qm and ssh are on PATH and the
OpenTofu stacks and scripts are in the tree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

# SSH options for reaching VMs on the management bridges (host -> VM only).
# Host keys are not pinned (lab VMs are rebuilt with fresh keys), matching the
# repo's ansible.cfg (host_key_checking = False).
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
]


class CommandError(RuntimeError):
    def __init__(self, argv, returncode, stderr):
        self.argv, self.returncode, self.stderr = argv, returncode, stderr
        super().__init__(f"{argv[0]} exited {returncode}: {stderr.strip()[:500]}")


class Executor:
    def __init__(self, repo_root, ssh_user="debian"):
        self.repo_root = Path(repo_root)
        self.ssh_user = ssh_user

    def run(self, argv, cwd=None, input=None, timeout=1800, check=True):
        """Run argv (a list). Returns stdout. Raises CommandError on failure."""
        proc = subprocess.run(
            [str(a) for a in argv],
            cwd=str(cwd or self.repo_root),
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise CommandError(argv, proc.returncode, proc.stderr)
        return proc.stdout

    def make(self, target, timeout=3600):
        return self.run(["make", target], timeout=timeout)

    def tofu(self, stack, *args, timeout=3600):
        path = self.repo_root / "opentofu" / "stacks" / stack
        return self.run(["tofu", f"-chdir={path}", *args], timeout=timeout)

    def tofu_output(self, stack):
        out = self.tofu(stack, "output", "-json", timeout=120)
        return {k: v.get("value") for k, v in json.loads(out or "{}").items()}

    def write_file(self, path, content):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def generate_sandbox(self, seed, params, network_file):
        """Run scripts/generate_sandbox.py; returns the HCL (stdout). The
        network graph is written to network_file (read back by the handler)."""
        script = self.repo_root / "scripts" / "generate_sandbox.py"
        return self.run(
            [
                "python3", script,
                "--seed", str(seed),
                "--networks", str(params["networks"]),
                "--vms", str(params["vms"]),
                "--sites", str(params["sites"]),
                "--max-interfaces", str(params["max_interfaces"]),
                "--network-file", str(network_file),
            ],
            timeout=120,
        )

    def install_sandbox_definition(self, hcl):
        """Make `hcl` the one active generated sandbox definition: write
        generated_vms.tf and remove any other generated_vms*.tf (they define
        the same locals and would clash). These files are runtime state."""
        sandbox = self.repo_root / "opentofu" / "stacks" / "sandbox"
        for stale in sandbox.glob("generated_vms*.tf"):
            if stale.name != "generated_vms.tf":
                stale.unlink()
        (sandbox / "generated_vms.tf").write_text(hcl)

    def read_json(self, path):
        return json.loads(Path(path).read_text() or "{}")

    def ssh(self, host, argv, input=None, timeout=120):
        """Run a command on a VM. `argv` is a list; it is passed as separate
        arguments to ssh (no local shell), so values in it are not reparsed by
        a shell on this side. Secrets go through `input` (stdin), never argv."""
        return self.run(
            ["ssh", *SSH_OPTS, f"{self.ssh_user}@{host}", "--", *argv],
            input=input,
            timeout=timeout,
        )
