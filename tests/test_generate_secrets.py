"""scripts/generate_secrets.py: the external tokens it takes from the
environment (docs/accounts.md, "Environment"), and that an existing vault is
completed, never rotated. Runs without ansible-vault (plaintext vault in a
temporary checkout), so it works everywhere."""

from __future__ import annotations

import os
import subprocess
import sys

from conftest import REPO

SCRIPT = REPO / "scripts" / "generate_secrets.py"


def run(root, **env):
    clean = {k: v for k, v in os.environ.items() if k not in ("PROXMOX_API_TOKEN", "PROXMOX_READONLY_API_TOKEN", "TF_VAR_proxmox_api_token")}
    # No ansible-vault on PATH: the vault stays plaintext, so it can be read.
    clean["PATH"] = str(root / "empty-path")
    (root / "empty-path").mkdir(exist_ok=True)
    proc = subprocess.run([sys.executable, str(SCRIPT), "--repo-root", str(root)], env={**clean, **env},
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc, (root / "ansible" / "group_vars" / "vault.yml").read_text()


def values(vault):
    out = {}
    for line in vault.splitlines():
        if line and not line.startswith("#") and ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip().strip('"')
    return out


def test_fresh_vault_takes_the_external_tokens(tmp_path):
    _, vault = run(tmp_path, PROXMOX_API_TOKEN="orch-secret", PROXMOX_READONLY_API_TOKEN="ro-secret",
                   TF_VAR_proxmox_api_token="root@pam!worms=prov-secret")
    v = values(vault)
    assert v["vault_proxmox_api_token_value"] == "orch-secret"
    assert v["vault_proxmox_readonly_api_token_value"] == "ro-secret"
    assert v["vault_tofu_proxmox_api_token"] == "root@pam!worms=prov-secret"
    assert len(v["vault_postgres_admin_password"]) >= 24
    assert oct((tmp_path / "ansible" / "group_vars" / "vault.yml").stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "ansible" / ".vault_pass").stat().st_mode & 0o777) == "0o600"


def test_missing_tokens_leave_placeholders(tmp_path):
    proc, vault = run(tmp_path)
    v = values(vault)
    assert v["vault_proxmox_api_token_value"] == "REPLACE_WITH_PROXMOX_API_TOKEN"
    assert v["vault_tofu_proxmox_api_token"] == ""
    assert "PROXMOX_API_TOKEN" in proc.stderr


def test_existing_vault_is_completed_not_rotated(tmp_path):
    _, first = run(tmp_path)
    # An older vault: without the newer keys.
    vault_file = tmp_path / "ansible" / "group_vars" / "vault.yml"
    old = "\n".join(line for line in first.splitlines()
                    if not line.startswith(("vault_tofu_proxmox_api_token", "vault_proxmox_readonly_api_token_value",
                                            "vault_orchestrator_dashboard_token")))
    vault_file.write_text(old + "\n")
    _, second = run(tmp_path, TF_VAR_proxmox_api_token="root@pam!worms=later")
    before, after = values(first), values(second)
    assert after["vault_postgres_admin_password"] == before["vault_postgres_admin_password"]  # not rotated
    assert after["vault_tofu_proxmox_api_token"] == "root@pam!worms=later"  # from the environment
    assert after["vault_proxmox_readonly_api_token_value"] == ""  # added, empty: edit later
    assert after["vault_orchestrator_dashboard_token"]  # a newer generated key
    proc, third = run(tmp_path, TF_VAR_proxmox_api_token="something-else")
    assert third == second and "already complete" in proc.stdout


# --- the secrets step of the pipeline (before/after guard for refactorings) --


EXTERNAL = {"vault_proxmox_api_token_value", "vault_proxmox_readonly_api_token_value", "vault_tofu_proxmox_api_token"}


def test_build_secrets_provides_every_vault_variable(tmp_path):
    """`worms build secrets` in a fresh checkout yields every vault_* variable
    the Ansible side refers to (the Proxmox tokens are entered by the operator,
    so they may be absent or empty), and a second run changes nothing."""
    import re
    import shutil

    import yaml

    root = tmp_path / "checkout"
    for part in ("src", "scripts", "ansible"):
        shutil.copytree(REPO / part, root / part, ignore=shutil.ignore_patterns(
            "vault.yml", ".vault_pass", "inventory.generated.yml", "pki", "__pycache__", ".venv*"))  # fmt: skip
    bin_dir = tmp_path / "bin"  # sh and python3 only: no ansible-vault, the vault stays readable
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    (bin_dir / "sh").symlink_to(shutil.which("sh"))
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path / "home"), "WORMS_REPO_ROOT": str(root), "NO_COLOR": "1"}

    def build_secrets():
        proc = subprocess.run([sys.executable, str(root / "src" / "manage.py"), "build", "secrets"], env=env,
                              capture_output=True, text=True)  # fmt: skip
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return (root / "ansible" / "group_vars" / "vault.yml").read_text()

    first = build_secrets()
    vault = yaml.safe_load(first)
    referenced = set()
    for path in (root / "ansible").rglob("*"):
        if path.suffix in (".yml", ".j2") and path.name != "vault.yml":
            referenced |= set(re.findall(r"\bvault_[a-z_]+", path.read_text()))
    assert referenced, "no vault_* variable found (the scan is broken)"
    for key in sorted(referenced - EXTERNAL):
        assert len(str(vault.get(key) or "")) >= 16, f"{key} is not generated"
    assert build_secrets() == first  # never rotated
