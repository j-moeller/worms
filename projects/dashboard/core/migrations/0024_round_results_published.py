"""Round.results_published_at: a round's results are shown to the teams once the operator publishes
them (`worms round publish`). The rounds that have a finished attempt already stay as they were:
published."""

from django.db import migrations, models
from django.db.models.functions import Now


def published_as_before(apps, schema_editor):
    # On the migrating connection (the migrator's): another would wait for this migration's own lock.
    rounds = apps.get_model("core", "Round").objects.using(schema_editor.connection.alias)
    rounds.filter(runs__state="finished").distinct().update(results_published_at=Now())


class Migration(migrations.Migration):
    dependencies = [("core", "0023_round_safety")]

    operations = [
        migrations.AddField(
            model_name="round",
            name="results_published_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.RunPython(published_as_before, migrations.RunPython.noop),
    ]
