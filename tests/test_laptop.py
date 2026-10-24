"""The laptop as controller (src/worms_cli/session.py, build.py): which steps
run here and which on the node, what each tool gets from the vault, the
inventory from a laptop, and the round agent's workspace on the node (what the
sync copies, what it never copies, what it leaves alone).

Nothing here connects anywhere: subprocess is recorded, or the tools run on
temporary directories (rsync, ssh-agent).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import FIXTURES, REPO, require

sys.path.insert(0, str(REPO / "src"))

from worms_cli import build  # noqa: E402
from worms_cli.config import Config  # noqa: E402
from worms_cli.secrets import SSH_PUBLIC_KEY, Vault  # noqa: E402
from worms_cli.session import NODE_RUN, Session  # noqa: E402

NODE = "root@pve.example"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A checkout with a (plaintext) vault and the provisioning token set."""
    require("ssh-keygen")
    real = shutil.which
    monkeypatch.setattr("worms_cli.secrets.shutil.which", lambda name: None if name == "ansible-vault" else real(name))
    vault = Vault(tmp_path)
    vault.ensure()
    vault.set("proxmox-token", "root@pam!worms=prov-secret")
    # An inventory written from a laptop (scripts/generate_inventory.py).
    (tmp_path / "ansible" / "inventory.generated.yml").write_text(
        "all:\n  children:\n    proxmox_host:\n      hosts:\n        proxmox.local:\n          ansible_host: pve.example\n"
    )
    return tmp_path


class Recorded:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, env=None, **_kwargs):
        self.calls.append((argv, env or {}))
        return subprocess.CompletedProcess(argv, 0)


OPERATOR_AGENT = "/run/operator-agent"  # your own ssh-agent (SSH_AUTH_SOCK)
WORMS_AGENT = "/run/worms-agent"  # the CLI's agent with the WORMS key


@pytest.fixture
def recorded(monkeypatch):
    """The Runner's commands are recorded, not run (the vault still uses the
    real ssh-keygen); no node to talk to. You have an ssh-agent of your own."""
    import types

    rec = Recorded()
    monkeypatch.setattr(build, "subprocess", types.SimpleNamespace(run=rec))
    monkeypatch.setattr(Session, "check_node", lambda self: None)
    monkeypatch.setattr(Session, "internet_allowed", lambda self: True)
    monkeypatch.setattr(Session, "worms_agent", lambda self: {"SSH_AUTH_SOCK": WORMS_AGENT, "SSH_AGENT_PID": "1"})
    monkeypatch.setattr(Session, "operator_agent", lambda self: True)
    monkeypatch.setenv("SSH_AUTH_SOCK", OPERATOR_AGENT)
    return rec


def runner(root, ssh=NODE):
    env = {"WORMS_REPO_ROOT": str(root)}
    if ssh:
        env["WORMS_SSH"] = ssh
    return build.Runner(Config(env=env))


def test_laptop_runs_tofu_here_with_the_vaults_inputs(checkout, recorded):
    runner(checkout).tofu("platform", "apply", "-auto-approve")
    (argv, env), = recorded.calls
    assert argv[:2] == ["sh", "-c"] and argv[2].endswith("opentofu/stacks/platform && tofu apply -auto-approve")
    assert env["TF_VAR_proxmox_api_token"] == "root@pam!worms=prov-secret"
    assert env["TF_VAR_proxmox_api_url"] == "https://pve.example:8006/"
    assert Path(env["TF_VAR_ssh_public_key"]).read_text().strip() == Vault(checkout).load()[SSH_PUBLIC_KEY].strip()
    assert env["WORMS_SSH"] == NODE


def test_tofu_keeps_your_own_ssh_agent(checkout, recorded):
    """OpenTofu's Proxmox provider logs in to root@<node> with the keys of the
    agent in SSH_AUTH_SOCK only. Replacing your agent by one with just the
    WORMS key made it fail ("unable to authenticate user root over SSH")
    wherever the node knows your key but not the WORMS key."""
    runner(checkout).tofu("platform", "apply")
    (_argv, env), = recorded.calls
    assert env["SSH_AUTH_SOCK"] == OPERATOR_AGENT
    assert env["WORMS_SSH_AUTH_SOCK"] == WORMS_AGENT  # for the VMs (inventory: IdentityAgent)


def test_without_an_agent_of_your_own_the_worms_key_reaches_the_node(checkout, recorded, monkeypatch):
    monkeypatch.setattr(Session, "operator_agent", lambda self: False)
    runner(checkout).tofu("platform", "apply")
    (_argv, env), = recorded.calls
    assert env["SSH_AUTH_SOCK"] == WORMS_AGENT == env["WORMS_SSH_AUTH_SOCK"]


