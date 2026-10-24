"""The vault: the one place the platform's secrets are kept.

    ansible/group_vars/vault.yml   encrypted with ansible-vault (git-ignored)
    ansible/.vault_pass            its key (generated, 0600, git-ignored)

Three secrets are entered by the operator, once (`worms secrets set <name>`,
prompted without echo or read from stdin): the Proxmox tokens, which only
Proxmox can issue. Everything else -- database passwords, Django keys, the CA
passphrase, API tokens, the SSH key -- is generated the first time it is
missing and never rotated. Every consumer reads the vault: OpenTofu through the
CLI (TF_VAR_* in its environment only), Ansible (-e @group_vars/vault.yml), the
services and the round agent through Ansible, the CLI's own database and
Proxmox access. No value is ever printed.

Standard library only (plus ansible-vault and ssh-keygen on PATH).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

# name -> (vault key, what it is, what is missing without it). Kept as "" in
# the vault until entered, so Ansible never sees an undefined variable.
EXTERNAL = {
    "proxmox-token": (
        "vault_tofu_proxmox_api_token",
        "OpenTofu's provisioning token, root@pam!<id>=<secret> (full form)",
        "MISSING: OpenTofu cannot run",
    ),
    "orchestrator-token": (
        "vault_proxmox_api_token_value",
        "secret of orchestrator@pam!orchestrator-prov (sandbox VM power)",
        "MISSING: rounds cannot power sandbox VMs",
    ),
    "topology-token": (
        "vault_proxmox_readonly_api_token_value",
        "secret of topology@pve!readonly (topology view, `worms status`)",
        "not set (optional)",
    ),
}
# Generated when missing. key -> random bytes (URL-safe text, ~1.3 chars/byte).
GENERATED = {
    "vault_postgres_admin_password": 24,
    "vault_postgres_migrator_password": 24,
    "vault_django_dashboard_password": 24,
    "vault_django_orchestrator_password": 24,
    "vault_dbuser_logging_password": 24,
    "vault_dbuser_network_monitor_password": 24,
    "vault_dbuser_scoring_password": 24,
    "vault_round_agent_db_password": 24,
    "vault_pki_ca_passphrase": 24,
    "vault_dashboard_secret_key": 64,  # Django SECRET_KEY
    "vault_orchestrator_secret_key": 64,
    "vault_orchestrator_dashboard_token": 32,  # dashboard -> devbox reset
}
# The WORMS SSH key (generated; `worms secrets set ssh-key` imports one): the
# one key every VM authorizes, used by OpenTofu, Ansible, the CLI and the
# round agent (docs/accounts.md §4).
SSH_KEY = "vault_worms_ssh_key"
SSH_PUBLIC_KEY = "vault_worms_ssh_public_key"

PROXMOX_TOKEN = re.compile(r"^[^@\s]+@[^!\s]+![^=\s]+=\S+$")
HEADER = (
    "# The WORMS vault: every secret of the platform (docs/accounts.md §4).\n"
    "# Enter the Proxmox tokens with `worms secrets set <name>`; everything else\n"
    "# is generated when missing and never rotated. Do not edit by hand.\n"
)


class VaultError(RuntimeError):
    pass


def token(nbytes: int) -> str:
    # URL-safe: only [A-Za-z0-9_-], safe unquoted in env files and PostgreSQL.
    return secrets.token_urlsafe(nbytes)


def parse(text: str) -> dict:
    """The vault's `key: value` lines (values JSON-quoted, as render() writes
    them; plain or single-quoted values of hand-edited vaults also work)."""
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#") or line.strip() == "---":
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$", line)
        if not match:
            raise VaultError(f"vault line {number}: expected `key: value` (multi-line YAML is not supported)")
        key, raw = match.groups()
        if raw.startswith('"'):
            values[key] = json.loads(raw)
        elif raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
            values[key] = raw[1:-1].replace("''", "'")
        elif raw in ("|", ">", "|-", ">-"):
            raise VaultError(f"vault line {number}: block scalars are not supported")
        else:
            values[key] = raw
    return values


def render(values: dict) -> str:
    """Deterministic YAML: entered secrets, generated ones, then any other key
    (kept as they are). JSON strings are valid YAML double-quoted strings."""
    order = [key for key, _, _ in EXTERNAL.values()] + list(GENERATED) + [SSH_KEY, SSH_PUBLIC_KEY]
    keys = [k for k in order if k in values] + sorted(k for k in values if k not in order)
    return HEADER + "".join(f"{key}: {json.dumps(str(values[key]))}\n" for key in keys)


def generate_ssh_key() -> tuple[str, str]:
    """A new ed25519 key pair (private, public); the files never outlive this."""
    if not shutil.which("ssh-keygen"):
        raise VaultError("ssh-keygen is not installed (OpenSSH client)")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "worms"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "worms", "-f", str(path)],
                       check=True, capture_output=True)  # fmt: skip
        return path.read_text(), path.with_suffix(".pub").read_text().strip()


def public_key_of(private_key: str) -> str:
    """The public half of an OpenSSH private key (validates it, too)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "key"
        path.write_text(private_key if private_key.endswith("\n") else private_key + "\n")
        path.chmod(0o600)
        proc = subprocess.run(["ssh-keygen", "-y", "-f", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise VaultError("not an unencrypted OpenSSH private key")
    return proc.stdout.strip()


class Vault:
    def __init__(self, repo_root: Path):
        self.file = Path(repo_root) / "ansible" / "group_vars" / "vault.yml"
        self.key_file = Path(repo_root) / "ansible" / ".vault_pass"

    def exists(self) -> bool:
        return self.file.exists()

    def _ansible_vault(self, *args, input=None) -> str:
        proc = subprocess.run(["ansible-vault", *args, "--vault-password-file", str(self.key_file)],
                              input=input, capture_output=True, text=True)  # fmt: skip
        if proc.returncode != 0:
            raise VaultError(f"ansible-vault {args[0]} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def load(self) -> dict:
        if not self.exists():
            return {}
        text = self.file.read_text()
        if text.startswith("$ANSIBLE_VAULT"):
            if not shutil.which("ansible-vault"):
                raise VaultError(f"{self.file} is encrypted and ansible-vault is not installed")
            text = self._ansible_vault("view", str(self.file))
        return parse(text)

    def save(self, values: dict) -> None:
        """Write the vault, encrypted when ansible-vault is available (the
        plaintext never touches the disk then), else plaintext (0600)."""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        if not self.key_file.exists():
            self.key_file.write_text(token(32) + "\n")
            self.key_file.chmod(0o600)
        text = render(values)
        fd, tmp = tempfile.mkstemp(dir=self.file.parent, prefix=".vault-", suffix=".tmp")
        os.close(fd)
        try:
            os.chmod(tmp, 0o600)
            if shutil.which("ansible-vault"):
                self._ansible_vault("encrypt", "--output", tmp, input=text)
            else:
                Path(tmp).write_text(text)
            os.replace(tmp, self.file)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def ensure(self) -> list[str]:
        """Generate every missing generated secret (never rotate one); returns
        the keys added."""
        values = self.load()
        added = [key for key in GENERATED if not values.get(key)]
        for key in added:
            values[key] = token(GENERATED[key])
        for key, _, _ in EXTERNAL.values():
            if key not in values:
                values[key] = ""
                added.append(key)
        if not values.get(SSH_KEY):
            values[SSH_KEY], values[SSH_PUBLIC_KEY] = generate_ssh_key()
            added.append(SSH_KEY)
        elif not values.get(SSH_PUBLIC_KEY):
            values[SSH_PUBLIC_KEY] = public_key_of(values[SSH_KEY])
            added.append(SSH_PUBLIC_KEY)
        if added or not self.exists():
            self.save(values)
        return added

    def set(self, name: str, value: str) -> None:
        """Store an entered secret (EXTERNAL name) or import an SSH key."""
        value = value.strip()
        values = self.load()
        if name == "ssh-key":
            values[SSH_PUBLIC_KEY] = public_key_of(value)
            values[SSH_KEY] = value + "\n"
        elif name in EXTERNAL:
            if not value or any(c.isspace() for c in value):
                raise VaultError(f"{name}: empty or contains whitespace")
            if name == "proxmox-token" and not PROXMOX_TOKEN.match(value):
                raise VaultError("proxmox-token: expected the full form user@realm!tokenid=secret")
            values[EXTERNAL[name][0]] = value
        else:
            raise VaultError(f"unknown secret {name!r} (one of: {', '.join([*EXTERNAL, 'ssh-key'])})")
        self.save(values)

    def status(self) -> list[tuple[str, str, str]]:
        """(name, what, state) for every secret; never a value."""
        values = self.load()
        rows = [(name, what, "set" if values.get(key) else unset) for name, (key, what, unset) in EXTERNAL.items()]
        rows.append(("ssh-key", "the WORMS SSH key every VM authorizes (generated or imported)",
                     "set" if values.get(SSH_KEY) else "generated on first use"))  # fmt: skip
        missing = [key for key in GENERATED if not values.get(key)]
        rows.append(("(generated)", f"{len(GENERATED)} internal passwords, keys and tokens",
                     f"{len(GENERATED) - len(missing)}/{len(GENERATED)} present"))  # fmt: skip
        return rows

    def get(self, name: str) -> str:
        """An entered secret by name ("" if not set)."""
        return self.load().get(EXTERNAL[name][0], "")
