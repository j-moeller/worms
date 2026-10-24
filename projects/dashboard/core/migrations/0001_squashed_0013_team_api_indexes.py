"""The schema as of 0013_team_api_indexes, in one migration (squashed).

`replaces` keeps databases created by the 13 original migrations valid:
Django treats this migration as applied when all of them are recorded, and
runs it on a fresh database. Do not edit it; add new migrations after it.

Not included: the unused worm deployment tables (0002 drops them where an
older release created them)."""

import core.clock
import django.contrib.auth.models
import django.contrib.auth.validators
import django.db.models.deletion
import django.db.models.functions.datetime
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    replaces = [
        ("core", "0001_initial"),
        ("core", "0002_topology_snapshot"),
        ("core", "0003_network_monitoring"),
        ("core", "0004_attempt_db_defaults"),
        ("core", "0005_round_orchestration"),
        ("core", "0006_round_sandbox_definition"),
        ("core", "0007_attempts_team_uuid"),
        ("core", "0008_must_change_password"),
        ("core", "0009_devbox"),
        ("core", "0010_must_change_password_dbdefault"),
        ("core", "0011_devbox_index"),
        ("core", "0012_devbox_ssh_fields"),
        ("core", "0013_team_api_indexes"),
    ]

    dependencies = [("auth", "0012_alter_user_first_name_max_length")]

    operations = [
        migrations.CreateModel(
            name="Devbox",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uuid", models.UUIDField(default=uuid.uuid4, unique=True)),
                ("index", models.PositiveIntegerField(blank=True, null=True, unique=True)),
                ("services_source_ip", models.TextField(blank=True, default="")),
                ("ssh_gateway_host", models.TextField(blank=True, default="")),
                ("ssh_gateway_port", models.IntegerField(default=2222)),
                ("ssh_gateway_user", models.TextField(db_default="team", default="team")),
                ("gateway_vm_id", models.IntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                ("updated_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"db_table": "devboxes"},
        ),
        migrations.CreateModel(
            name="MalwareArtifact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("content_hash", models.TextField(unique=True)),
                ("storage_key", models.TextField()),
                ("created_at", models.DateTimeField(default=core.clock.now)),
            ],
            options={"db_table": "malware_artifacts"},
        ),
        migrations.CreateModel(
            name="TopologySnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source", models.TextField(unique=True)),
                ("data", models.JSONField(default=dict)),
                ("error", models.TextField(blank=True, default="")),
                ("collected_at", models.DateTimeField(blank=True, null=True)),
                ("attempted_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={"db_table": "topology_snapshots"},
        ),
        migrations.CreateModel(
            name="User",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("password", models.CharField(max_length=128, verbose_name="password")),
                ("last_login", models.DateTimeField(blank=True, null=True, verbose_name="last login")),
                (
                    "is_superuser",
                    models.BooleanField(
                        default=False,
                        help_text="Designates that this user has all permissions without explicitly assigning them.",
                        verbose_name="superuser status",
                    ),
                ),
                (
                    "username",
                    models.CharField(
                        error_messages={"unique": "A user with that username already exists."},
                        help_text="Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only.",
                        max_length=150,
                        unique=True,
                        validators=[django.contrib.auth.validators.UnicodeUsernameValidator()],
                        verbose_name="username",
                    ),
                ),
                ("first_name", models.CharField(blank=True, max_length=150, verbose_name="first name")),
                ("last_name", models.CharField(blank=True, max_length=150, verbose_name="last name")),
                ("email", models.EmailField(blank=True, max_length=254, verbose_name="email address")),
                (
                    "is_staff",
                    models.BooleanField(
                        default=False,
                        help_text="Designates whether the user can log into this admin site.",
                        verbose_name="staff status",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="Designates whether this user should be treated as active. Unselect this instead of deleting accounts.",
                        verbose_name="active",
                    ),
                ),
                ("date_joined", models.DateTimeField(default=django.utils.timezone.now, verbose_name="date joined")),
                ("uuid", models.UUIDField(default=uuid.uuid4, unique=True)),
                ("created_at", models.DateTimeField(default=core.clock.now)),
                ("must_change_password", models.BooleanField(db_default=False, default=False)),
                (
                    "groups",
                    models.ManyToManyField(
                        blank=True,
                        help_text="The groups this user belongs to. A user will get all permissions granted to each of their groups.",
                        related_name="user_set",
                        related_query_name="user",
                        to="auth.group",
                        verbose_name="groups",
                    ),
                ),
                (
                    "user_permissions",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Specific permissions for this user.",
                        related_name="user_set",
                        related_query_name="user",
                        to="auth.permission",
                        verbose_name="user permissions",
                    ),
                ),
            ],
            options={"db_table": "users"},
            managers=[("objects", django.contrib.auth.models.UserManager())],
        ),
        migrations.CreateModel(
            name="Competition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.TextField()),
                ("created_at", models.DateTimeField(default=core.clock.now)),
                ("is_dev_competition", models.BooleanField(default=False)),
                ("registration_starts_at", models.DateTimeField(blank=True, null=True)),
                ("registration_ends_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "competitions",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("registration_ends_at__isnull", True), ("registration_starts_at__isnull", True)),
                            models.Q(
                                ("registration_ends_at__isnull", False), ("registration_starts_at__isnull", False)
                            ),
                            _connector="OR",
                        ),
                        name="registration_dates_both_null_or_set",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="DevboxVM",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uuid", models.UUIDField(default=uuid.uuid4, unique=True)),
                ("vm_identifier", models.TextField()),
                ("role", models.TextField(default="")),
                ("vm_maintenance_ip", models.TextField(blank=True, default="")),
                ("ssh_fabric_ip", models.TextField(blank=True, db_default="", default="")),
                ("proxmox_vm_id", models.IntegerField(blank=True, null=True)),
                ("addresses", models.JSONField(blank=True, default=list)),
                ("networks", models.JSONField(blank=True, default=list)),
                ("ctf_flag_hash", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                (
                    "devbox",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="vms", to="core.devbox"
                    ),
                ),
            ],
            options={"db_table": "devbox_vms"},
        ),
        migrations.CreateModel(
            name="FlagCaptureAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("team_uuid", models.TextField(blank=True, null=True)),
                ("vm_flag_hash", models.TextField()),
                ("devbox", models.BooleanField(db_default=False, default=False)),
                ("processed", models.BooleanField(db_default=False, default=False)),
                ("processed_at", models.DateTimeField(blank=True, default=None, null=True)),
                (
                    "created_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), default=core.clock.now),
                ),
            ],
            options={
                "db_table": "flag_capture_attempts",
                "indexes": [
                    models.Index(
                        condition=models.Q(("processed", False)), fields=["id"], name="pending_flag_capture_attempts"
                    ),
                    models.Index(fields=["team_uuid", "created_at", "id"], name="flag_attempts_team_time"),
                ],
            },
        ),
        migrations.CreateModel(
            name="LoggingAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uuid", models.UUIDField(default=uuid.uuid4, unique=True)),
                ("team_uuid", models.TextField(blank=True, null=True)),
                ("log_data", models.JSONField()),
                ("devbox", models.BooleanField(db_default=False, default=False)),
                ("processed", models.BooleanField(db_default=False, default=False)),
                ("processed_at", models.DateTimeField(blank=True, default=None, null=True)),
                (
                    "created_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), default=core.clock.now),
                ),
            ],
            options={
                "db_table": "logging_attempts",
                "indexes": [
                    models.Index(
                        condition=models.Q(("processed", False)), fields=["id"], name="pending_logging_attempts"
                    ),
                    models.Index(fields=["uuid"], name="pending_logging_attempts_uuid"),
                    models.Index(fields=["team_uuid", "created_at", "id"], name="logging_attempts_team_time"),
                ],
            },
        ),
        migrations.CreateModel(
            name="NetworkEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("log", models.TextField()),
                ("ts", models.DateTimeField()),
                ("uid", models.TextField(blank=True, null=True)),
                ("data", models.JSONField()),
                ("inserted_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
            ],
            options={
                "db_table": "network_events",
                "indexes": [
                    models.Index(fields=["ts"], name="network_events_ts"),
                    models.Index(fields=["uid"], name="network_events_uid"),
                    models.Index(fields=["log", "ts"], name="network_events_log"),
                ],
            },
        ),
        migrations.CreateModel(
            name="NetworkFlow",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uid", models.TextField(unique=True)),
                ("community_id", models.TextField(blank=True, null=True)),
                ("ts", models.DateTimeField()),
                ("duration", models.FloatField(blank=True, null=True)),
                ("src_ip", models.GenericIPAddressField()),
                ("src_port", models.IntegerField(blank=True, null=True)),
                ("dst_ip", models.GenericIPAddressField()),
                ("dst_port", models.IntegerField(blank=True, null=True)),
                ("src_mac", models.TextField(blank=True, null=True)),
                ("dst_mac", models.TextField(blank=True, null=True)),
                ("transport", models.TextField()),
                ("service", models.TextField(blank=True, null=True)),
                ("src_bytes", models.BigIntegerField(blank=True, null=True)),
                ("dst_bytes", models.BigIntegerField(blank=True, null=True)),
                ("src_pkts", models.BigIntegerField(blank=True, null=True)),
                ("dst_pkts", models.BigIntegerField(blank=True, null=True)),
                ("src_ip_bytes", models.BigIntegerField(blank=True, null=True)),
                ("dst_ip_bytes", models.BigIntegerField(blank=True, null=True)),
                ("missed_bytes", models.BigIntegerField(blank=True, null=True)),
                ("conn_state", models.TextField(blank=True, null=True)),
                ("history", models.TextField(blank=True, null=True)),
                ("inserted_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
            ],
            options={
                "db_table": "network_flows",
                "indexes": [
                    models.Index(fields=["ts"], name="network_flows_ts"),
                    models.Index(fields=["src_ip", "ts"], name="network_flows_src"),
                    models.Index(fields=["dst_ip", "ts"], name="network_flows_dst"),
                ],
            },
        ),
        migrations.CreateModel(
            name="Round",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("seed", models.BigIntegerField()),
                ("round_start", models.DateTimeField(blank=True, null=True)),
                ("round_end", models.DateTimeField(blank=True, null=True)),
                ("name", models.TextField()),
                ("created_at", models.DateTimeField(default=core.clock.now)),
                (
                    "competition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="rounds", to="core.competition"
                    ),
                ),
            ],
            options={"db_table": "rounds"},
        ),
        migrations.CreateModel(
            name="RoundRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uuid", models.UUIDField(default=uuid.uuid4, unique=True)),
                ("position", models.PositiveIntegerField()),
                ("seed", models.BigIntegerField()),
                (
                    "state",
                    models.TextField(
                        choices=[
                            ("pending", "Pending"),
                            ("preparing", "Preparing"),
                            ("checking", "Checking"),
                            ("deploying", "Deploying"),
                            ("armed", "Armed"),
                            ("running", "Running"),
                            ("ending", "Ending"),
                            ("tearing_down", "Tearing Down"),
                            ("finished", "Finished"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                        ],
                        default="pending",
                    ),
                ),
                ("error", models.TextField(blank=True, default="")),
                ("sandbox_params", models.JSONField(default=dict)),
                ("topology", models.JSONField(default=dict)),
                ("sandbox_definition", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                ("state_changed_at", models.DateTimeField(blank=True, null=True)),
                ("worm_start_at", models.DateTimeField(blank=True, null=True)),
                ("ends_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "round",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="runs", to="core.round"
                    ),
                ),
            ],
            options={"db_table": "round_runs"},
        ),
        migrations.CreateModel(
            name="HostJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uuid", models.UUIDField(default=uuid.uuid4, unique=True)),
                (
                    "kind",
                    models.TextField(
                        choices=[
                            ("prepare", "Prepare"),
                            ("check", "Check"),
                            ("deploy", "Deploy"),
                            ("release_secrets", "Release Secrets"),
                            ("teardown", "Teardown"),
                            ("devbox_provision", "Devbox Provision"),
                            ("devbox_rebuild", "Devbox Rebuild"),
                        ]
                    ),
                ),
                ("params", models.JSONField(default=dict)),
                (
                    "state",
                    models.TextField(
                        choices=[("queued", "Queued"), ("running", "Running"), ("done", "Done"), ("failed", "Failed")],
                        db_default="queued",
                        default="queued",
                    ),
                ),
                ("requested_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True, default="")),
                (
                    "run",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="jobs",
                        to="core.roundrun",
                    ),
                ),
            ],
            options={"db_table": "host_jobs"},
        ),
        migrations.CreateModel(
            name="Team",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uuid", models.UUIDField(default=uuid.uuid4, unique=True)),
                ("name", models.TextField()),
                ("max_members", models.PositiveIntegerField(default=2)),
                ("created_at", models.DateTimeField(default=core.clock.now)),
                ("devbox_version", models.PositiveIntegerField(db_default=1, default=1)),
                (
                    "competition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="teams", to="core.competition"
                    ),
                ),
                (
                    "development_competition",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="development_teams",
                        to="core.competition",
                    ),
                ),
            ],
            options={"db_table": "teams"},
        ),
        migrations.AddField(
            model_name="roundrun",
            name="team",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="runs", to="core.team"),
        ),
        migrations.CreateModel(
            name="Logging",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=core.clock.now)),
                (
                    "logging_attempt",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="logging", to="core.loggingattempt"
                    ),
                ),
                (
                    "round",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="logging", to="core.round"
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="logging", to="core.team"
                    ),
                ),
            ],
            options={"db_table": "logging"},
        ),
        migrations.CreateModel(
            name="DevboxSSHKey",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.TextField(blank=True, default="")),
                ("public_key", models.TextField()),
                ("created_at", models.DateTimeField(default=core.clock.now)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="devbox_ssh_keys",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="devbox_ssh_keys", to="core.team"
                    ),
                ),
            ],
            options={"db_table": "devbox_ssh_keys"},
        ),
        migrations.CreateModel(
            name="DevboxLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("devbox_version", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                (
                    "logging_attempt",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="devbox_logs",
                        to="core.loggingattempt",
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="devbox_logs", to="core.team"
                    ),
                ),
            ],
            options={"db_table": "devbox_logs"},
        ),
        migrations.CreateModel(
            name="DevboxCapture",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("devbox_version", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                (
                    "devbox_vm",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="captures", to="core.devboxvm"
                    ),
                ),
                (
                    "flag_capture_attempt",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="devbox_captures",
                        to="core.flagcaptureattempt",
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="devbox_captures", to="core.team"
                    ),
                ),
            ],
            options={"db_table": "devbox_captures"},
        ),
        migrations.AddField(
            model_name="devbox",
            name="team",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE, related_name="devbox", to="core.team"
            ),
        ),
        migrations.CreateModel(
            name="AccessTokens",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.TextField()),
                ("description", models.TextField(default="")),
                ("access_token_hash", models.TextField(unique=True)),
                ("revoked", models.BooleanField(default=False)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=core.clock.now)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access_tokens",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="access_tokens", to="core.team"
                    ),
                ),
            ],
            options={"db_table": "access_tokens"},
        ),
        migrations.CreateModel(
            name="TeamMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=core.clock.now)),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="team_memberships", to="core.team"
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="team_memberships",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"db_table": "team_memberships"},
        ),
        migrations.AddField(
            model_name="team",
            name="users",
            field=models.ManyToManyField(
                related_name="teams", through="core.TeamMembership", to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.CreateModel(
            name="Upload",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=core.clock.now)),
                (
                    "artifact",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="uploads", to="core.malwareartifact"
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="uploads", to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
            options={"db_table": "uploads"},
        ),
        migrations.AddField(
            model_name="roundrun",
            name="upload",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="runs",
                to="core.upload",
            ),
        ),
        migrations.CreateModel(
            name="VirtualMachine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("vm_identifier", models.TextField()),
                ("vm_maintenance_ip", models.TextField()),
                ("ctf_flag_hash", models.TextField(blank=True, null=True)),
                ("proxmox_vm_id", models.IntegerField(null=True)),
                ("addresses", models.JSONField(blank=True, default=list)),
                ("networks", models.JSONField(blank=True, default=list)),
                ("secret_released_at", models.DateTimeField(blank=True, null=True)),
                ("uuid", models.UUIDField(default=uuid.uuid4, unique=True)),
                ("created_at", models.DateTimeField(default=core.clock.now)),
                (
                    "round",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="virtual_machines", to="core.round"
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="virtual_machines",
                        to="core.roundrun",
                    ),
                ),
            ],
            options={"db_table": "virtual_machines"},
        ),
        migrations.CreateModel(
            name="RunEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ts", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                (
                    "kind",
                    models.TextField(
                        choices=[
                            ("state", "State"),
                            ("vm_stopped", "Vm Stopped"),
                            ("vm_started", "Vm Started"),
                            ("secret_released", "Secret Released"),
                            ("worm_deployed", "Worm Deployed"),
                            ("worm_started", "Worm Started"),
                            ("captured", "Captured"),
                            ("error", "Error"),
                        ]
                    ),
                ),
                ("data", models.JSONField(blank=True, default=dict)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="events", to="core.roundrun"
                    ),
                ),
                (
                    "vm",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events",
                        to="core.virtualmachine",
                    ),
                ),
            ],
            options={"db_table": "run_events"},
        ),
        migrations.CreateModel(
            name="GamePlanStep",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.PositiveIntegerField()),
                (
                    "action",
                    models.TextField(
                        choices=[("release_secret", "Release Secret"), ("stop_vm", "Stop Vm"), ("start_vm", "Start Vm")]
                    ),
                ),
                ("at_seconds", models.IntegerField(blank=True, null=True)),
                ("after", models.JSONField(blank=True, default=list)),
                (
                    "state",
                    models.TextField(
                        choices=[
                            ("pending", "Pending"),
                            ("requested", "Requested"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                    ),
                ),
                ("executed_at", models.DateTimeField(blank=True, null=True)),
                ("detail", models.TextField(blank=True, default="")),
                (
                    "host_job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="steps",
                        to="core.hostjob",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="steps", to="core.roundrun"
                    ),
                ),
                (
                    "vm",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="steps", to="core.virtualmachine"
                    ),
                ),
            ],
            options={"db_table": "game_plan_steps", "ordering": ["position"]},
        ),
        migrations.CreateModel(
            name="FlagCapture",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=core.clock.now)),
                (
                    "flag_capture_attempt",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="flag_captures",
                        to="core.flagcaptureattempt",
                    ),
                ),
                (
                    "round",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="flag_captures", to="core.round"
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="flag_captures", to="core.team"
                    ),
                ),
                (
                    "vm",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="flag_captures",
                        to="core.virtualmachine",
                    ),
                ),
            ],
            options={"db_table": "flag_captures"},
        ),
        migrations.CreateModel(
            name="ApplicationSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(unique=True, verbose_name=8)),
                (
                    "current_competition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.competition"
                    ),
                ),
            ],
            options={
                "db_table": "application_setting",
                "indexes": [models.Index(fields=["key"], name="application_key_53043d_idx")],
            },
        ),
        migrations.AddConstraint(
            model_name="devboxvm",
            constraint=models.UniqueConstraint(fields=("devbox", "vm_identifier"), name="unique_devbox_vm_name"),
        ),
        migrations.AddConstraint(
            model_name="devboxvm",
            constraint=models.UniqueConstraint(
                condition=models.Q(("ctf_flag_hash__isnull", False)),
                fields=("ctf_flag_hash",),
                name="unique_devbox_vm_flag_hash",
            ),
        ),
        migrations.AddIndex(
            model_name="round", index=models.Index(fields=["competition"], name="rounds_competi_2c354a_idx")
        ),
        migrations.AddIndex(
            model_name="round", index=models.Index(fields=["round_start"], name="rounds_round_s_792345_idx")
        ),
        migrations.AddIndex(
            model_name="round", index=models.Index(fields=["round_end"], name="rounds_round_e_fca7a8_idx")
        ),
        migrations.AddConstraint(
            model_name="round",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("round_start__isnull", True),
                    ("round_end__isnull", True),
                    ("round_end__gt", models.F("round_start")),
                    _connector="OR",
                ),
                name="round_end_after_round_start",
            ),
        ),
        migrations.AddConstraint(
            model_name="round",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("round_end__isnull", True), ("round_start__isnull", True)),
                    models.Q(("round_end__isnull", False), ("round_start__isnull", False)),
                    _connector="OR",
                ),
                name="round_dates_both_null_or_set",
            ),
        ),
        migrations.AddIndex(model_name="hostjob", index=models.Index(fields=["state", "id"], name="host_jobs_queue")),
        migrations.AddIndex(
            model_name="logging", index=models.Index(fields=["team"], name="logging_team_id_7a16a3_idx")
        ),
        migrations.AddIndex(
            model_name="logging", index=models.Index(fields=["created_at"], name="logging_created_f9d7db_idx")
        ),
        migrations.AddIndex(
            model_name="logging", index=models.Index(fields=["team", "round", "id"], name="logging_team_round")
        ),
        migrations.AddConstraint(
            model_name="logging",
            constraint=models.UniqueConstraint(fields=("logging_attempt",), name="unique_logging_per_attempt"),
        ),
        migrations.AddIndex(
            model_name="devboxsshkey", index=models.Index(fields=["team"], name="devbox_ssh__team_id_ce56df_idx")
        ),
        migrations.AddIndex(
            model_name="devboxlog",
            index=models.Index(fields=["team", "devbox_version"], name="devbox_logs_team_id_243d03_idx"),
        ),
        migrations.AddIndex(
            model_name="devboxlog", index=models.Index(fields=["created_at"], name="devbox_logs_created_324942_idx")
        ),
        migrations.AddConstraint(
            model_name="devboxlog",
            constraint=models.UniqueConstraint(fields=("logging_attempt",), name="unique_devbox_log_per_attempt"),
        ),
        migrations.AddIndex(
            model_name="devboxcapture",
            index=models.Index(fields=["team", "devbox_version"], name="devbox_capt_team_id_b7199d_idx"),
        ),
        migrations.AddConstraint(
            model_name="devboxcapture",
            constraint=models.UniqueConstraint(
                fields=("team", "devbox_vm", "devbox_version"), name="unique_devbox_capture_per_version"
            ),
        ),
        migrations.AddConstraint(
            model_name="teammembership",
            constraint=models.UniqueConstraint(fields=("user", "team"), name="unique_user_team_membership"),
        ),
        migrations.AddConstraint(
            model_name="team",
            constraint=models.UniqueConstraint(fields=("competition", "name"), name="unique_team_per_competition"),
        ),
        migrations.AddIndex(
            model_name="upload", index=models.Index(fields=["artifact"], name="uploads_artifac_b019e5_idx")
        ),
        migrations.AddIndex(
            model_name="upload", index=models.Index(fields=["user"], name="uploads_user_id_a9f05f_idx")
        ),
        migrations.AddIndex(
            model_name="upload", index=models.Index(fields=["created_at"], name="uploads_created_b3d1d3_idx")
        ),
        migrations.AddIndex(
            model_name="roundrun", index=models.Index(fields=["state"], name="round_runs_state_084cab_idx")
        ),
        migrations.AddConstraint(
            model_name="roundrun",
            constraint=models.UniqueConstraint(fields=("round", "team"), name="unique_run_per_round_team"),
        ),
        migrations.AddConstraint(
            model_name="roundrun",
            constraint=models.UniqueConstraint(fields=("round", "position"), name="unique_run_position"),
        ),
        migrations.AddIndex(
            model_name="virtualmachine", index=models.Index(fields=["round"], name="virtual_mac_round_i_a2a8e2_idx")
        ),
        migrations.AddIndex(
            model_name="virtualmachine", index=models.Index(fields=["run"], name="virtual_mac_run_id_172d0c_idx")
        ),
        migrations.AddIndex(
            model_name="virtualmachine",
            index=models.Index(fields=["proxmox_vm_id"], name="virtual_mac_proxmox_e37b8f_idx"),
        ),
        migrations.AddIndex(
            model_name="virtualmachine", index=models.Index(fields=["uuid"], name="virtual_mac_uuid_537957_idx")
        ),
        migrations.AddConstraint(
            model_name="virtualmachine",
            constraint=models.UniqueConstraint(fields=("run", "vm_identifier"), name="unique_vm_name_per_run"),
        ),
        migrations.AddConstraint(
            model_name="virtualmachine",
            constraint=models.UniqueConstraint(
                fields=("run", "vm_maintenance_ip"), name="unique_vm_maintenance_ip_per_run"
            ),
        ),
        migrations.AddConstraint(
            model_name="virtualmachine",
            constraint=models.UniqueConstraint(
                condition=models.Q(("ctf_flag_hash__isnull", False)),
                fields=("ctf_flag_hash",),
                name="unique_vm_flag_hash",
            ),
        ),
        migrations.AddIndex(
            model_name="runevent", index=models.Index(fields=["run", "ts"], name="run_events_timeline")
        ),
        migrations.AddConstraint(
            model_name="gameplanstep",
            constraint=models.UniqueConstraint(fields=("run", "position"), name="unique_step_position"),
        ),
        migrations.AddIndex(
            model_name="flagcapture", index=models.Index(fields=["team"], name="flag_captur_team_id_d9ca4b_idx")
        ),
        migrations.AddIndex(
            model_name="flagcapture", index=models.Index(fields=["round"], name="flag_captur_round_i_6da220_idx")
        ),
        migrations.AddIndex(
            model_name="flagcapture", index=models.Index(fields=["vm"], name="flag_captur_vm_id_9904ec_idx")
        ),
        migrations.AddIndex(
            model_name="flagcapture",
            index=models.Index(fields=["flag_capture_attempt"], name="flag_captur_flag_ca_b2c073_idx"),
        ),
        migrations.AddConstraint(
            model_name="flagcapture",
            constraint=models.UniqueConstraint(fields=("flag_capture_attempt",), name="unique_capture_per_attempt"),
        ),
        migrations.AddConstraint(
            model_name="flagcapture",
            constraint=models.UniqueConstraint(fields=("team", "vm"), name="unique_capture_per_team_vm"),
        ),
    ]