def test_preflight_logs_in_like_the_provider_and_says_what_to_do(checkout, monkeypatch):
    """Agent keys only (IdentityFile=none), no prompt; a failure names the
    fixes instead of the provider's "unable to authenticate"."""
    import types

    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 255, "", "root@pve.example: Permission denied (publickey).")

    monkeypatch.setattr("worms_cli.session.subprocess", types.SimpleNamespace(run=run, SubprocessError=Exception))
    monkeypatch.setattr(Session, "worms_agent", lambda self: {"SSH_AUTH_SOCK": WORMS_AGENT, "SSH_AGENT_PID": "1"})
    monkeypatch.setattr(Session, "operator_agent", lambda self: True)
    session = Session(Config(env={"WORMS_REPO_ROOT": str(checkout), "WORMS_SSH": NODE}))
    with pytest.raises(build.SessionError) as error:
        session.tofu_env()
    assert calls == [["ssh", "-o", "BatchMode=yes", "-o", "IdentityFile=none", NODE, "true"]]
    message = str(error.value)
    assert "Permission denied (publickey)" in message and "ssh-add" in message and "worms node trust" in message


def test_the_token_only_reaches_tofu(checkout, recorded):
    r = runner(checkout)
    r.ansible("SetupDNS.yml")
    r.python("scripts/generate_inventory.py")
    for argv, env in recorded.calls:
        assert "prov-secret" not in " ".join(argv) and "TF_VAR_proxmox_api_token" not in env


def test_laptop_runs_the_nodes_stacks_on_the_node(checkout, recorded):
    r = runner(checkout)
    r.tofu("sandbox", "apply", "-auto-approve")
    r.ansible("SetupDevbox.yml")
    sync, apply, devbox = recorded.calls
    # The workspace is brought up to date first, once per run.
    assert "SetupRoundAgent.yml --tags agent-sync" in sync[0][2]
    # Your own SSH setup to the node, as `ssh root@<node>`.
    assert apply[0] == ["ssh", "-o", "BatchMode=yes", NODE, f"{NODE_RUN} opentofu/stacks/sandbox 'tofu apply -auto-approve'"]
    assert apply[1]["SSH_AUTH_SOCK"] == OPERATOR_AGENT
    assert "TF_VAR_proxmox_api_token" not in apply[1]  # the node has its own (the agent's env file)
    assert devbox[0][0] == "ssh" and devbox[0][-2] == NODE
    assert devbox[0][-1].startswith(f"{NODE_RUN} ansible ") and "ansible-playbook SetupDevbox.yml" in devbox[0][-1]
    assert "vault.yml" not in devbox[0][-1]


def test_init_of_the_nodes_stacks_happens_on_the_node(checkout, recorded):
    """A first `worms build` starts with `tofu init` of every stack, before
    there is an inventory or a CA to sync with; the workspace sync initialises
    the sandbox and devbox stacks on the node instead."""
    assert runner(checkout).tofu("sandbox", "init", "-input=false") == 0
    assert not recorded.calls


def test_laptop_playbooks_get_the_vault_and_the_nodes_internet_flag(checkout, recorded):
    runner(checkout).ansible("SetupPrivateInternet.yml")
    (argv, _env), = recorded.calls
    assert "ansible-playbook -e @group_vars/vault.yml -e internet_enabled=true SetupPrivateInternet.yml" in argv[2]


def test_laptop_refuses_an_inventory_written_on_the_node(checkout, recorded):
    """It says `ansible_connection: local` for the node: the node's roles
    would run on the laptop."""
    inventory = checkout / "ansible" / "inventory.generated.yml"
    inventory.write_text(inventory.read_text().replace("ansible_host: pve.example", "ansible_connection: local"))
    with pytest.raises(build.BuildError, match="written on the node"):
        runner(checkout).ansible("SetupHostNetwork.yml")
    inventory.unlink()
    with pytest.raises(build.BuildError, match="worms build inventory"):
        runner(checkout).ansible("SetupHostNetwork.yml")
    assert not recorded.calls


def test_a_checkout_on_the_node_uses_the_agents_workspace(checkout, recorded):
    """Operator logged in on the node with a checkout (and vault): the sandbox
    and devbox stacks still have one state, the agent's (/opt/worms)."""
    runner(checkout, ssh="").tofu("devbox", "apply")
    sync, apply = recorded.calls
    assert "SetupRoundAgent.yml --tags agent-sync" in sync[0][2]
    assert apply[0] == ["sh", "-c", f"{NODE_RUN} opentofu/stacks/devbox 'tofu apply'"]


