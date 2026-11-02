"""A devbox SSH key's identity (type + base64 blob) is unique across every
team: the same public key may belong to only one team (docs/security-assessment.md,
D7). Add the fingerprint column, backfill it, drop any cross-team/exact
duplicates (keeping the earliest), then add the UNIQUE constraint.
"""

import hashlib

from django.db import migrations, models


def _fingerprint(public_key: str) -> str:
    return hashlib.sha256(" ".join((public_key or "").split()[:2]).encode()).hexdigest()


def backfill(apps, schema_editor):
    Key = apps.get_model("core", "DevboxSSHKey")
    seen = set()
    # Oldest first: a duplicate registered later is the one removed.
    for key in Key.objects.order_by("id").iterator():
        fp = _fingerprint(key.public_key)
        if fp in seen:
            key.delete()  # a redundant or cross-team-duplicate key
            continue
        seen.add(fp)
        key.key_fingerprint = fp
        key.save(update_fields=["key_fingerprint"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_upload_team"),
    ]

    operations = [
        migrations.AddField(
            model_name="devboxsshkey",
            name="key_fingerprint",
            field=models.CharField(default="", editable=False, max_length=64),
        ),
        migrations.RunPython(backfill, noop),
        migrations.AddConstraint(
            model_name="devboxsshkey",
            constraint=models.UniqueConstraint(fields=["key_fingerprint"], name="unique_devbox_ssh_key_fingerprint"),
        ),
    ]
