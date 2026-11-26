# A round's note for staff (the rounds panel, dashboard/views/staff_rounds.py round_note): never
# shown to a team. db_default too: rounds inserted with raw SQL (the simulation) get it empty.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0026_hostjob_build_steps'),
    ]

    operations = [
        migrations.AddField(
            model_name='round',
            name='note',
            field=models.TextField(blank=True, db_default='', default=''),
        ),
    ]
