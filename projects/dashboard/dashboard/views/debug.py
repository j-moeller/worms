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

    # The development competition (the Dev environment), with a week-long
    # round: what the team's devbox did in it is its Dev progression.
    dev = Competition.objects.filter(is_dev_competition=True).first() or Competition.objects.create(
        name=f"{competition.name} · dev", is_dev_competition=True
    )
    Round.objects.get_or_create(
        competition=dev,
        name="Dev week",
        defaults={"seed": 2, "round_start": now - timedelta(days=1), "round_end": now + timedelta(days=6)},
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
                "development_competition": dev,
            },
        )
        if team.development_competition_id != dev.id:  # demo data from before the dev competition
            team.development_competition = dev
            team.save(update_fields=["development_competition"])
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
    # ranked points and the progression page has content.
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
    for hours, (role, ip, fabric, found) in zip((20, 3, 0), [
        ("a", "10.128.3.10", "10.7.3.10", True),
        ("b", "10.128.3.11", "10.7.3.11", True),
        ("c", "10.128.3.12", "10.7.3.12", False),
    ]):
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
            capture, _ = DevboxCapture.objects.get_or_create(
                team=red, devbox_vm=devbox_vm, devbox_version=red.devbox_version
            )
            # The submission that found it, during the dev round.
            if capture.flag_capture_attempt_id is None:
                capture.flag_capture_attempt = FlagCaptureAttempt.objects.create(
                    team_uuid=str(red.uuid), vm_flag_hash=devbox_vm.ctf_flag_hash or "", devbox=True,
                    processed=True, processed_at=now - timedelta(hours=hours), created_at=now - timedelta(hours=hours),
                )  # fmt: skip
                capture.save(update_fields=["flag_capture_attempt"])
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
                processed=True,
                created_at=now - timedelta(hours=21 - 8 * i),
            )
            DevboxLog.objects.create(
                team=red, devbox_version=red.devbox_version, logging_attempt=attempt
            )

    # Red Team's run in the round and a snapshot of the infrastructure: what
    # the PROGRESSION page and the staff TOPOLOGY player replay.
    _demo_run(round, red, now)
    _demo_snapshot()

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


def _demo_run(round, team, now):
    """A finished run of `team` in `round`, 30 minutes ago: three sandbox VMs
    on two networks; the worm starts on web-0, captures it and db-1, probes
    vault-2 (never captured, so the team does not see it), submits flags and
    sends logs; the monitor's connections in between. Idempotent."""
    from django.db.models import Max

    from core.models.logging import Logging, LoggingAttempt
    from core.models.monitoring import NetworkFlow
    from core.models.rounds import RoundRun, RunEvent
    from core.models.scoring import FlagCapture, FlagCaptureAttempt
    from core.models.vm import VirtualMachine

    if RoundRun.objects.filter(round=round, team=team).exists():
        return
    start = now - timedelta(minutes=30)

    def at(seconds):
        return start + timedelta(seconds=seconds)

    position = (RoundRun.objects.filter(round=round).aggregate(last=Max("position"))["last"] or 0) + 1
    run = RoundRun.objects.create(
        round=round, team=team, position=position, seed=7, state=RoundRun.State.FINISHED,
        sandbox_params={"start_vm": "web-0"}, worm_start_at=start, ends_at=at(600), finished_at=at(660),
    )  # fmt: skip
    nets = {"dmz": {"name": "vmbr1000", "cidr": "10.64.0.0/24"}, "core": {"name": "vmbr1001", "cidr": "10.64.1.0/24"}}
    vms = {}
    for n, (name, addresses, on) in enumerate(
        [("web-0", ["10.64.0.10"], ["dmz"]), ("db-1", ["10.64.0.11", "10.64.1.11"], ["dmz", "core"]), ("vault-2", ["10.64.1.12"], ["core"])]
    ):
        vms[name] = VirtualMachine.objects.create(
            round=round, run=run, vm_identifier=name, vm_maintenance_ip=f"10.4.0.{30 + n}", proxmox_vm_id=7100 + n,
            addresses=addresses, networks=[nets[k] for k in on], secret_released_at=at(5),
            ctf_flag_hash=hashlib.sha256(f"demo-run-{run.uuid}-{name}".encode()).hexdigest(),
        )  # fmt: skip

    def event(seconds, kind, vm=None, **data):
        RunEvent.objects.create(run=run, ts=at(seconds), kind=kind, vm=vm, data=data)

    for seconds, state in [(-150, "preparing"), (-100, "checking"), (-60, "deploying"), (-20, "armed"), (0, "running"),
                           (600, "ending"), (630, "tearing_down"), (660, "finished")]:  # fmt: skip
        event(seconds, "state", state=state)
    event(-30, "worm_deployed", worm_start_at=start.isoformat())
    event(0, "worm_started")
    for vm in vms.values():
        event(5, "secret_released", vm)
    event(300, "vm_stopped", vms["vault-2"])
    event(360, "vm_started", vms["vault-2"])

    def submit(seconds, flag_hash, captured=None):
        attempt = FlagCaptureAttempt.objects.create(
            team_uuid=str(team.uuid), vm_flag_hash=flag_hash, processed=True, processed_at=at(seconds + 1), created_at=at(seconds)
        )
        if captured is not None:
            FlagCapture.objects.create(team=team, round=round, vm=captured, flag_capture_attempt=attempt)
            event(seconds, "captured", captured, team=team.id, attempt=attempt.id)

    submit(42, vms["web-0"].ctf_flag_hash, vms["web-0"])
    submit(185, vms["db-1"].ctf_flag_hash, vms["db-1"])
    submit(240, vms["web-0"].ctf_flag_hash)  # already captured
    submit(410, "0" * 64)  # not a flag

    for seconds, data in [
        (60, {"event": "infected", "host": "web-0"}),
        (190, {"event": "infected", "host": "db-1", "via": "ssh"}),
        (420, {"event": "scan", "from": "db-1", "targets": ["10.64.1.12"], "open": [22]}),
    ]:
        attempt = LoggingAttempt.objects.create(
            team_uuid=str(team.uuid), log_data=data, processed=True, processed_at=at(seconds + 1), created_at=at(seconds)
        )
        Logging.objects.create(team=team, round=round, logging_attempt=attempt)

    web, db, db_core, vault = "10.64.0.10", "10.64.0.11", "10.64.1.11", "10.64.1.12"
    scoring, logging, dns, apt = "10.2.0.14", "10.2.0.15", "10.2.0.11", "10.2.0.20"
    flows = [
        (3, 1.2, web, apt, 80, "tcp", "http", "SF"),
        (40, 0.3, web, scoring, 443, "tcp", "ssl", "SF"),
        (58, 0.3, web, logging, 443, "tcp", "ssl", "SF"),
        (90, 2.5, web, db, 22, "tcp", "ssh", "SF"),
        (120, 0.1, web, db, 3306, "tcp", None, "REJ"),
        (150, 6.0, web, db, 22, "tcp", "ssh", "SF"),
        (183, 0.3, db, scoring, 443, "tcp", "ssl", "SF"),
        (188, 0.3, db, logging, 443, "tcp", "ssl", "SF"),
        (238, 0.3, web, scoring, 443, "tcp", "ssl", "SF"),
        (260, 1.0, db_core, vault, 22, "tcp", "ssh", "SF"),
        (330, 0.1, db_core, vault, 445, "tcp", None, "S0"),
        (408, 0.3, db, scoring, 443, "tcp", "ssl", "SF"),
        (418, 0.3, db, logging, 443, "tcp", "ssl", "SF"),
    ] + [(seconds, 0.05, web if seconds % 2 else db, dns, 53, "udp", "dns", "SF") for seconds in range(10, 600, 25)]
    for i, (seconds, duration, src, dst, port, transport, service, state) in enumerate(flows):
        NetworkFlow.objects.create(
            uid=f"Cdemo{run.id}n{i}", ts=at(seconds), duration=duration, src_ip=src, src_port=40000 + i, dst_ip=dst,
            dst_port=port, transport=transport, service=service, conn_state=state, src_ip_bytes=300 + 40 * i,
            dst_ip_bytes=1200 + 90 * i, src_pkts=4 + i % 5, dst_pkts=3 + i % 4,
        )  # fmt: skip


