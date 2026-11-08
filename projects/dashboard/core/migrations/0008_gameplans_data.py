# The data of the old round parameters that has a successor (0007_gameplans).

from django.db import migrations


def keep_durations_and_entry_vms(apps, schema_editor):
    """What the old parameters held that still means something: a round's run length becomes
    its duration per team, a run's start VM its entry point. The sandbox generator's and the
    old game plan's parameters have no successor (a gameplan file replaces them)."""
    Round = apps.get_model('core', 'Round')
    RoundRun = apps.get_model('core', 'RoundRun')
    alias = schema_editor.connection.alias

    def run_length(params):
        value = ((params or {}).get('timing') or {}).get('run_duration')
        return value if isinstance(value, int) and not isinstance(value, bool) and 60 <= value <= 7 * 86400 else None

    for round_ in Round.objects.using(alias).all().iterator():
        length = run_length(round_.sandbox_params)
        if length:
            round_.team_duration = length
            round_.save(update_fields=['team_duration'])
    for run in RoundRun.objects.using(alias).all().iterator():
        params = run.sandbox_params or {}
        run.entrypoint = str(params.get('start_vm') or '')
        run.duration = run_length(params) or 3600
        run.save(update_fields=['entrypoint', 'duration'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_gameplans'),
    ]

    operations = [
        migrations.RunPython(keep_durations_and_entry_vms, migrations.RunPython.noop),
    ]
