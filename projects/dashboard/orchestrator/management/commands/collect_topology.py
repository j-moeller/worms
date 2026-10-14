from core.models.topology import TopologySnapshot
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from orchestrator.proxmox_inventory import collect_topology, readonly_client

SOURCE = "proxmox"


class Command(BaseCommand):
    help = (
        "Collect VMs, bridges and firewall rules from Proxmox (read-only token) "
        "and store them for the dashboard topology view. Run periodically by the "
        "orchestrator-collect-topology systemd timer."
    )

    def handle(self, *args, **options):
        now = timezone.now()
        try:
            config = settings.PROXMOX_READONLY
            data = collect_topology(readonly_client(), config["node"], config["port"])
        except Exception as e:
            # Keep the last good snapshot, but let the dashboard show the failure.
            TopologySnapshot.objects.update_or_create(
                source=SOURCE,
                defaults={"error": str(e), "attempted_at": now},
            )
            raise CommandError(f"Topology collection failed: {e}") from e

        TopologySnapshot.objects.update_or_create(
            source=SOURCE,
            defaults={
                "data": data,
                "error": "",
                "collected_at": now,
                "attempted_at": now,
            },
        )

        self.stdout.write(
            f"Collected {len(data['guests'])} guests, {len(data['networks'])} "
            f"interfaces, {len(data['cluster_firewall']['groups'])} security groups "
            f"({len(data['errors'])} partial errors)."
        )
