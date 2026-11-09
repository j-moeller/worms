# A round's uuid is required and unique (0010_round_uuid, 0011_round_uuid_data).

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_round_uuid_data'),
    ]

    operations = [
        migrations.AlterField(
            model_name='round',
            name='uuid',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
