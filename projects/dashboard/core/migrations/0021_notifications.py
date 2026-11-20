# Notifications from staff to the teams (core/models/notification.py): the bell in the header and
# its page; staff manage them on /staff/notifications/. One table for them, one for the teams a
# notification names (audience "teams"). The dashboard's grants on both: setup_db_permissions.

import core.clock
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0020_round_devboxes_off'),
    ]

    operations = [
        migrations.CreateModel(
            name='Notification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('severity', models.TextField(choices=[('info', 'Info'), ('warning', 'Warning'), ('urgent', 'Urgent'), ('error', 'Error')], default='info')),
                ('message', models.TextField()),
                ('active', models.BooleanField(default=True)),
                ('audience', models.TextField(choices=[('all', 'Every team'), ('teams', 'Selected teams')], default='all')),
                ('created_at', models.DateTimeField(default=core.clock.now)),
                ('updated_at', models.DateTimeField(default=core.clock.now)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('teams', models.ManyToManyField(blank=True, related_name='notifications', to='core.team')),
            ],
            options={
                'db_table': 'notifications',
                'indexes': [models.Index(fields=['active', 'created_at'], name='notifications_active_created')],
            },
        ),
    ]
