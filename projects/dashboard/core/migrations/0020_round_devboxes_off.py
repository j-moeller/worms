# A round can shut every devbox down while its attempts run (rounds.devboxes_off, staff's choice
# until its start), and the conductor records what it does about it (rounds.devbox_hold); two host
# job kinds do it (hold_devboxes, release_devboxes: choices only, no SQL). Both columns have a
# database default: existing rounds hold nothing.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_gameplan_versions'),
    ]

    operations = [
        migrations.AddField(
            model_name='round',
            name='devbox_hold',
            field=models.TextField(blank=True, db_default='', default=''),
        ),
        migrations.AddField(
            model_name='round',
            name='devboxes_off',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AlterField(
            model_name='hostjob',
            name='kind',
            field=models.TextField(choices=[('gameplans', 'Gameplans'), ('prepare', 'Prepare'), ('check', 'Check'), ('deploy', 'Deploy'), ('remove_flags', 'Remove Flags'), ('place_flags', 'Place Flags'), ('write_file', 'Write File'), ('read_file', 'Read File'), ('teardown', 'Teardown'), ('devbox_provision', 'Devbox Provision'), ('devbox_rebuild', 'Devbox Rebuild'), ('devbox_reconfigure', 'Devbox Reconfigure'), ('devbox_deploy', 'Devbox Deploy'), ('hold_devboxes', 'Hold Devboxes'), ('release_devboxes', 'Release Devboxes')]),
        ),
    ]
