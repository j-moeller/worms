# Every existing round gets its uuid (0010_round_uuid).

import uuid

from django.db import migrations


def fill(apps, schema_editor):
    rounds = apps.get_model("core", "Round").objects.using(schema_editor.connection.alias)
    for pk in rounds.filter(uuid__isnull=True).values_list("pk", flat=True):
        rounds.filter(pk=pk).update(uuid=uuid.uuid4())


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_round_uuid'),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
