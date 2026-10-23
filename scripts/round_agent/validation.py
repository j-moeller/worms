"""Validation of host-job parameters.

Everything the round agent reads from `host_jobs` was written by the
orchestrator, which is a lower-privilege VM. The agent treats it as hostile
input: every value is checked here before it is used, and nothing from the
database is ever passed to a shell. Names and identifiers must match tight
patterns; numbers must sit in fixed ranges. A bad job is failed, not guessed.
"""

from __future__ import annotations

import re

# A sandbox VM name as scripts/generate_sandbox.py produces it (dns_name()):
# lowercase letters, digits and single hyphens.
VM_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# A sha256 hex digest (artifact content hash, flag hash).
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# default_storage key of an uploaded artifact (dashboard/views/uploads.py):
# malware-artifacts/<2>/<2>/<hash>.zip, no traversal.
STORAGE_KEY = re.compile(r"^malware-artifacts/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.zip$")


class Invalid(ValueError):
    """A job parameter that cannot be trusted; the job is failed."""


def integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int):
        raise Invalid(f"{name} must be an integer")
    if not low <= value <= high:
        raise Invalid(f"{name} must be in [{low}, {high}]")
    return value


def vm_name(value, field="vm"):
    if not isinstance(value, str) or not VM_NAME.match(value):
        raise Invalid(f"{field} is not a valid sandbox VM name: {value!r}")
    return value


def vm_names(value, field="vms", allow_empty=False):
    if not isinstance(value, list) or (not value and not allow_empty):
        raise Invalid(f"{field} must be a non-empty list")
    if len(value) > 256:
        raise Invalid(f"{field} has too many entries")
    return [vm_name(v, field) for v in value]


def sha256(value, field="hash"):
    if not isinstance(value, str) or not SHA256.match(value):
        raise Invalid(f"{field} is not a sha256 digest")
    return value


def storage_key(value):
    if not isinstance(value, str) or not STORAGE_KEY.match(value):
        raise Invalid(f"storage_key is not a valid artifact key: {value!r}")
    return value


def sandbox_params(value):
    """The sandbox generator parameters. Only known keys, all bounded; the
    game_plan sub-dict is the conductor's business and is not used here."""
    value = value or {}
    if not isinstance(value, dict):
        raise Invalid("sandbox_params must be an object")
    return {
        "networks": integer(value.get("networks", 4), "networks", 1, 30),
        "vms": integer(value.get("vms", 8), "vms", 1, 250),
        "sites": integer(value.get("sites", 1), "sites", 1, 8),
        "max_interfaces": integer(value.get("max_interfaces", 3), "max_interfaces", 1, 8),
    }


def prepare(params):
    return {
        "seed": integer(params.get("seed", 0), "seed", 0, 2**63 - 1),
        "sandbox_params": sandbox_params(params.get("sandbox_params")),
    }


def deploy(params):
    out = {
        "start_vm": vm_name(params.get("start_vm"), "start_vm"),
        "worm_delay": integer(params.get("worm_delay", 300), "worm_delay", 0, 86400),
        "initial_secrets": vm_names(params.get("initial_secrets", []), "initial_secrets", allow_empty=True),
    }
    artifact = params.get("artifact") or {}
    if not isinstance(artifact, dict):
        raise Invalid("artifact must be an object")
    out["artifact"] = {
        "storage_key": storage_key(artifact.get("storage_key")),
        "content_hash": sha256(artifact.get("content_hash"), "artifact.content_hash"),
    }
    return out


def release_secrets(params):
    return {"vms": vm_names(params.get("vms", []))}


# Job kind -> validator. CHECK and TEARDOWN take no parameters.
VALIDATORS = {
    "prepare": prepare,
    "check": lambda params: {},
    "deploy": deploy,
    "release_secrets": release_secrets,
    "teardown": lambda params: {},
}


def validate(kind, params):
    if kind not in VALIDATORS:
        raise Invalid(f"unknown job kind: {kind!r}")
    if not isinstance(params, dict):
        raise Invalid("params must be an object")
    return VALIDATORS[kind](params)