def test_host_commands_run_on_the_node(checkout, recorded):
    runner(checkout).node_sh("qm list")
    (argv, _env), = recorded.calls
    assert argv[0] == "ssh" and argv[-2:] == [NODE, "qm list"]


def test_without_the_token_tofu_does_not_start(tmp_path, recorded, monkeypatch):
    require("ssh-keygen")
    monkeypatch.setattr("worms_cli.secrets.shutil.which",
                        lambda name, real=shutil.which: None if name == "ansible-vault" else real(name))  # fmt: skip
    with pytest.raises(build.BuildError, match="worms secrets set proxmox-token"):
        runner(tmp_path).tofu("platform", "apply")
    assert not recorded.calls


def test_on_the_node_without_a_vault_the_environment_is_used(tmp_path, recorded, monkeypatch):
    """The round agent: its environment file has the tofu inputs and the key;
    no vault, nothing routed elsewhere."""
    monkeypatch.setenv("TF_VAR_proxmox_api_token", "from-the-agent-env")
    r = runner(tmp_path, ssh="")
    r.tofu("sandbox", "apply")
    r.ansible("SetupSandbox.yml")
    (tofu_argv, tofu_env), (ansible_argv, _) = recorded.calls
    assert tofu_argv[:2] == ["sh", "-c"] and tofu_env["TF_VAR_proxmox_api_token"] == "from-the-agent-env"
    assert "ansible-playbook SetupSandbox.yml" in ansible_argv[2] and "vault.yml" not in ansible_argv[2]
    assert tofu_env["SSH_AUTH_SOCK"] == OPERATOR_AGENT and "WORMS_SSH_AUTH_SOCK" not in tofu_env


# --- the inventory from a laptop ----------------------------------------------


def test_inventory_from_a_laptop(tmp_path):
    """The node over SSH (host key checked), every VM through it, the node's
    stacks read on the node."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sandbox = {"ansible_hosts": {"value": {"vm01": {"name": "sandbox-vm01", "management_ip": "10.4.0.11", "vm_id": 9001}}},
               "sandbox": {"value": {"hosts": {"vm01": {"addresses": ["10.64.0.11"]}}}}}  # fmt: skip
    (bin_dir / "ssh").write_text(
        "#!/bin/sh\n"
        f"case \"$*\" in *stacks/sandbox*) echo '{json.dumps(sandbox)}' ;; *) echo '{{}}' ;; esac\n"
    )
    (bin_dir / "ssh").chmod(0o755)
    out = tmp_path / "inventory.generated.yml"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "WORMS_SSH": NODE, "WORMS_SSH_AUTH_SOCK": WORMS_AGENT}
    subprocess.run([sys.executable, str(REPO / "scripts" / "generate_inventory.py"), "--repo-root", str(tmp_path),
                    "--platform-json", str(FIXTURES / "platform-outputs.json"), "--output", str(out), "--quiet"],
                   env=env, check=True, capture_output=True)  # fmt: skip
    children = yaml.safe_load(out.read_text())["all"]["children"]
    node = children["proxmox_host"]["hosts"]["proxmox.local"]
    assert node == {"ansible_host": "pve.example", "ansible_user": "root", "ansible_host_key_checking": True}
    vm = children["sandbox"]["hosts"]["sandbox-vm01"]
    # The VM through the node (your agent for that hop), with the WORMS key's
    # agent (ssh expands the variable; the path differs per run).
    assert vm["ansible_host"] == "10.4.0.11"
    assert vm["ansible_ssh_common_args"] == f"-o ProxyJump={NODE} -o IdentityAgent=${{WORMS_SSH_AUTH_SOCK}}"


def test_inventory_on_the_node(generated_inventory):
    children = yaml.safe_load(generated_inventory.read_text())["all"]["children"]
    assert children["proxmox_host"]["hosts"]["proxmox.local"] == {"ansible_host": "127.0.0.1", "ansible_connection": "local"}


# --- the round agent's workspace on the node -----------------------------------

ROLE = REPO / "ansible" / "roles" / "host" / "round_agent"


def test_workspace_sync_copies_code_never_secrets_or_the_controllers_state(tmp_path):
    """rsync with the role's options (what ansible.posix.synchronize runs)."""
    require("rsync")
    src, dest = tmp_path / "checkout", tmp_path / "workspace"
    files = {
        "src/manage.py": "cli", "scripts/round_agent/agent.py": "agent", "ansible/SetupDevbox.yml": "play",
        "opentofu/modules/x/main.tf": "module", "opentofu/stacks/sandbox/main.tf": "sandbox",
        "opentofu/stacks/sandbox/generated_vms.tf": "repo default", "opentofu/stacks/devbox/main.tf": "devbox",
        # never on the node:
        "ansible/group_vars/vault.yml": "SECRET", "ansible/.vault_pass": "SECRET", "ansible/pki/worms/ca.key": "SECRET",
        "ansible/pki/worms/ca.crt": "copied separately", "ansible/inventory.generated.yml": "controller's",
        "opentofu/stacks/platform/main.tf": "controller's", "opentofu/stacks/platform/terraform.tfstate": "controller's",
        "opentofu/stacks/infrastructure/persistent/main.tf": "controller's",
        "opentofu/stacks/private_internet/main.tf": "controller's", "projects/dashboard/manage.py": "not needed",
    }  # fmt: skip
    for name, content in files.items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_text(content)
    for ignore in (".gitignore", "ansible/.gitignore", "opentofu/.gitignore"):
        shutil.copy(REPO / ignore, src / ignore)
    node_owned = {"opentofu/stacks/sandbox/terraform.tfstate": "node state",
                  "opentofu/stacks/sandbox/generated_vms.tf": "the agent's definition",
                  "opentofu/stacks/devbox/.terraform/x": "providers", "src/stale.py": "removed upstream"}  # fmt: skip
    for name, content in node_owned.items():
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        (dest / name).write_text(content)

    opts = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())["round_agent_sync_rsync_opts"]
    # As ansible.posix.synchronize runs it: the options joined into one
    # command line and split again (an option must not contain a space).
    import shlex

    argv = shlex.split(" ".join(["rsync", "--archive", "--delete-after", *opts, f"{src}/", f"{dest}/"]))
    subprocess.run(argv, check=True, capture_output=True)

    present = {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()}
    for name in ("src/manage.py", "scripts/round_agent/agent.py", "ansible/SetupDevbox.yml", "opentofu/modules/x/main.tf",
                 "opentofu/stacks/sandbox/main.tf", "opentofu/stacks/devbox/main.tf"):  # fmt: skip
        assert name in present, name
    assert not any("SECRET" in (dest / name).read_text() for name in present)
    assert not [n for n in present if n.startswith(("opentofu/stacks/platform", "opentofu/stacks/infrastructure",
                                                     "opentofu/stacks/private_internet", "projects/"))]  # fmt: skip
    assert "ansible/inventory.generated.yml" not in present and "ansible/pki/worms/ca.crt" not in present
    assert (dest / "opentofu/stacks/sandbox/terraform.tfstate").read_text() == "node state"
    assert (dest / "opentofu/stacks/sandbox/generated_vms.tf").read_text() == "the agent's definition"
    assert (dest / "opentofu/stacks/devbox/.terraform/x").exists()
    assert "src/stale.py" not in present  # code removed upstream goes


