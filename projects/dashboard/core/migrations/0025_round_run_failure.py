"""RoundRun.failure: why an attempt failed, for the staff round page: the phase, the host step and
its log on the host, the cause and the excerpt of the output that says it (`worms round start`
writes it; scripts/round_agent/failure.py)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0024_round_results_published")]

    operations = [
        migrations.AddField(
            model_name="roundrun",
            name="failure",
            field=models.JSONField(default=dict, blank=True),
        ),
    ]
