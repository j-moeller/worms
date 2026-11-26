# Rounds editable between sessions (dashboard/views/staff_rounds.py, src/worms_cli/rounds.py): a
# running `worms round start` marks its round (session_seen_at, a heartbeat), and an attempt the
# round's later change made obsolete is kept as `superseded` (choices only, no SQL).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0027_round_note'),
    ]

    operations = [
        migrations.AddField(
            model_name='round',
            name='session_seen_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='roundrun',
            name='state',
            field=models.TextField(choices=[('pending', 'Pending'), ('preparing', 'Preparing'), ('checking', 'Checking'), ('running', 'Running'), ('ending', 'Ending'), ('tearing_down', 'Tearing Down'), ('finished', 'Finished'), ('failed', 'Failed'), ('skipped', 'Skipped'), ('superseded', 'Superseded')], default='pending'),
        ),
    ]
