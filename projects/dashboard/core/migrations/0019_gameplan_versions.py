# Every version of a gameplan, by SHA-256, with its bytes (gameplan_versions), what each file was
# over time (gameplan_changes), and the version each run pinned (round_runs.gameplan_version).
# The description for teams moves from the file to its version.

import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import migrations, models

# PostgreSQL only, as 0004 does for the event tables: the database itself keeps the record whole.
# - A version's SHA-256 is its content's, and its size the content's length.
# - The account that inserts a row is recorded (recorded_by DEFAULT current_user); no writer is
#   granted that column, nor the time.
# - No row of either table is ever changed or removed: no role is granted UPDATE or DELETE
#   (setup_db_permissions), and this trigger refuses both to the tables' owner as well.
PROTECT = [
    "ALTER TABLE gameplan_versions ALTER COLUMN recorded_by SET DEFAULT current_user",
    "ALTER TABLE gameplan_changes ALTER COLUMN recorded_by SET DEFAULT current_user",
    "ALTER TABLE gameplan_versions ADD CONSTRAINT gameplan_versions_whole CHECK ("
    "sha256 ~ '^[0-9a-f]{64}$' AND encode(sha256(content), 'hex') = sha256 AND size = octet_length(content) "
    "AND length(filename) <= 200 AND length(description) <= 20000)",
    "ALTER TABLE gameplan_changes ADD CONSTRAINT gameplan_changes_small CHECK (length(filename) <= 200)",
    """CREATE FUNCTION worms_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION USING MESSAGE = TG_TABLE_NAME || ' is append-only: a row is never changed or removed',
    ERRCODE = 'insufficient_privilege';
END
$$""",
    "CREATE TRIGGER gameplan_versions_append_only BEFORE UPDATE OR DELETE ON gameplan_versions "
    "FOR EACH ROW EXECUTE FUNCTION worms_append_only()",
    "CREATE TRIGGER gameplan_changes_append_only BEFORE UPDATE OR DELETE ON gameplan_changes "
    "FOR EACH ROW EXECUTE FUNCTION worms_append_only()",
]
# Back to 0018: the tables go with their triggers and checks; the function stays otherwise.
UNPROTECT = [
    "DROP TRIGGER IF EXISTS gameplan_versions_append_only ON gameplan_versions",
    "DROP TRIGGER IF EXISTS gameplan_changes_append_only ON gameplan_changes",
    "DROP FUNCTION IF EXISTS worms_append_only()",
]


def protect(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        for statement in PROTECT:
            schema_editor.execute(statement)


def unprotect(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        for statement in UNPROTECT:
            schema_editor.execute(statement)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0018_gameplan_description'),
    ]

    operations = [
        migrations.CreateModel(
            name='GameplanVersion',
            fields=[
                ('sha256', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('filename', models.TextField()),
                ('content', models.BinaryField()),
                ('size', models.PositiveIntegerField()),
                ('description', models.TextField(blank=True, default='')),
                ('first_seen_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                ('recorded_by', models.TextField(db_default='', editable=False)),
            ],
            options={
                'db_table': 'gameplan_versions',
                'ordering': ['first_seen_at', 'sha256'],
            },
        ),
        migrations.RemoveField(
            model_name='gameplanfile',
            name='description',
        ),
        migrations.CreateModel(
            name='GameplanChange',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('filename', models.TextField()),
                ('seen_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                ('recorded_by', models.TextField(db_default='', editable=False)),
                ('version', models.ForeignKey(db_column='sha256', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='changes', to='core.gameplanversion')),
            ],
            options={
                'db_table': 'gameplan_changes',
                'ordering': ['seen_at', 'id'],
            },
        ),
        migrations.AddField(
            model_name='gameplanfile',
            name='version',
            field=models.ForeignKey(blank=True, db_column='sha256', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='files', to='core.gameplanversion'),
        ),
        migrations.AddField(
            model_name='roundrun',
            name='gameplan_version',
            field=models.ForeignKey(blank=True, db_column='gameplan_version', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='runs', to='core.gameplanversion'),
        ),
        migrations.AddConstraint(
            model_name='roundrun',
            constraint=models.CheckConstraint(condition=models.Q(('gameplan_version__isnull', True), ('gameplan_version', models.F('gameplan_sha256')), _connector='OR'), name='run_gameplan_version_is_its_sha256'),
        ),
        migrations.AddIndex(
            model_name='gameplanchange',
            index=models.Index(fields=['filename', 'seen_at'], name='gameplan_ch_filenam_7ec807_idx'),
        ),
        migrations.RunPython(protect, unprotect),
    ]
