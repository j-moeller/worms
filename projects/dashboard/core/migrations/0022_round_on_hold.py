"""Round.on_hold: the operator's pause of a round (`worms pause round`), which the conductor honours."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0021_notifications")]

    operations = [
        migrations.AddField(
            model_name="round",
            name="on_hold",
            field=models.BooleanField(default=False, db_default=False),
        ),
    ]
