from core.models.competition import Round
from django.db import models
from django.utils import timezone


class VirtualMachine(models.Model):
    vm_identifier = models.TextField()
    vm_maintenance_ip = models.TextField()
    ctf_flag_hash = models.TextField(unique=True)
    round = models.ForeignKey(
        Round,
        on_delete=models.CASCADE,
        related_name="virtual_machines",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "virtual_machines"
        indexes = [
            models.Index(fields=["round"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["round", "vm_identifier"],
                name="unique_vm_per_round",
            ),
        ]

    def __str__(self):
        return f"VM({self.vm_identifier})"
