# A gameplan's execute() asks whether a file on a VM changed (vm.read_file): a new host job.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_token_key_uuid_required'),
    ]

    operations = [
        migrations.AlterField(
            model_name='hostjob',
            name='kind',
            field=models.TextField(choices=[('gameplans', 'Gameplans'), ('prepare', 'Prepare'), ('check', 'Check'), ('deploy', 'Deploy'), ('remove_flags', 'Remove Flags'), ('place_flags', 'Place Flags'), ('write_file', 'Write File'), ('read_file', 'Read File'), ('teardown', 'Teardown'), ('devbox_provision', 'Devbox Provision'), ('devbox_rebuild', 'Devbox Rebuild'), ('devbox_reconfigure', 'Devbox Reconfigure'), ('devbox_deploy', 'Devbox Deploy')]),
        ),
    ]
