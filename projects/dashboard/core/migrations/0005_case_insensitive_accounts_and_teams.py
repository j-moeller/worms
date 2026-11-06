from django.db import migrations, models
from django.db.models.functions import Lower


class Migration(migrations.Migration):
    dependencies = [("core", "0004_event_tables")]

    operations = [
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(Lower("username"), name="users_username_case_insensitive"),
        ),
        migrations.AddConstraint(
            model_name="team",
            constraint=models.UniqueConstraint(Lower("name"), "competition", name="teams_name_case_insensitive"),
        ),
    ]
