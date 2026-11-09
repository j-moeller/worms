"""A team's devbox SSH keys: what is accepted, for the dashboard (dashboard/views/devbox.py) and
the console (`worms devbox-add-key`). A key is stored in the database; the orchestrator then has
the round agent push every team's keys to the devboxes and the SSH gateway
(orchestrator/rounds/devbox.py, request_reconfigure).

Accept only a single OpenSSH public key line of a known type, bounded length. A key comment is
restricted to a safe charset: a registered key is rendered through Jinja on the Proxmox host
(the devbox and gateway authorized_keys, by the round agent as root), so a `{{...}}`/`{%...%}`
in the comment would be evaluated there (template injection). The agent's validator enforces
the same (scripts/round_agent/validation.py); this is the first gate (docs/07-security.md, D6).
"""

from __future__ import annotations

import base64
import re

TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)
COMMENT = re.compile(r"^[A-Za-z0-9._@/+:,-]{1,200}$")
MAX_KEYS_PER_TEAM = 64


def valid(value: str) -> bool:
    if not value or "\n" in value or len(value) > 8192:
        return False
    parts = value.split(" ")
    # type + base64 blob, then at most a one-token comment (no spaces).
    if len(parts) < 2 or len(parts) > 3 or parts[0] not in TYPES or len(parts[1]) < 20:
        return False
    try:
        base64.b64decode(parts[1], validate=True)
    except Exception:  # noqa: BLE001  (binascii.Error, ValueError)
        return False
    if len(parts) == 3 and not COMMENT.match(parts[2]):
        return False
    return True
