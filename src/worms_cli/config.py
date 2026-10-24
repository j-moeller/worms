"""Configuration for the `worms` CLI: where things are, never secrets (those are
in the vault, worms_cli/secrets.py).

Precedence: process environment  >  --config file  >  ~/.worms/config  >  defaults.
A config file is simple `KEY=value` lines (no quoting, `#` comments).

  WORMS_SSH           root@<node>: this machine (a laptop) is the controller and
                      reaches the Proxmox node over SSH; empty = this machine is
                      the node (worms_cli/session.py)
  WORMS_REPO_ROOT     the worms checkout (default: the repo this script is in)
  WORMS_PROXMOX_URL   the node's API, if not https://<WORMS_SSH host>:8006/
  NO_COLOR            set: no colours in the output

Overrides for development and tests (normally derived from the vault and the
platform stack; docs/accounts.md §4):
  WORMS_DB_HOST/PORT/NAME/USER/PASSWORD/SSLMODE/SSLROOTCERT
  WORMS_PROXMOX_USER/TOKEN_NAME/TOKEN_VALUE/NODE/VERIFY_SSL
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_FILE = Path(os.path.expanduser("~/.worms/config"))


def _repo_root_default() -> Path:
    # src/worms_cli/config.py -> repo root is two levels up from src/.
    return Path(__file__).resolve().parents[2]


def load_file(path: Path) -> dict:
    values: dict[str, str] = {}
    if not path or not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@dataclass
class Config:
    env: dict = field(default_factory=dict)

    @classmethod
    def load(cls, config_file: str | None = None) -> "Config":
        merged: dict[str, str] = {}
        merged.update(load_file(DEFAULT_CONFIG_FILE))
        if config_file:
            merged.update(load_file(Path(config_file)))
        # The real environment wins.
        for key, value in os.environ.items():
            if key.startswith("WORMS_"):
                merged[key] = value
        return cls(env=merged)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.env.get(key, default)

    # -- derived settings ----------------------------------------------------

    @property
    def repo_root(self) -> Path:
        return Path(self.get("WORMS_REPO_ROOT") or _repo_root_default())

    @property
    def ssh(self) -> str:
        return self.get("WORMS_SSH", "") or ""

    def db_env(self) -> dict:
        """Environment for the Django bootstrap (manage settings): explicit
        WORMS_DB_* settings, else the admin role from the vault at the
        database's address (platform stack), through an SSH tunnel via the
        node from a laptop (pg_hba admits the admin from the node only).
        Returns {} if neither is available."""
        host = self.get("WORMS_DB_HOST")
        if not host:
            return self._vault_db_env()
        return {
            "WORMS_DB_HOST": host,
            "WORMS_DB_PORT": self.get("WORMS_DB_PORT", "5432"),
            "WORMS_DB_NAME": self.get("WORMS_DB_NAME", "mydb"),
            "WORMS_DB_USER": self.get("WORMS_DB_USER", "admin"),
            "WORMS_DB_PASSWORD": self.get("WORMS_DB_PASSWORD", ""),
            "WORMS_DB_SSLMODE": self.get("WORMS_DB_SSLMODE", "verify-full"),
            "WORMS_DB_SSLROOTCERT": self.get("WORMS_DB_SSLROOTCERT", str(self.default_ca())),
        }

    def _vault_db_env(self) -> dict:
        from worms_cli.session import for_config, group_var

        session = for_config(self)
        if not session.has_vault():
            return {}
        db = session.infrastructure()["infrastructure_ips"]["database"]
        env = {
            "WORMS_DB_HOST": db,  # TLS checks the certificate against this name
            "WORMS_DB_PORT": "5432",
            "WORMS_DB_NAME": group_var(self.repo_root, "postgres_db", "mydb"),
            "WORMS_DB_USER": group_var(self.repo_root, "postgres_admin_name", "admin"),
            "WORMS_DB_PASSWORD": session.values()["vault_postgres_admin_password"],
            "WORMS_DB_SSLMODE": "verify-full",
            "WORMS_DB_SSLROOTCERT": str(self.default_ca()),
        }
        if session.laptop:
            env["WORMS_DB_HOSTADDR"] = "127.0.0.1"  # ... while connecting to the tunnel
            env["WORMS_DB_PORT"] = str(session.tunnel(db, 5432))
        return env

    def default_ca(self) -> Path:
        """The internal CA that signed the database's certificate
        (ansible/pki/worms/ca.crt, created by CreateCA.yml on the node); the
        system bundle if this checkout has none (e.g. on a laptop)."""
        ca = self.repo_root / "ansible" / "pki" / "worms" / "ca.crt"
        return ca if ca.exists() else Path("/etc/ssl/certs/ca-certificates.crt")

    def has_db(self) -> bool:
        from worms_cli.session import for_config

        return bool(self.get("WORMS_DB_HOST")) or for_config(self).has_vault()

    def proxmox(self) -> dict | None:
        """The read-only (topology) token for `worms status`: explicit
        WORMS_PROXMOX_* settings, else from the vault, to the node's API (from
        a laptop through an SSH tunnel, so PVE's certificate, issued for
        127.0.0.1, verifies). None if neither is available."""
        if self.get("WORMS_PROXMOX_TOKEN_VALUE"):
            return {
                "url": self.get("WORMS_PROXMOX_URL", "https://127.0.0.1:8006"),
                "user": self.get("WORMS_PROXMOX_USER", "root@pam"),
                "token_name": self.get("WORMS_PROXMOX_TOKEN_NAME", ""),
                "token_value": self.get("WORMS_PROXMOX_TOKEN_VALUE", ""),
                "node": self.get("WORMS_PROXMOX_NODE", "worms-pve"),
                "verify_ssl": self.get("WORMS_PROXMOX_VERIFY_SSL", "1") == "1",
            }
        from worms_cli.session import for_config, group_var

        session = for_config(self)
        if not session.has_vault() or not session.values().get("vault_proxmox_readonly_api_token_value"):
            return None
        port = session.tunnel("127.0.0.1", 8006) if session.laptop else 8006
        return {
            "url": f"https://127.0.0.1:{port}",
            "user": group_var(self.repo_root, "proxmox_readonly_api_user", "topology@pve"),
            "token_name": group_var(self.repo_root, "proxmox_readonly_api_token_name", "readonly"),
            "token_value": session.values()["vault_proxmox_readonly_api_token_value"],
            "node": self.get("WORMS_PROXMOX_NODE", "worms-pve"),
            "verify_ssl": session.pve_ca(),
        }
