"""The dashboard's development ("devbox") view and its actions.

In the development environment (the dev/prod toggle in the nav), the home page
shows the team's devbox instead of the leaderboard: its VMs, how to connect and
the reset button. A reset rebuilds the devbox and clears the score by bumping the
team's devbox version (docs/04-dev-environment.md). The profile has the dev score, the reset
button too and the SSH keys (the last two are handled here), and the progression
page has the logs.

Production view = the leaderboard (dashboard/views/leaderboard.py). This module
only runs when request.is_dev_environment is set.
"""

from __future__ import annotations

from core import events
from core.ssh_keys import MAX_KEYS_PER_TEAM as MAX_KEYS
from core.models.devbox import Devbox, DevboxCapture, DevboxSSHKey, DevboxVM
from core.models.worms import Upload
from core.membership import membership_change
from core.models.team import Team, TeamMembership
from dashboard.views.decorators import user_in_team_required
from dashboard.views.leaderboard import leaderboard
from dashboard.views.navigation import safe_next
from dashboard.views.util import render_with_base_info
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import HttpRequest
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from orchestrator.rounds.devbox_runs import ACTIVE


def captured_vm_ids(team: Team) -> set[int]:
    """The devbox VMs whose token the team has submitted at its current version."""
    captures = DevboxCapture.objects.filter(team=team, devbox_version=team.devbox_version)
    return set(captures.values_list("devbox_vm_id", flat=True))


def active_upload(team: Team) -> Upload | None:
    """The team's dev upload under way, from waiting for its reset to the end of its hour.
    Only the newest can be (orchestrator/rounds/devbox_runs.py): a newer upload or a reset
    replaces it. One the orchestrator has not picked up yet is about to reset the devbox."""
    upload = Upload.objects.filter(team=team, devbox=True).select_related("devbox_run").order_by("-created_at", "-id").first()
    run = getattr(upload, "devbox_run", None)
    return upload if upload is not None and (run is None or run.state in ACTIVE) else None


def home(request: HttpRequest):
    """Index dispatcher: the devbox in the development view, else the
    leaderboard (production)."""
    if request.is_dev_environment:
        return devbox_home(request)
    return leaderboard(request)


def _hops(vms) -> list[dict]:
    """How to go on from A: to B, then from B to C. A VM's first address is on its link towards
    A. -A passes the team's agent on to B for the next hop; C is the last, so it needs none."""
    by_role = {vm.role: vm for vm in vms}
    hops = []
    for here, role, options in (("A", "b", "-A "), ("B", "c", "")):
        vm = by_role.get(role)
        if vm is not None and vm.addresses:
            hops.append({"where": f"On {here}, go on to {role.upper()}", "command": f"ssh {options}root@{vm.addresses[0]}"})
    return hops


def _chain(vms) -> list[dict]:
    """The VMs in order for the page's picture, each with the interface it is reached on and the
    one that leads on. A VM's interfaces come in a fixed order: one per link, then the SSH
    fabric (opentofu/stacks/devbox/main.tf)."""
    by_role = {vm.role: vm for vm in vms}
    chain = []
    for role in ("a", "b", "c"):
        vm = by_role.get(role)
        if vm is None:
            continue
        links = [{"iface": f"eth{n}", "ip": ip} for n, ip in enumerate(vm.addresses)]
        if role == "a":
            entry = {"iface": f"eth{len(links)}" if links else "", "ip": vm.ssh_fabric_ip} if vm.ssh_fabric_ip else None
            onward = links[0] if links else None
        else:
            entry = links[0] if links else None
            onward = links[1] if len(links) > 1 else None
        chain.append({"vm": vm, "letter": role.upper(), "entry": entry, "onward": onward})
    return chain


@login_required
@user_in_team_required
def devbox_home(request: HttpRequest, team: Team):
    devbox = Devbox.objects.filter(team=team).prefetch_related("vms").first()
    version = team.devbox_version

    vms = list(devbox.vms.order_by("role", "vm_identifier")) if devbox else []
    captured = captured_vm_ids(team)
    for vm in vms:
        vm.found = vm.id in captured

    # The "main" devbox VM (A) is the entry point teams SSH into (through the
    # gateway, to its SSH-fabric address). Fall back to the first VM.
    main_vm = next((vm for vm in vms if vm.role == "a"), vms[0] if vms else None)
    ssh_command = None
    if devbox and main_vm and devbox.ssh_gateway_host and main_vm.ssh_fabric_ip:
        port = "" if devbox.ssh_gateway_port == 22 else f":{devbox.ssh_gateway_port}"
        ssh_command = f"ssh -A -J {devbox.ssh_gateway_user}@{devbox.ssh_gateway_host}{port} root@{main_vm.ssh_fabric_ip}"

    context = {
        "devbox": devbox,
        "version": version,
        "vms": vms,
        "main_vm": main_vm,
        "ssh_command": ssh_command,
        "hops": _hops(vms),
        "chain": _chain(vms),
        "running": active_upload(team) if devbox else None,
    }
    return render_with_base_info(request, "devbox.html", context)


