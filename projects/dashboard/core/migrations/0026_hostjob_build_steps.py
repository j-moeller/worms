# A round's sandbox is built once, before its first attempt, in steps of their own (`worms round
# start`): build (OpenTofu's apply, the configuration), snapshot, a rollback before every attempt
# but the first after it, and at the round's end delete_snapshot before the teardown. Host job
# kinds: choices only, no SQL.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0025_round_run_failure'),
    ]

    operations = [
        migrations.AlterField(
            model_name='hostjob',
            name='kind',
            field=models.TextField(choices=[('gameplans', 'Gameplans'), ('preflight', 'Preflight'), ('build', 'Build'), ('snapshot', 'Snapshot'), ('rollback', 'Rollback'), ('prepare', 'Prepare'), ('check', 'Check'), ('deploy', 'Deploy'), ('remove_flags', 'Remove Flags'), ('place_flags', 'Place Flags'), ('write_file', 'Write File'), ('read_file', 'Read File'), ('observe', 'Observe'), ('delete_snapshot', 'Delete Snapshot'), ('teardown', 'Teardown'), ('devbox_provision', 'Devbox Provision'), ('devbox_rebuild', 'Devbox Rebuild'), ('devbox_reconfigure', 'Devbox Reconfigure'), ('devbox_deploy', 'Devbox Deploy'), ('hold_devboxes', 'Hold Devboxes'), ('release_devboxes', 'Release Devboxes')]),
        ),
    ]
