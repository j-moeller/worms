# A gameplan's description for teams (Gameplan.description), shown on the dashboard's round page.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_gameplan_simulations'),
    ]

    operations = [
        migrations.AddField(
            model_name='gameplanfile',
            name='description',
            field=models.TextField(blank=True, db_default='', default=''),
        ),
    ]