@login_required
@require_POST
@user_in_team_required
def devbox_reset(request: HttpRequest, team: Team):
    """Reset the team's devbox: rebuild its VMs and clear the score by bumping
    the team's devbox version (docs/04-dev-environment.md). The reset goes through the
    orchestrator (the dashboard role may not bump the version); in DEBUG
    (sqlite, no orchestrator) it bumps locally so the flow is usable. A failure
    is reported to the user instead of being shown as success. It returns to the page
    the button was on (the dev dashboard or the profile)."""
    from dashboard import orchestrator_client

    back = safe_next(request, "profile")

    # You cannot reset a devbox you do not have (it is created with the team).
    if not Devbox.objects.filter(team=team).exists():
        messages.error(request, "You have no devbox to reset yet — it is created together with your team.")
        return back

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
            return back
        messages.error(
            request,
            f"Could not reset your devbox: {error}. Please try again; if it keeps "
            "failing, contact an organiser.",
        )
        return back

    if result.get("rebuild_enqueued", True):
        events.team("devbox_reset", request=request, team=team)
        messages.success(request, "Reset requested: your devbox VMs are being rebuilt and the score starts over.")
    else:
        messages.info(request, "A rebuild of your devbox is already in progress.")
    return back


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
    messages.success(request, f"SSH key {changed}. Your devbox and the SSH gateway are being updated now.")


@login_required
@require_POST
@user_in_team_required
def devbox_add_ssh_key(request: HttpRequest, team: Team):
    name = request.POST.get("name", "").strip()[:200]
    public_key = request.POST.get("public_key", "").strip()
    if not _valid_ssh_key(public_key):
        messages.error(request, "That is not a single-line OpenSSH public key (ssh-ed25519, ssh-rsa, ecdsa, sk-…), or it contains characters that are not allowed.")
        return redirect("profile")
    retry = False
    # Membership can change after middleware resolved request.team. The team
    # lock also makes the key-count limit atomic across its members.
    with membership_change(request.user, team):
        if not TeamMembership.objects.filter(user=request.user, team=team).exists():
            messages.error(request, "You are no longer a member of this team.")
            return redirect("profile")
        fingerprint = DevboxSSHKey.fingerprint(public_key)
        existing = DevboxSSHKey.objects.filter(key_fingerprint=fingerprint).first()
        if existing is not None:
            if existing.team_id != team.id:
                messages.error(request, "That public key is already registered by another team. Use a different key.")
                return redirect("profile")
            messages.info(request, "Your team already has that key.")
            retry = True
        else:
            if DevboxSSHKey.objects.filter(team=team).count() >= MAX_KEYS:
                messages.error(request, f"Your team already has {MAX_KEYS} SSH keys; remove an unused key first.")
                return redirect("profile")
            try:
                with transaction.atomic():
                    DevboxSSHKey.objects.create(team=team, name=name, public_key=public_key, created_by=request.user)
                    events.credential("ssh_key_added", request=request, team=team, name=name, public_key=public_key)
            except IntegrityError:
                messages.error(request, "That public key is already registered. Use a different key.")
                return redirect("profile")
    # Network calls happen after releasing membership and team locks.
    _reconfigure_devbox(request, team, "pushed again" if retry else "added")
    return redirect("profile")


@login_required
@require_POST
@user_in_team_required
def devbox_delete_ssh_key(request: HttpRequest, team: Team, key_uuid):
    key = DevboxSSHKey.objects.filter(uuid=key_uuid, team=team).first()
    deleted, _ = DevboxSSHKey.objects.filter(uuid=key_uuid, team=team).delete()
    if deleted:
        events.credential("ssh_key_removed", request=request, team=team, name=key.name, public_key=key.public_key)
        _reconfigure_devbox(request, team, "removed")
    return redirect("profile")


def _valid_ssh_key(value: str) -> bool:
    """One OpenSSH public key line we accept (core/ssh_keys.py: also the console's rules)."""
    from core import ssh_keys

    return ssh_keys.valid(value)
