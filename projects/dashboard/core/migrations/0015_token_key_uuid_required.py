# An access token's and a devbox SSH key's uuid are required and unique
# (0013_token_key_uuid, 0014_token_key_uuid_data).

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_token_key_uuid_data'),
    ]

    operations = [
        migrations.AlterField(
            model_name='accesstokens',
            name='uuid',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name='devboxsshkey',
            name='uuid',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
