# A round shuts every devbox down while its attempts run unless staff turn it off
# (rounds.devboxes_off, default and database default now true), and so does every round that
# has not begun (devbox_hold "": the conductor has not recorded it yet). Two host job kinds,
# preflight (before each attempt) and observe (while one runs): choices only, no SQL.

from django.db import migrations, models


def hold_in_rounds_to_come(apps, schema_editor):
    rounds = apps.get_model("core", "Round").objects.using(schema_editor.connection.alias)
    rounds.filter(devbox_hold="").update(devboxes_off=True)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_round_on_hold'),
    ]

    operations = [
        migrations.AlterField(
            model_name='hostjob',
            name='kind',
            field=models.TextField(choices=[('gameplans', 'Gameplans'), ('preflight', 'Preflight'), ('prepare', 'Prepare'), ('check', 'Check'), ('deploy', 'Deploy'), ('remove_flags', 'Remove Flags'), ('place_flags', 'Place Flags'), ('write_file', 'Write File'), ('read_file', 'Read File'), ('observe', 'Observe'), ('teardown', 'Teardown'), ('devbox_provision', 'Devbox Provision'), ('devbox_rebuild', 'Devbox Rebuild'), ('devbox_reconfigure', 'Devbox Reconfigure'), ('devbox_deploy', 'Devbox Deploy'), ('hold_devboxes', 'Hold Devboxes'), ('release_devboxes', 'Release Devboxes')]),
        ),
        migrations.AlterField(
            model_name='round',
            name='devboxes_off',
            field=models.BooleanField(db_default=True, default=True),
        ),
        # Undone, the rounds keep what they have: the old default does not say what staff chose.
        migrations.RunPython(hold_in_rounds_to_come, migrations.RunPython.noop),
    ]
