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

from core.locking import serialized_resource
from core.models.devbox import Devbox, DevboxSSHKey, DevboxVM
from core.models.rounds import HostJob
from core.models.team import Team
from django.conf import settings
from django.db import transaction
from django.db.models import F, Max

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


def request_rebuild(team):
    """Bump the team's devbox version (which immediately clears its dev score and logs,
    without deleting any records) and enqueue a rebuild host job, which rebuilds its VMs and
    rewrites their tokens. Returns (job, enqueued). Rebuilds never pile up: if one is queued or
    running, that one comes back with enqueued False and the version stays
    (docs/security-assessment.md, D2). Without a devbox there is nothing to rebuild: (None,
    False), with the version bumped all the same, as a reset always did.

    A rebuild carries every team's keys, so it takes the devbox-job queue lock that key
    updates take too: a later rebuild cannot undo a revocation."""
    with serialized_resource("devbox-jobs", "queue"):
        Team.objects.select_for_update().get(pk=team.pk)
        existing = HostJob.objects.filter(
            kind=HostJob.Kind.DEVBOX_REBUILD,
            state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING],
            params__team_uuid=str(team.uuid),
        ).first()
        if existing is not None:
            return existing, False
        Team.objects.filter(pk=team.pk).update(devbox_version=F("devbox_version") + 1)
        team.refresh_from_db(fields=["devbox_version"])
        if not Devbox.objects.filter(team=team).exists():
            return None, False
        # The agent regenerates the whole devbox stack and re-pushes every team's SSH keys, so
        # the job carries the full teams list (with keys), not just this team: otherwise the
        # rebuild fails validation and the VMs and keys are never updated.
        job = HostJob.objects.create(
            kind=HostJob.Kind.DEVBOX_REBUILD,
            params={
                "team_uuid": str(team.uuid),
                "devbox_version": team.devbox_version,
                "teams": provisioned_team_specs(team.competition),
            },
        )
    return job, True


class ProvisionError(ValueError):
    pass


def _next_free_index(used):
    for i in range(MAX_TEAMS):
        if i not in used:
            return i
    raise ProvisionError(f"no free devbox index (at most {MAX_TEAMS} teams)")


def request_reconfigure(competition):
    """Enqueue a devbox_reconfigure host job: the round agent pushes every team's
    SSH keys and the source->team map again, from what the database says. Needed
    after a reset of the service VMs (`worms devbox reconfigure`), which takes the
    map off scoring and logging. Touches no VM's disk and no token.

    Returns the job, or None when no devbox is provisioned or a queued job
    already carries exactly this, after any queued rebuild or provision (whose older
    key snapshot would otherwise come last)."""
    with serialized_resource("devbox-jobs", "queue"):
        teams = provisioned_team_specs(competition)
        barrier = HostJob.objects.filter(
            kind__in=[HostJob.Kind.DEVBOX_REBUILD, HostJob.Kind.DEVBOX_PROVISION],
            state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING],
        ).aggregate(latest=Max("id"))["latest"] or 0
        queued = HostJob.objects.filter(id__gt=barrier, kind=HostJob.Kind.DEVBOX_RECONFIGURE, state=HostJob.State.QUEUED)
        if not teams or any(job.params.get("teams") == teams for job in queued):
            return None
        # The agent requires a team_uuid, though only for its log; no team asked for this one.
        return HostJob.objects.create(
            kind=HostJob.Kind.DEVBOX_RECONFIGURE,
            params={"team_uuid": teams[0]["team_uuid"], "teams": teams},
        )


def provision(competition):
    """Ensure every team of `competition` has a Devbox with a stable index and
    services-source address, and enqueue one devbox_provision host job with
    the full team list. The agent's provisioning is idempotent, so this is
    safe whenever teams change (`manage.py provision_devboxes`, run by the
    operator as `worms devbox provision`). Indices are assigned once and kept.

    Returns (spec, job): job is None when a queued provision job already
    carries exactly this spec (nothing new to build)."""
    with serialized_resource("devbox-jobs", "queue"):
        return _provision(competition)


def _provision(competition):
    teams = list(Team.objects.filter(competition=competition).order_by("id"))
    if len(teams) > MAX_TEAMS:
        raise ProvisionError(f"{len(teams)} teams exceeds the devbox maximum of {MAX_TEAMS}")
    with transaction.atomic():
        used = set(Devbox.objects.filter(index__isnull=False).values_list("index", flat=True))
        spec = []
        for team in teams:
            devbox, _ = Devbox.objects.get_or_create(team=team)
            if devbox.index is None:
                devbox.index = _next_free_index(used)
                used.add(devbox.index)
                devbox.services_source_ip = f"10.2.0.{32 + devbox.index}"
                devbox.save(update_fields=["index", "services_source_ip"])
            spec.append(team_spec(team, devbox.index))
        if not spec:
            return spec, None
        queued = HostJob.objects.filter(kind=HostJob.Kind.DEVBOX_PROVISION, state=HostJob.State.QUEUED)
        if any(job.params.get("teams") == spec for job in queued):
            return spec, None
        job = HostJob.objects.create(kind=HostJob.Kind.DEVBOX_PROVISION, params={"teams": spec})
    return spec, job


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

    # Where each VM is, for the team's page. Every kind of devbox job reports the
    # addresses of all VMs, so the newest one is enough, and a key change also
    # corrects VMs built before jobs reported them.
    newest = (
        HostJob.objects.filter(
            kind__in=[HostJob.Kind.DEVBOX_PROVISION, HostJob.Kind.DEVBOX_REBUILD, HostJob.Kind.DEVBOX_RECONFIGURE],
            state=HostJob.State.DONE,
            result__has_key="addresses",
        )
        .order_by("-id")
        .first()
    )
    if newest is not None:
        for name, addresses in newest.result["addresses"].items():
            touched += DevboxVM.objects.filter(vm_identifier=name).exclude(addresses=addresses).update(addresses=addresses)
    return touched
