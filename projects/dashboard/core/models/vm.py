import uuid

from core import clock
from core.models.competition import Round
from core.models.team import Team
from django.db import models
from django.db.models import Q


class VirtualMachine(models.Model):
    """A sandbox VM. With round orchestration (docs/rounds.md) every team run
    builds its own sandbox, so names, management addresses and Proxmox VMIDs
    repeat across runs: they are unique per run only."""

    vm_identifier = models.TextField()
    vm_maintenance_ip = models.TextField()
    # SHA-256 of the VM's secret (the flag a worm must submit to scoring).
    # Null until the round agent has released the secret (game plan); the
    # plaintext never reaches the database.
    ctf_flag_hash = models.TextField(null=True, blank=True)
    round = models.ForeignKey(
        Round,
        on_delete=models.CASCADE,
        related_name="virtual_machines",
    )
    run = models.ForeignKey(
        "core.RoundRun",
        on_delete=models.CASCADE,
        related_name="virtual_machines",
        null=True,
        blank=True,
    )

    proxmox_vm_id = models.IntegerField(null=True)
    # Sandbox addresses (one per sandbox NIC) and the networks they are on.
    addresses = models.JSONField(default=list, blank=True)
    networks = models.JSONField(default=list, blank=True)
    secret_released_at = models.DateTimeField(null=True, blank=True)

    uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        db_table = "virtual_machines"
        indexes = [
            models.Index(fields=["round"]),
            models.Index(fields=["run"]),
            models.Index(fields=["proxmox_vm_id"]),
            models.Index(fields=["uuid"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["run", "vm_identifier"], name="unique_vm_name_per_run"),
            models.UniqueConstraint(fields=["run", "vm_maintenance_ip"], name="unique_vm_maintenance_ip_per_run"),
            models.UniqueConstraint(
                fields=["ctf_flag_hash"],
                condition=Q(ctf_flag_hash__isnull=False),
                name="unique_vm_flag_hash",
            ),
        ]

    def __str__(self):
        return f"VM({self.vm_identifier})"
