import base64
import hashlib

from django.db import migrations


def canonical_fingerprints(apps, schema_editor):
    Key = apps.get_model('core', 'DevboxSSHKey')
    alias = schema_editor.connection.alias
    updates, seen = [], {}
    for key in Key.objects.using(alias).all().iterator():
        key_type, encoded = key.public_key.split()[:2]
        canonical = base64.b64encode(base64.b64decode(encoded, validate=True)).decode('ascii')
        fingerprint = hashlib.sha256(f'{key_type} {canonical}'.encode()).hexdigest()
        if fingerprint in seen:
            raise ValueError(
                f'SSH keys {seen[fingerprint]} and {key.pk} have the same identity; '
                'resolve the duplicate before migrating. No keys have been removed.'
            )
        seen[fingerprint] = key.pk
        updates.append((key.pk, fingerprint))
    for pk, fingerprint in updates:
        Key.objects.using(alias).filter(pk=pk).update(key_fingerprint=fingerprint)


def original_fingerprints(apps, schema_editor):
    Key = apps.get_model('core', 'DevboxSSHKey')
    alias = schema_editor.connection.alias
    for key in Key.objects.using(alias).all().iterator():
        fingerprint = hashlib.sha256(' '.join(key.public_key.split()[:2]).encode()).hexdigest()
        Key.objects.using(alias).filter(pk=key.pk).update(key_fingerprint=fingerprint)


class Migration(migrations.Migration):
    dependencies = [('core', '0005_case_insensitive_accounts_and_teams')]
    operations = [migrations.RunPython(canonical_fingerprints, original_fingerprints)]
