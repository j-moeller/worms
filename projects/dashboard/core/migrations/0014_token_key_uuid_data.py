# Every existing access token and devbox SSH key gets its uuid (0013_token_key_uuid).

import uuid

from django.db import migrations


def fill(apps, schema_editor):
    for model in ("AccessTokens", "DevboxSSHKey"):
        rows = apps.get_model("core", model).objects.using(schema_editor.connection.alias)
        for pk in rows.filter(uuid__isnull=True).values_list("pk", flat=True):
            rows.filter(pk=pk).update(uuid=uuid.uuid4())


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0013_token_key_uuid'),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
