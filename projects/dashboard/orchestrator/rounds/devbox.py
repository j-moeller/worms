"""Reconcile the database from completed devbox host jobs (docs/devbox.md).

The round agent builds the devboxes (devbox_provision / devbox_rebuild host
jobs) and reports the result (the VMs, their SSH-fabric addresses, the per-team
SNAT source and the token hashes). The agent cannot write the application
tables, so this reconciler -- run by the orchestrator (in the process_attempts
loop) -- applies those results into `devboxes` / `devbox_vms` so the dashboard
can show each team its VMs and the SSH command, and so the attempt processor
can score devbox tokens.

It is idempotent: it applies the newest done provision job (which covers every
team) and then, per team, any newer rebuild job (fresh token hashes). Running
it repeatedly converges to the current state. The orchestrator role has
INSERT/UPDATE on devboxes/devbox_vms (least privilege); it never writes
host_jobs.
"""

from __future__ import annotations

from core.models.devbox import Devbox, DevboxSSHKey, DevboxVM
from core.models.rounds import HostJob
from core.models.team import Team
from django.conf import settings
from django.db import transaction

MAX_TEAMS = 32  # the services block 10.2.0.32/27


def team_slug(team) -> str:
    # A stable, valid slug (scripts/generate_devbox.py / the agent accept it).
    return f"t{team.id}"


def team_authorized_keys(team) -> list[str]:
    return list(DevboxSSHKey.objects.filter(team=team).order_by("id").values_list("public_key", flat=True))


def team_spec(team, index) -> dict:
    """One team's entry in a devbox host job: slug, addressing index, uuid and
    its registered SSH keys. The agent regenerates the whole devbox stack from
    the full list and (re)writes each team's authorized_keys from it."""
    return {
        "slug": team_slug(team),
        "index": index,
        "team_uuid": str(team.uuid),
        "authorized_keys": team_authorized_keys(team),
    }


def provisioned_team_specs(competition) -> list[dict]:
    """Specs for every provisioned team of `competition` (one that has a devbox
    index), by id -- the full teams list a rebuild/reconfigure job needs so the
    agent can regenerate the stack and (re)push every team's keys. A team with
    no devbox yet (not provisioned) is left out: it is not in the stack."""
    indices = {d.team_id: d.index for d in Devbox.objects.filter(team__competition=competition, index__isnull=False)}
    return [team_spec(team, indices[team.id]) for team in Team.objects.filter(competition=competition).order_by("id") if team.id in indices]


def _gateway_defaults():
    cfg = getattr(settings, "DEVBOX", {}) or {}
    return cfg.get("ssh_gateway_host", ""), int(cfg.get("ssh_gateway_port", 2222)), cfg.get("ssh_gateway_user", "team")


def _role_of(name: str) -> str:
    # devbox-<slug>-a / -b / -c
    return name.rsplit("-", 1)[-1] if "-" in name else ""


def apply_devbox_jobs() -> int:
    """Apply completed devbox jobs to the database. Returns the number of VM
    rows upserted/updated."""
    provision = (
        HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_PROVISION, state=HostJob.State.DONE)
        .order_by("-id")
        .first()
    )
    touched = 0
    gw_host, gw_port, gw_user = _gateway_defaults()

    if provision is not None:
        vms = (provision.result or {}).get("vms", [])
        sources = (provision.result or {}).get("services_sources", {})
        teams = {str(t.uuid): t for t in Team.objects.filter(uuid__in={v["team_uuid"] for v in vms if v.get("team_uuid")})}
        with transaction.atomic():
            for team_uuid, team in teams.items():
                slug = next((v["team_slug"] for v in vms if v.get("team_uuid") == team_uuid and v.get("team_slug")), None)
                devbox, _ = Devbox.objects.get_or_create(team=team)
                changed = []
                src = sources.get(slug) if slug else None
                if src and devbox.services_source_ip != src:
                    devbox.services_source_ip = src
                    changed.append("services_source_ip")
                if gw_host and devbox.ssh_gateway_host != gw_host:
                    devbox.ssh_gateway_host, devbox.ssh_gateway_port, devbox.ssh_gateway_user = gw_host, gw_port, gw_user
                    changed += ["ssh_gateway_host", "ssh_gateway_port", "ssh_gateway_user"]
                if changed:
                    devbox.save(update_fields=changed)
                for vm in [v for v in vms if v.get("team_uuid") == team_uuid]:
                    DevboxVM.objects.update_or_create(
                        devbox=devbox,
                        vm_identifier=vm["name"],
                        defaults={
                            "role": _role_of(vm["name"]),
                            "vm_maintenance_ip": vm.get("maintenance_ip", ""),
                            "ssh_fabric_ip": vm.get("ssh_ip", ""),
                            "proxmox_vm_id": vm.get("vmid"),
                            "ctf_flag_hash": vm.get("flag_hash"),
                        },
                    )
                    touched += 1

    # Per-team rebuilds newer than the provision refresh only the token hashes.
    rebuilds = HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_REBUILD, state=HostJob.State.DONE)
    if provision is not None:
        rebuilds = rebuilds.filter(id__gt=provision.id)
    newest_rebuild: dict[str, HostJob] = {}
    for job in rebuilds.order_by("id"):
        tu = (job.result or {}).get("team_uuid") or (job.params or {}).get("team_uuid")
        if tu:
            newest_rebuild[tu] = job
    for team_uuid, job in newest_rebuild.items():
        team = Team.objects.filter(uuid=team_uuid).first()
        devbox = Devbox.objects.filter(team=team).first() if team else None
        if devbox is None:
            continue
        for vm in (job.result or {}).get("vms", []):
            updated = DevboxVM.objects.filter(devbox=devbox, vm_identifier=vm["name"]).update(
                ctf_flag_hash=vm.get("flag_hash")
            )
            touched += updated
    return touched
