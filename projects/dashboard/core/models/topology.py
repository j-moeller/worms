from django.db import models
from django.utils import timezone


class TopologySnapshot(models.Model):
    """Latest view of the Proxmox host (VMs, bridges, firewall), one row per source.

    Written only by the orchestrator (`manage.py collect_topology`, read-only
    Proxmox API token) and read by the dashboard. The dashboard never talks to
    Proxmox itself: the database is the only channel between the two.

    Timestamps use the real wall clock (not core.clock), because this describes
    infrastructure, not competition time.
    """

    source = models.TextField(unique=True)
    data = models.JSONField(default=dict)
    # Set when the last collection attempt failed; `data` then still holds the
    # last successful snapshot.
    error = models.TextField(blank=True, default="")
    collected_at = models.DateTimeField(null=True, blank=True)
    attempted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "topology_snapshots"

    def __str__(self):
        return f"TopologySnapshot({self.source}, {self.collected_at})"
