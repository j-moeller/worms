import uuid

from core import clock
from core.models.competition import Round
from core.models.team import Team
from django.db import models


class VirtualMachine(models.Model):
    vm_identifier = models.TextField(unique=True)
    vm_maintenance_ip = models.TextField(unique=True)
    ctf_flag_hash = models.TextField(unique=True)
    round = models.ForeignKey(
        Round,
        on_delete=models.CASCADE,
        related_name="virtual_machines",
    )

    proxmox_vm_id = models.IntegerField(unique=True, null=True)
    uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        db_table = "virtual_machines"
        indexes = [
            models.Index(fields=["round"]),
            models.Index(fields=["proxmox_vm_id"]),
            models.Index(fields=["uuid"]),
        ]

    def __str__(self):
        return f"VM({self.vm_identifier})"
