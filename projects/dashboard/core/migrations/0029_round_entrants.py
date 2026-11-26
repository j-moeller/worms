# A round's entrants (orchestrator/rounds/planner.py): the teams that take part, by id; null, every
# team of its competition (every round so far). `worms round dev-test` names its own teams.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0028_round_session_superseded'),
    ]

    operations = [
        migrations.AddField(
            model_name='round',
            name='entrants',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
