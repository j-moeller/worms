"""Debug-only helpers (DEBUG=1): switch between users, and populate the
database with demo data so the otherwise-empty views have something to show.
Both are reachable from the debug console at the bottom of every page
(templates/base.html) and refuse with 404 when DEBUG is off."""

import hashlib
from datetime import timedelta

from core import clock
from core.models.user import User
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST


def _back(request: HttpRequest, default: str = "index"):
    """Redirect to the page the debug form was submitted from (same site only)."""
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect(default)


@login_required
@require_POST
def debug_login_as(request: HttpRequest, uuid):
    if not settings.DEBUG:
        return JsonResponse(
            {"error": "not available"},
            status=404,
        )

    user = get_object_or_404(User, uuid=uuid)

    login(request, user)

    next_url = request.POST.get("next")

    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)

    return redirect("profile")


@login_required
@require_POST
def populate(request: HttpRequest):
    """Fill the database with demo data (teams, users, a running round with
    scores, a devbox, access tokens and logs) so every view has something to
    show. Idempotent: it only creates what is missing, so it is safe to click
    more than once. Switch into any of the created users with the user switcher
    next to this button."""
    if not settings.DEBUG:
        return JsonResponse({"error": "not available"}, status=404)

    from core.models.competition import ApplicationSetting, Competition, Round
    from core.models.devbox import (
        Devbox,
        DevboxCapture,
        DevboxLog,
        DevboxSSHKey,
        DevboxVM,
    )
    from core.models.logging import LoggingAttempt
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from core.models.team import AccessTokens, Team, TeamMembership
    from core.models.vm import VirtualMachine

    now = clock.now()

    # A current competition. Reuse the one `manage.py init` made (giving it
    # dates so registration counts as closed and the team views open), or make
    # one if there is none.
    competition = ApplicationSetting.get_current_competition()
    if competition is None:
        competition = Competition.objects.create(
            name="WORMS 2026",
            registration_starts_at=now - timedelta(days=9),
            registration_ends_at=now - timedelta(days=2),
        )
        ApplicationSetting.objects.update_or_create(
            key="current", defaults={"current_competition": competition}
        )
    elif competition.registration_ends_at is None:
        competition.registration_starts_at = now - timedelta(days=9)
        competition.registration_ends_at = now - timedelta(days=2)
        competition.save(update_fields=["registration_starts_at", "registration_ends_at"])

    # A round in progress (status badge, current-round scoring).
    round, _ = Round.objects.get_or_create(
        competition=competition,
        name="Round 1",
        defaults={
            "seed": 1,
            "round_start": now - timedelta(hours=1),
            "round_end": now + timedelta(hours=2, minutes=37),
        },
    )

    # Teams, each with a lead and a few members (people to switch into).
    names = ["Red Team", "Neural Net", "0xCAFE", "Null Pointer", "Segfault Collective", "rm -rf"]
    teams = []
    for i, name in enumerate(names):
        team, _ = Team.objects.get_or_create(
            competition=competition,
            name=name,
            defaults={
                "max_members": 4,
                "devbox_version": 1,
                "development_competition": competition,
            },
        )
        teams.append(team)
        slug = name.split()[0].lower().lstrip("0x") or "team"
        lead, _ = User.objects.get_or_create(
            username=f"{slug}-lead", defaults={"first_name": name.split()[0]}
        )
        TeamMembership.objects.get_or_create(user=lead, team=team)
        for j in range(i % 3):
            member, _ = User.objects.get_or_create(username=f"player-{i}-{j}")
            TeamMembership.objects.get_or_create(user=member, team=team)

    red = teams[0]
    # Put the signed-in user into a team as well, so their own team views
    # (devbox, profile tokens, upload) are populated immediately -- but only if
    # they are not already in a team (get_users_team expects at most one).
    if TeamMembership.get_users_team(request.user, competition) is None:
        TeamMembership.objects.get_or_create(user=request.user, team=red)

    # A few sandbox VMs on the round, and flag captures so the leaderboard has
    # ranked points and the captures page has content.
    vms = []
    for n in range(3):
        vm, _ = VirtualMachine.objects.get_or_create(
            round=round,
            vm_identifier=f"sandbox-vm-{n}",
            defaults={
                "vm_maintenance_ip": f"10.64.0.{10 + n}",
                "proxmox_vm_id": 7000 + n,
                "addresses": [f"10.64.0.{10 + n}"],
                "networks": [],
                "ctf_flag_hash": hashlib.sha256(f"sandbox-vm-{n}".encode()).hexdigest(),
            },
        )
        vms.append(vm)
    if not FlagCapture.objects.filter(round=round).exists():
        for i, team in enumerate(teams):
            for vm in vms[: max(0, 3 - i)]:  # 3, 2, 1, 0, 0, 0 -> a ranking
                attempt = FlagCaptureAttempt.objects.create(
                    team_uuid=str(team.uuid),
                    vm_flag_hash=vm.ctf_flag_hash or "",
                    processed=True,
                    processed_at=now,
                )
                FlagCapture.objects.create(
                    team=team, round=round, vm=vm, flag_capture_attempt=attempt
                )

    # The first team's devbox (so the Dev view is populated).
    devbox, _ = Devbox.objects.get_or_create(
        team=red,
        defaults={
            "index": 3,
            "services_source_ip": "10.2.0.35",
            "ssh_gateway_host": "worms.example.edu",
            "ssh_gateway_port": 2222,
            "ssh_gateway_user": "team",
            "gateway_vm_id": 9003,
        },
    )
    for role, ip, fabric, found in [
        ("a", "10.128.3.10", "10.7.3.10", True),
        ("b", "10.128.3.11", "10.7.3.11", True),
        ("c", "10.128.3.12", "10.7.3.12", False),
    ]:
        devbox_vm, _ = DevboxVM.objects.get_or_create(
            devbox=devbox,
            vm_identifier=f"devbox-red-{role}",
            defaults={
                "role": role,
                "vm_maintenance_ip": ip,
                "ssh_fabric_ip": fabric,
                "proxmox_vm_id": 9000 + ord(role),
                "addresses": [ip],
                "networks": [],
                "ctf_flag_hash": hashlib.sha256(f"devbox-{role}".encode()).hexdigest(),
            },
        )
        if found:
            DevboxCapture.objects.get_or_create(
                team=red, devbox_vm=devbox_vm, devbox_version=red.devbox_version
            )
    DevboxSSHKey.objects.get_or_create(
        team=red,
        name="mara@laptop",
        defaults={
            "created_by": request.user,
            "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO1k2b9mara+laptop+demo+key",
        },
    )
    if not DevboxLog.objects.filter(team=red, devbox_version=red.devbox_version).exists():
        for i in range(3):
            attempt = LoggingAttempt.objects.create(
                team_uuid=str(red.uuid),
                devbox=True,
                log_data={"event": "probe", "vm": f"devbox-red-{'abc'[i]}", "open_ports": [22, 80, 8888]},
            )
            DevboxLog.objects.create(
                team=red, devbox_version=red.devbox_version, logging_attempt=attempt
            )

    # Access tokens for the first team (shown on the profile).
    AccessTokens.objects.get_or_create(
        access_token_hash=hashlib.sha256(b"demo-ci-runner").hexdigest(),
        defaults={
            "name": "ci-runner",
            "description": "submits from the pipeline",
            "team": red,
            "created_by": request.user,
        },
    )
    AccessTokens.objects.get_or_create(
        access_token_hash=hashlib.sha256(b"demo-laptop").hexdigest(),
        defaults={
            "name": "laptop",
            "description": "manual testing",
            "team": red,
            "created_by": request.user,
            "revoked": True,
            "revoked_at": now,
        },
    )

    messages.success(
        request,
        "Demo data ready: 6 teams, a running round with scores, and a devbox. "
        "Switch into a team's user below to see their views.",
    )
    return _back(request)
