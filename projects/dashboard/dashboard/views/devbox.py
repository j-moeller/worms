"""The dashboard's development ("devbox") view and its actions.

In the development environment (the dev/prod toggle in the nav), the home page
shows the team's devbox instead of the leaderboard: the current dev score
(tokens found in the devbox VMs), the VMs, the devbox logs, the SSH keys the
team has registered, and how to connect. A reset rebuilds the devbox and clears
the score by bumping the team's devbox version (docs/devbox.md).

Production view = the leaderboard (dashboard/views/leaderboard.py). This module
only runs when request.is_dev_environment is set.
"""

from __future__ import annotations

import json
import re

from core.models.devbox import Devbox, DevboxCapture, DevboxLog, DevboxSSHKey, DevboxVM
from core.models.team import Team
from dashboard.views.decorators import user_in_team_required
from dashboard.views.leaderboard import leaderboard
from dashboard.views.util import render_with_base_info
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F
from django.http import HttpRequest
from django.shortcuts import redirect
from django.views.decorators.http import require_POST


def pretty(log_data) -> str:
    """The worm's log entry as indented JSON text. The template escapes it:
    it is attacker-controlled (written by the worm via the logging API)."""
    return json.dumps(log_data, indent=2, ensure_ascii=False)


def home(request: HttpRequest):
    """Index dispatcher: the devbox in the development view, else the
    leaderboard (production)."""
    if request.session.get("is_dev_environment"):
        return devbox_home(request)
    return leaderboard(request)


@login_required
@user_in_team_required
def devbox_home(request: HttpRequest, team: Team):
    devbox = Devbox.objects.filter(team=team).prefetch_related("vms").first()
    version = team.devbox_version

    vms = list(devbox.vms.order_by("role", "vm_identifier")) if devbox else []
    captured_vm_ids = set(
        DevboxCapture.objects.filter(team=team, devbox_version=version).values_list("devbox_vm_id", flat=True)
    )
    for vm in vms:
        vm.found = vm.id in captured_vm_ids

    # The "main" devbox VM (A) is the entry point teams SSH into (through the
    # gateway, to its SSH-fabric address). Fall back to the first VM.
    main_vm = next((vm for vm in vms if vm.role == "a"), vms[0] if vms else None)
    ssh_command = None
    if devbox and main_vm and devbox.ssh_gateway_host and main_vm.ssh_fabric_ip:
        ssh_command = (
            f"ssh -J {devbox.ssh_gateway_user}@{devbox.ssh_gateway_host}"
            f":{devbox.ssh_gateway_port} root@{main_vm.ssh_fabric_ip}"
        )

    logs = [
        {"entry": entry, "data": pretty(entry.logging_attempt.log_data)}
        for entry in DevboxLog.objects.filter(team=team, devbox_version=version)
        .select_related("logging_attempt")
        .order_by("-created_at")[:200]
    ]
    ssh_keys = list(DevboxSSHKey.objects.filter(team=team).order_by("-created_at"))

    context = {
        "devbox": devbox,
        "version": version,
        "vms": vms,
        "main_vm": main_vm,
        "ssh_command": ssh_command,
        "score": len(captured_vm_ids),
        "total_tokens": len(vms),
        "logs": logs,
        "ssh_keys": ssh_keys,
    }
    return render_with_base_info(request, "devbox.html", context)


@login_required
@require_POST
@user_in_team_required
def devbox_reset(request: HttpRequest, team: Team):
    """Reset the team's devbox: rebuild its VMs and clear the score by bumping
    the team's devbox version (docs/devbox.md). The reset goes through the
    orchestrator (the dashboard role may not bump the version); in DEBUG
    (sqlite, no orchestrator) it bumps locally so the flow is usable. A failure
    is reported to the user instead of being shown as success."""
    from dashboard import orchestrator_client

    # You cannot reset a devbox you do not have (it is created with the team).
    if not Devbox.objects.filter(team=team).exists():
        messages.error(request, "You have no devbox to reset yet — it is created together with your team.")
        return redirect("index")

    try:
        result = orchestrator_client.post(f"/devbox/{team.uuid}/reset/")
    except orchestrator_client.OrchestratorError as error:
        if settings.DEBUG:
            # Local development has no orchestrator: bump the version directly
            # so the flow is usable, and say plainly that nothing is rebuilt.
            with transaction.atomic():
                Team.objects.filter(pk=team.pk).update(devbox_version=F("devbox_version") + 1)
            messages.success(
                request,
                "Devbox version bumped (development mode: no orchestrator, so the "
                "VMs are not really rebuilt). The score starts over.",
            )
            return redirect("index")
        messages.error(
            request,
            f"Could not reset your devbox: {error}. Please try again; if it keeps "
            "failing, contact an organiser.",
        )
        return redirect("index")

    if result.get("rebuild_enqueued", True):
        messages.success(request, "Reset requested: your devbox VMs are being rebuilt and the score starts over.")
    else:
        messages.info(request, "A rebuild of your devbox is already in progress.")
    return redirect("index")


