from django.db import models
from django.db.models.functions import Now


class NetworkFlow(models.Model):
    """One connection seen on the sandbox networks (a Zeek conn.log record).

    Written only by the monitor VM (INSERT-only role dbuser_network_monitor,
    ansible role network_monitor: zeek-shipper); read by the dashboard and the
    orchestrator. Zeek writes a record when the connection ends (or times out),
    so `ts` is the start of the connection and rows arrive up to the
    connection's duration later.

    Every value is derived from attacker-generated traffic. Addresses are the
    sandbox VMs' own (the copy is taken before sandbox-gw's SNAT). Rounds are
    not referenced: a flow belongs to the round whose time window contains `ts`.
    """

    # Zeek connection id: joins the flow to its NetworkEvent rows (dns, http,
    # ssl, ...). Unique, so a record shipped twice is stored once.
    uid = models.TextField(unique=True)
    # Community ID flow hash (https://github.com/corelight/community-id-spec):
    # the same flow in other tools (Suricata, Wireshark) has the same value.
    community_id = models.TextField(blank=True, null=True)

    ts = models.DateTimeField()
    duration = models.FloatField(blank=True, null=True)  # seconds

    src_ip = models.GenericIPAddressField()
    src_port = models.IntegerField(blank=True, null=True)  # ICMP: type
    dst_ip = models.GenericIPAddressField()
    dst_port = models.IntegerField(blank=True, null=True)  # ICMP: code
    src_mac = models.TextField(blank=True, null=True)
    dst_mac = models.TextField(blank=True, null=True)

    # tcp | udp | icmp | unknown_transport
    transport = models.TextField()
    # Application protocol(s) Zeek recognised from the payload, independent of
    # the port (e.g. "http", "ssl", "dns", "ssh"; comma-separated; empty if
    # unknown).
    service = models.TextField(blank=True, null=True)

    # Payload bytes (TCP: from sequence numbers) and packets / bytes on the
    # wire (IP header included) per direction (src = originator).
    src_bytes = models.BigIntegerField(blank=True, null=True)
    dst_bytes = models.BigIntegerField(blank=True, null=True)
    src_pkts = models.BigIntegerField(blank=True, null=True)
    dst_pkts = models.BigIntegerField(blank=True, null=True)
    src_ip_bytes = models.BigIntegerField(blank=True, null=True)
    dst_ip_bytes = models.BigIntegerField(blank=True, null=True)
    # Bytes Zeek did not see (capture loss / asymmetric capture).
    missed_bytes = models.BigIntegerField(blank=True, null=True)

    # TCP state summary (S0 = unanswered SYN, REJ = rejected, SF = normal, ...)
    # and the packet history ("ShADadFf"), see Zeek's conn.log documentation.
    conn_state = models.TextField(blank=True, null=True)
    history = models.TextField(blank=True, null=True)

    inserted_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "network_flows"
        indexes = [
            # Append-only, roughly in time order: inserts land on the
            # right-most leaf, so the index stays cheap to maintain.
            models.Index(fields=["ts"], name="network_flows_ts"),
            models.Index(fields=["src_ip", "ts"], name="network_flows_src"),
            models.Index(fields=["dst_ip", "ts"], name="network_flows_dst"),
        ]

    def __str__(self):
        return f"NetworkFlow({self.transport} {self.src_ip}:{self.src_port} -> {self.dst_ip}:{self.dst_port})"


class NetworkEvent(models.Model):
    """Application-layer details and monitor status, one row per Zeek record.

    `log` is the Zeek log the record comes from (dns, http, ssl, ssh, notice,
    weird, ...; see the role network_monitor for the list); `data` the record
    itself (JSON, size-limited by the shipper). `uid` joins it to the
    NetworkFlow. The monitor also writes its own status here:
    `stats`/`capture_loss` (Zeek, every few minutes: a heartbeat) and
    `shipper` (records dropped by the rate limit).
    """

    log = models.TextField()
    ts = models.DateTimeField()
    uid = models.TextField(blank=True, null=True)
    data = models.JSONField()

    inserted_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "network_events"
        indexes = [
            models.Index(fields=["ts"], name="network_events_ts"),
            models.Index(fields=["uid"], name="network_events_uid"),
            models.Index(fields=["log", "ts"], name="network_events_log"),
        ]

    def __str__(self):
        return f"NetworkEvent({self.log}, {self.ts})"
