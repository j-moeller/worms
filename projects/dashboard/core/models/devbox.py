"""Devbox: a team-personalised, always-on clone of the sandbox VMs.

Each team gets its own three untrusted VMs (A-B, B-C) they can SSH into
(through a locked-down gateway) and attack freely, separately from the
competition rounds. See docs/devbox.md.

Design points reflected here:

- A devbox belongs to exactly one team (`Devbox`, one row per team). Its three
  VMs (`DevboxVM`) each hold a token (`ctf_flag_hash`); the team's dev score is
  the number of distinct tokens its worm has submitted to scoring.
- Resetting the environment does NOT delete records. Instead the team's
  `devbox_version` (on `Team`) is incremented; captures and logs are tagged
  with the version in force when they were recorded (`DevboxCapture`,
  `DevboxLog`), and the dashboard only shows the current version. Old data
  stays for inspection.
- The scoring/logging servers attribute a devbox submission to a team by the
  source address of its devbox gateway (`Devbox.services_source_ip`), which is
  ipfilter-pinned, so one team cannot submit as another and devbox traffic
  never interferes with the single running round (which is attributed
  separately, by the current-team file). See docs/devbox.md.
"""

import uuid

from core import clock
from core.models.scoring import FlagCaptureAttempt
from core.models.logging import LoggingAttempt
from core.models.team import Team
from core.models.user import User
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now


class Devbox(models.Model):
    """One team's devbox environment (its three VMs, gateway and attribution
    address). Created when the team is created; rebuilt on reset."""

    team = models.OneToOneField(Team, on_delete=models.CASCADE, related_name="devbox")
    uuid = models.UUIDField(default=uuid.uuid4, unique=True)

    # The SNAT source address of this team's devbox gateway on the services
    # bridge. The scoring/logging servers map it to the team (source-address
    # attribution; ipfilter-pinned, so it cannot be forged).
    services_source_ip = models.TextField(blank=True, default="")

    # Where the team connects with SSH (the restrictive devbox SSH gateway)
    # and the Proxmox VMID of the per-team gateway router VM.
    ssh_gateway_host = models.TextField(blank=True, default="")
    ssh_gateway_port = models.IntegerField(default=2222)
    gateway_vm_id = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "devboxes"

    def __str__(self):
        return f"Devbox({self.team.name})"


class DevboxVM(models.Model):
    """One VM of a team's devbox (A, B or C). Mirrors the sandbox VM record but
    is permanent and team-bound; names/IPs repeat across resets, so they are
    unique per devbox only."""

    devbox = models.ForeignKey(Devbox, on_delete=models.CASCADE, related_name="vms")
    uuid = models.UUIDField(default=uuid.uuid4, unique=True)

    vm_identifier = models.TextField()  # devbox-<team>-a / -b / -c
    role = models.TextField(default="")  # a | b | c (position in the A-B-C chain)
    vm_maintenance_ip = models.TextField(blank=True, default="")
    proxmox_vm_id = models.IntegerField(null=True, blank=True)
    addresses = models.JSONField(default=list, blank=True)
    networks = models.JSONField(default=list, blank=True)

    # SHA-256 of the token in this VM (the flag a worm submits to scoring). The
    # plaintext is written into the VM by the host and never stored here.
    ctf_flag_hash = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "devbox_vms"
        constraints = [
            models.UniqueConstraint(fields=["devbox", "vm_identifier"], name="unique_devbox_vm_name"),
            models.UniqueConstraint(
                fields=["ctf_flag_hash"],
                condition=Q(ctf_flag_hash__isnull=False),
                name="unique_devbox_vm_flag_hash",
            ),
        ]

    def __str__(self):
        return f"DevboxVM({self.vm_identifier})"


class DevboxSSHKey(models.Model):
    """A public SSH key a team registered; the devbox VMs are initialised to
    accept it (through the gateway). Teams add and remove keys on the dashboard
    dev view."""

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="devbox_ssh_keys")
    name = models.TextField(blank=True, default="")
    public_key = models.TextField()
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="devbox_ssh_keys")
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        db_table = "devbox_ssh_keys"
        indexes = [models.Index(fields=["team"])]

    def __str__(self):
        return f"DevboxSSHKey({self.team.name}, {self.name})"


class DevboxCapture(models.Model):
    """A token found in a team's devbox, at a given devbox version. The dev
    score is the count of these at the team's current version. A token counts
    once per (team, VM, version); a reset bumps the version and the count
    restarts without deleting history."""

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="devbox_captures")
    devbox_vm = models.ForeignKey(DevboxVM, on_delete=models.CASCADE, related_name="captures")
    devbox_version = models.PositiveIntegerField()
    flag_capture_attempt = models.ForeignKey(
        FlagCaptureAttempt, on_delete=models.CASCADE, related_name="devbox_captures", null=True, blank=True
    )
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "devbox_captures"
        indexes = [
            models.Index(fields=["team", "devbox_version"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "devbox_vm", "devbox_version"],
                name="unique_devbox_capture_per_version",
            ),
        ]


class DevboxLog(models.Model):
    """A log a team's devbox sent, tagged with the devbox version in force. The
    dev log view shows only the current version."""

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="devbox_logs")
    devbox_version = models.PositiveIntegerField()
    logging_attempt = models.ForeignKey(LoggingAttempt, on_delete=models.CASCADE, related_name="devbox_logs")
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "devbox_logs"
        indexes = [
            models.Index(fields=["team", "devbox_version"]),
            models.Index(fields=["created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["logging_attempt"], name="unique_devbox_log_per_attempt"),
        ]