def _key_blob(public_key: str) -> str:
    # type + base64 blob, without the comment: the identity sshd matches on.
    return " ".join(public_key.split(" ")[:2])


def _reconfigure_devbox(request: HttpRequest, team: Team, changed: str):
    """Ask the orchestrator to re-push the team's keys to the devbox and the
    gateway (no VM rebuild). Reports honestly whether it took effect."""
    from dashboard import orchestrator_client

    if not Devbox.objects.filter(team=team).exists():
        messages.success(request, f"SSH key {changed}. It will be installed when your devbox is first provisioned.")
        return
    try:
        orchestrator_client.post(f"/devbox/{team.uuid}/reconfigure/")
    except orchestrator_client.OrchestratorError as error:
        if settings.DEBUG:
            messages.success(request, f"SSH key {changed} (development mode: no orchestrator, so the devbox is not re-keyed).")
        else:
            messages.warning(
                request,
                f"SSH key {changed}, but applying it to your devbox failed: {error}. It will be applied at the "
                "next reset or provision; retry or contact an organiser if it persists.",
            )
        return
    messages.success(request, f"SSH key {changed}. It is being installed on your devbox and the SSH gateway now.")


@login_required
@require_POST
@user_in_team_required
def devbox_add_ssh_key(request: HttpRequest, team: Team):
    name = request.POST.get("name", "").strip()[:200]
    public_key = request.POST.get("public_key", "").strip()
    if not _valid_ssh_key(public_key):
        messages.error(request, "That is not a single-line OpenSSH public key (ssh-ed25519, ssh-rsa, ecdsa, sk-…), or it contains characters that are not allowed.")
        return redirect("index")
    blob = _key_blob(public_key)
    # A key may belong to only one team: sshd matches a key to the first line,
    # so a shared key would let another team inherit this team's gateway
    # forward and log in to its devbox VMs (docs/security-assessment.md).
    if any(_key_blob(k) == blob for k in DevboxSSHKey.objects.exclude(team=team).values_list("public_key", flat=True)):
        messages.error(request, "That public key is already registered — by another team, or already by yours. Use a different key.")
        return redirect("index")
    if any(_key_blob(k) == blob for k in DevboxSSHKey.objects.filter(team=team).values_list("public_key", flat=True)):
        messages.info(request, "Your team already has that key.")
        return redirect("index")
    DevboxSSHKey.objects.create(team=team, name=name, public_key=public_key, created_by=request.user)
    _reconfigure_devbox(request, team, "added")
    return redirect("index")


@login_required
@require_POST
@user_in_team_required
def devbox_delete_ssh_key(request: HttpRequest, team: Team, key_id: int):
    deleted, _ = DevboxSSHKey.objects.filter(id=key_id, team=team).delete()
    if deleted:
        _reconfigure_devbox(request, team, "removed")
    return redirect("index")


# Accept only a single OpenSSH public key line of a known type, bounded length.
_SSH_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)


# A key comment is restricted to a safe charset. A registered key is rendered
# through Jinja on the Proxmox host (the devbox + gateway authorized_keys, run
# by the round agent as root), so a `{{...}}`/`{%...%}` in the comment would be
# evaluated there (template injection). The agent's validator enforces the same
# (scripts/round_agent/validation.py); this is the first gate. See
# docs/security-assessment.md.
_SSH_COMMENT = re.compile(r"^[A-Za-z0-9._@/+:,-]{1,200}$")


def _valid_ssh_key(value: str) -> bool:
    if not value or "\n" in value or len(value) > 8192:
        return False
    parts = value.split(" ")
    # type + base64 blob, then at most a one-token comment (no spaces).
    if len(parts) < 2 or len(parts) > 3 or parts[0] not in _SSH_TYPES:
        return False
    import base64

    try:
        base64.b64decode(parts[1], validate=True)
    except Exception:
        return False
    if len(parts) == 3 and not _SSH_COMMENT.match(parts[2]):
        return False
    return True
