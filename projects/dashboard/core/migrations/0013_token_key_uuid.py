# Access tokens and devbox SSH keys are named by a uuid in URLs (revoking, removing), not by
# their primary key. Added empty here, filled in 0014, required and unique in 0015: on
# PostgreSQL a table cannot be altered in the transaction that updated its rows (pending
# trigger events). The same steps as a round's uuid (0010-0012).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_round_uuid_required'),
    ]

    operations = [
        migrations.AddField(
            model_name='accesstokens',
            name='uuid',
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name='devboxsshkey',
            name='uuid',
            field=models.UUIDField(editable=False, null=True),
        ),
    ]
