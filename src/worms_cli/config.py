"""Configuration for the `worms` CLI.

Everything is read from the environment (optionally seeded from a config file),
so the tool can run from your laptop with the same access tokens you would use
on the server. Nothing is hardcoded and no secret is written anywhere.

Precedence: process environment  >  --config file  >  ~/.worms/config  >  defaults.
A config file is simple `KEY=value` lines (no quoting, `#` comments).

Keys (all optional; a feature that needs one it does not have says so):

  WORMS_REPO_ROOT     the worms checkout (default: the repo this script is in)
  WORMS_SSH           user@host to run `make`/host commands on (e.g. root@worms-pve);
                      empty = run locally (you are on the Proxmox host)
  WORMS_DB_HOST/PORT/NAME/USER/PASSWORD/SSLMODE/SSLROOTCERT
                      database connection for status/teams/leaderboard (a
                      privileged role: admin or migrator)
  WORMS_PROXMOX_URL/USER/TOKEN_NAME/TOKEN_VALUE/NODE/VERIFY_SSL
                      Proxmox API for infrastructure status (VMs, bridges, firewall)
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
        """Environment for the Django bootstrap (manage settings). Returns {}
        if no database is configured."""
        host = self.get("WORMS_DB_HOST")
        if not host:
            return {}
        return {
            "WORMS_DB_HOST": host,
            "WORMS_DB_PORT": self.get("WORMS_DB_PORT", "5432"),
            "WORMS_DB_NAME": self.get("WORMS_DB_NAME", "mydb"),
            "WORMS_DB_USER": self.get("WORMS_DB_USER", "admin"),
            "WORMS_DB_PASSWORD": self.get("WORMS_DB_PASSWORD", ""),
            "WORMS_DB_SSLMODE": self.get("WORMS_DB_SSLMODE", "verify-full"),
            "WORMS_DB_SSLROOTCERT": self.get(
                "WORMS_DB_SSLROOTCERT", "/etc/ssl/certs/ca-certificates.crt"
            ),
        }

    def has_db(self) -> bool:
        return bool(self.get("WORMS_DB_HOST"))

    def proxmox(self) -> dict | None:
        url = self.get("WORMS_PROXMOX_URL")
        if not url:
            return None
        return {
            "url": url,
            "user": self.get("WORMS_PROXMOX_USER", "root@pam"),
            "token_name": self.get("WORMS_PROXMOX_TOKEN_NAME", ""),
            "token_value": self.get("WORMS_PROXMOX_TOKEN_VALUE", ""),
            "node": self.get("WORMS_PROXMOX_NODE", "worms-pve"),
            "verify_ssl": self.get("WORMS_PROXMOX_VERIFY_SSL", "1") == "1",
        }
