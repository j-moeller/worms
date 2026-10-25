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
import shlex
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

    def worms(self, component, timeout=3600, args=None):
        """Run a build component through the worms CLI (src/manage.py), which is
        where the former Makefile logic now lives. `args` are passed through to
        ansible-playbook (after `--`)."""
        import shlex

        argv = ["python3", "src/manage.py", "build", component]
        if args:
            argv += ["--", *shlex.split(args)]
        return self.run(argv, timeout=timeout)

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
                "--vm-ram", str(params["vm_ram"]),
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
