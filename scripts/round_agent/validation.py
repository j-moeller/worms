"""Validation of host-job parameters.

Everything the round agent reads from `host_jobs` was written by the
orchestrator, which is a lower-privilege VM. The agent treats it as hostile
input: every value is checked here before it is used, and nothing from the
database is ever passed to a shell. Names and identifiers must match tight
patterns; numbers must sit in fixed ranges. A bad job is failed, not guessed.
"""

from __future__ import annotations

import re

# A sandbox VM name as a gameplan makes it (sandbox-<attribute>, src/gameplan/definition.py):
# lowercase letters, digits and hyphens.
VM_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# A sha256 hex digest (artifact content hash, flag hash).
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# default_storage key of an uploaded artifact (dashboard/views/uploads.py):
# malware-artifacts/<2>/<2>/<hash>.zip, no traversal.
STORAGE_KEY = re.compile(r"^malware-artifacts/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.zip$")
# A team uuid (the running team, recorded by scoring/logging).
UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class Invalid(ValueError):
    """A job parameter that cannot be trusted; the job is failed."""


def integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int):
        raise Invalid(f"{name} must be an integer")
    if not low <= value <= high:
        raise Invalid(f"{name} must be in [{low}, {high}]")
    return value


def vm_name(value, field="vm"):
    if not isinstance(value, str) or not VM_NAME.fullmatch(value):
        raise Invalid(f"{field} is not a valid sandbox VM name: {value!r}")
    return value


def vm_names(value, field="vms", allow_empty=False):
    if not isinstance(value, list) or (not value and not allow_empty):
        raise Invalid(f"{field} must be a non-empty list")
    if len(value) > 256:
        raise Invalid(f"{field} has too many entries")
    return [vm_name(v, field) for v in value]


def sha256(value, field="hash"):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise Invalid(f"{field} is not a sha256 digest")
    return value


def storage_key(value):
    if not isinstance(value, str) or not STORAGE_KEY.fullmatch(value):
        raise Invalid(f"storage_key is not a valid artifact key: {value!r}")
    return value


def gameplan_file(value):
    """A gameplan's file name: the loader takes it from the gameplans directory's own listing
    only (src/gameplan/loader.py), so no path is built from it."""
    from gameplan.loader import valid_name

    if not valid_name(value):
        raise Invalid(f"not a gameplan file name: {value!r}")
    return value


def prepare(params):
    return {
        "gameplan": gameplan_file(params.get("gameplan")),
        "gameplan_sha256": sha256(params.get("gameplan_sha256"), "gameplan_sha256"),
        "run_uuid": team_uuid(params.get("run_uuid"), "run_uuid"),
    }


def team_uuid(value, field="team_uuid"):
    if not isinstance(value, str) or not UUID.fullmatch(value):
        raise Invalid(f"{field} is not a uuid: {value!r}")
    return value


def deploy(params):
    out = {
        "start_vm": vm_name(params.get("start_vm"), "start_vm"),
        "team_uuid": team_uuid(params.get("team_uuid")),
        "worm_delay": integer(params.get("worm_delay", 15), "worm_delay", 5, 86400),
    }
    out["artifact"] = _artifact(params)
    return out


def _artifact(params):
    artifact = params.get("artifact") or {}
    if not isinstance(artifact, dict):
        raise Invalid("artifact must be an object")
    return {
        "storage_key": storage_key(artifact.get("storage_key")),
        "content_hash": sha256(artifact.get("content_hash"), "artifact.content_hash"),
    }


def _strict(params):
    # Anything but false is strict: a failure fails the job (the safe side).
    return params.get("strict", True) is not False


def flags(params):
    return {"vms": vm_names(params.get("vms", [])), "strict": _strict(params)}


def write_file(params):
    return {
        "run_uuid": team_uuid(params.get("run_uuid"), "run_uuid"),
        "file": integer(params.get("file"), "file", 0, 10_000),
        "strict": _strict(params),
    }


# --- devbox (docs/04-dev-environment.md) --------------------------------------------------

# A team slug as scripts/generate_devbox.py accepts it.
TEAM_SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
# One OpenSSH public key line: type + base64 blob + an optional comment. The
# comment is restricted to a safe charset -- NO `{ } % $ "` ' \ etc. -- because
# a registered key is rendered through Jinja on the Proxmox host (the devbox +
# gateway authorized_keys, run by the agent as root): a `{{...}}` in a comment
# would otherwise be evaluated (template injection). See docs/07-security.md.
SSH_KEY = re.compile(
    r"^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521)|sk-[a-z0-9@.-]+) "
    r"[A-Za-z0-9+/=]{20,}( [A-Za-z0-9._@/+:,-]{1,200})?$"
)


def team_slug(value):
    if not isinstance(value, str) or not TEAM_SLUG.fullmatch(value):
        raise Invalid(f"invalid team slug: {value!r}")
    return value


