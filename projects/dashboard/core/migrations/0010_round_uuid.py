# A round is named by a uuid in URLs and the team API, not by its primary key. Added empty
# here, filled in 0011, required and unique in 0012: on PostgreSQL a table cannot be altered
# in the transaction that updated its rows (pending trigger events).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_gameplans_cleanup'),
    ]

    operations = [
        migrations.AddField(
            model_name='round',
            name='uuid',
            field=models.UUIDField(editable=False, null=True),
        ),
    ]