def test_node_run_wrapper(tmp_path, render):
    """worms-node-run: the workspace directory, the agent's environment, the
    WORMS key in a private ssh-agent."""
    require("ssh-agent", "ssh-add", "ssh-keygen")
    workspace, env_file, key = tmp_path / "ws", tmp_path / "agent.env", tmp_path / "ssh" / "worms"
    (workspace / "opentofu" / "stacks" / "devbox").mkdir(parents=True)
    env_file.write_text("FROM_ENV_FILE=yes\n")
    key.parent.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "worms-test", "-f", str(key)], check=True)
    script = tmp_path / "worms-node-run"
    script.write_text(render("ansible/roles/host/round_agent", "worms-node-run.j2", round_agent_repo_root=str(workspace),
                             round_agent_env_file=str(env_file), round_agent_ssh_key=str(key)))  # fmt: skip
    proc = subprocess.run(["sh", str(script), "opentofu/stacks/devbox", 'echo "$FROM_ENV_FILE"; pwd; ssh-add -l'],
                          capture_output=True, text=True, env={"PATH": os.environ["PATH"]})  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.splitlines()
    assert out[0] == "yes" and out[1] == str(workspace / "opentofu" / "stacks" / "devbox") and "worms-test" in out[2]


def test_role_keeps_proxmoxs_authorized_keys_symlink():
    """On Proxmox /root/.ssh/authorized_keys links into /etc/pve: replacing
    it with a file would break the cluster's own SSH."""
    tasks = yaml.safe_load((ROLE / "tasks" / "workspace.yml").read_text())
    authorized = [t for t in tasks if "ansible.posix.authorized_key" in t]
    assert authorized and all(t["ansible.posix.authorized_key"]["follow"] is True for t in authorized)
    env = (ROLE / "templates" / "round-agent.env.j2").read_text()
    assert "TF_VAR_ssh_public_key={{ round_agent_ssh_key }}.pub" in env