def ssh_keys(value):
    if not isinstance(value, list):
        raise Invalid("authorized_keys must be a list")
    if len(value) > 64:
        raise Invalid("too many authorized_keys")
    out = []
    for key in value:
        if not isinstance(key, str) or "\n" in key or len(key) > 8192 or not SSH_KEY.fullmatch(key.strip()):
            raise Invalid(f"invalid SSH public key: {key!r:.60}")
        out.append(key.strip())
    return out


def devbox_team(value):
    if not isinstance(value, dict):
        raise Invalid("team must be an object")
    return {
        "slug": team_slug(value.get("slug")),
        "index": integer(value.get("index", 0), "index", 0, 15),
        "team_uuid": team_uuid(value.get("team_uuid")),
        "authorized_keys": ssh_keys(value.get("authorized_keys", [])),
    }


def _key_blob(key: str) -> str:
    # The key without its comment: type + base64 blob. This is the identity an
    # sshd uses to match an authorized_keys line, so it must map to one team.
    import base64

    key_type, encoded = key.split()[:2]
    try:
        canonical = base64.b64encode(base64.b64decode(encoded, validate=True)).decode("ascii")
    except (ValueError, TypeError) as exc:
        raise Invalid("invalid SSH public key encoding") from exc
    return f"{key_type} {canonical}"


def devbox_teams(value):
    if not isinstance(value, list) or not value:
        raise Invalid("teams must be a non-empty list")
    if len(value) > 16:  # the link networks 10.128.0.0/19 (scripts/generate_devbox.py)
        raise Invalid("too many devbox teams (max 16)")
    teams = [devbox_team(t) for t in value]
    if len({t["slug"] for t in teams}) != len(teams):
        raise Invalid("duplicate team slug")
    if len({t["index"] for t in teams}) != len(teams):
        raise Invalid("duplicate team index")
    # The same public key must not be registered by two teams: sshd matches a
    # key to the first authorized_keys line, so a shared key would let one team
    # inherit another's gateway forward (and log in to its VMs). Fail closed.
    owner: dict[str, str] = {}
    for t in teams:
        for key in t["authorized_keys"]:
            blob = _key_blob(key)
            if owner.setdefault(blob, t["slug"]) != t["slug"]:
                raise Invalid(f"SSH key registered by more than one team (blob {blob[:28]}...)")
    return teams


def devbox_provision(params):
    return {"teams": devbox_teams(params.get("teams"))}


def devbox_rebuild(params):
    fresh = params.get("fresh", False)
    if not isinstance(fresh, bool):
        raise Invalid("fresh must be true or false")
    return {
        "team_uuid": team_uuid(params.get("team_uuid")),
        "devbox_version": integer(params.get("devbox_version", 1), "devbox_version", 1, 2**31 - 1),
        "teams": devbox_teams(params.get("teams")),
        "fresh": fresh,
    }


def devbox_reconfigure(params):
    # team_uuid is informational (which team triggered it); the keys for the
    # whole competition are re-pushed from `teams`.
    return {
        "team_uuid": team_uuid(params.get("team_uuid")),
        "teams": devbox_teams(params.get("teams")),
    }


def devbox_deploy(params):
    out = {
        "team_uuid": team_uuid(params.get("team_uuid")),
        "team_slug": team_slug(params.get("team_slug")),
        "start_vm": vm_name(params.get("start_vm"), "start_vm"),
        "worm_delay": integer(params.get("worm_delay", 10), "worm_delay", 0, 86400),
        "run_duration": integer(params.get("run_duration", 3600), "run_duration", 1, 7 * 86400),
        "artifact": _artifact(params),
    }
    # Only a team's own entry VM, whatever the database says.
    if out["start_vm"] != f"devbox-{out['team_slug']}-a":
        raise Invalid(f"start_vm is not the team's VM A: {out['start_vm']!r}")
    return out


def teardown(params):
    # Anything but true destroys the sandbox: the safe side.
    out = {"keep": params.get("keep", False) is True, "run_uuid": None}
    if params.get("run_uuid") is not None:
        out["run_uuid"] = team_uuid(params.get("run_uuid"), "run_uuid")
    return out


# Job kind -> validator. CHECK takes no parameters.
VALIDATORS = {
    "gameplans": lambda params: {},
    "prepare": prepare,
    "check": lambda params: {},
    "deploy": deploy,
    "remove_flags": flags,
    "place_flags": flags,
    "write_file": write_file,
    "teardown": teardown,
    "devbox_provision": devbox_provision,
    "devbox_rebuild": devbox_rebuild,
    "devbox_reconfigure": devbox_reconfigure,
    "devbox_deploy": devbox_deploy,
}


def validate(kind, params):
    if kind not in VALIDATORS:
        raise Invalid(f"unknown job kind: {kind!r}")
    if not isinstance(params, dict):
        raise Invalid("params must be an object")
    return VALIDATORS[kind](params)
