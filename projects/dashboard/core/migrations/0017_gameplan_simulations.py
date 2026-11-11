# A gameplan's simulated attempt, kept for staff's Simulations page (orchestrator/rounds/simulations.py).

import django.db.models.functions.datetime
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_hostjob_read_file'),
    ]

    operations = [
        migrations.CreateModel(
            name='GameplanSimulation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('filename', models.TextField()),
                ('sha256', models.TextField()),
                ('duration', models.PositiveIntegerField()),
                ('version', models.PositiveIntegerField()),
                ('document', models.JSONField()),
                ('created_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
            ],
            options={
                'db_table': 'gameplan_simulations',
                'constraints': [models.UniqueConstraint(fields=('filename', 'sha256', 'duration', 'version'), name='unique_gameplan_simulation')],
            },
        ),
    ]