def _demo_snapshot():
    """A Proxmox snapshot with the infrastructure and the private internet
    (no sandbox: no run is in progress), unless one was collected."""
    from core.models.topology import TopologySnapshot
    from django.utils import timezone

    if TopologySnapshot.objects.filter(source="proxmox").exists():
        return

    def guest(vmid, name, nics, pool="persistent", tags=""):
        config = {"name": name}
        for i, (bridge, cidr) in enumerate(nics):
            config[f"net{i}"] = f"virtio=BC:24:11:00:{vmid % 256:02X}:{i:02X},bridge={bridge},firewall=1"
            config[f"ipconfig{i}"] = f"ip={cidr}"
        return {
            "vmid": vmid, "type": "qemu", "name": name, "status": "running", "pool": pool, "tags": tags, "config": config,
            "firewall": {"options": {"enable": 1, "policy_in": "DROP", "policy_out": "DROP"}, "rules": [], "aliases": {}, "ipsets": {}},
        }  # fmt: skip

    guests = [
        guest(101, "database", [("vmbr21", "10.11.0.16/24")]),
        guest(102, "orchestrator", [("vmbr21", "10.11.0.19/24")]),
        guest(103, "dashboard", [("vmbr20", "10.10.0.18/24")]),
        guest(104, "scoring", [("vmbr23", "10.2.0.14/24"), ("vmbr22", "10.12.0.14/24")], pool="ephemeral"),
        guest(105, "logging", [("vmbr23", "10.2.0.15/24"), ("vmbr22", "10.12.0.15/24")], pool="ephemeral"),
        guest(106, "monitor", [("vmbr22", "10.12.0.100/24")], pool="ephemeral"),
        guest(110, "dns", [("vmbr23", "10.2.0.11/24")], pool="private_internet", tags="private_internet"),
        guest(120, "apt-cache", [("vmbr23", "10.2.0.20/24")], pool="private_internet", tags="private_internet"),
    ]
    bridges = [("vmbr20", "10.10.0.1/24"), ("vmbr21", "10.11.0.1/24"), ("vmbr22", "10.12.0.1/24"), ("vmbr23", None)]
    data = {
        "schema": 1, "node": "worms-pve", "version": "8.4.1", "services": [{"name": "pve-firewall", "state": "running"}],
        "networks": [{"iface": name, "type": "bridge", "cidr": cidr, "active": 1} for name, cidr in bridges],
        "guests": guests,
        "cluster_firewall": {"options": {"enable": 1, "policy_in": "DROP"}, "rules": [], "groups": [], "aliases": {}, "ipsets": {}},
        "node_firewall": {"options": {"enable": 1}, "rules": []}, "host": {"api_port": 8006}, "errors": [],
    }  # fmt: skip
    TopologySnapshot.objects.create(source="proxmox", data=data, collected_at=timezone.now(), attempted_at=timezone.now())
