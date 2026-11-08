# What gameplans replace: the generator and game plan parameters, the step table; one
# attempt per team and round becomes several (0007_gameplans).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_gameplans_data'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='gameplanstep',
            name='unique_step_position',
        ),
        migrations.RemoveConstraint(
            model_name='roundrun',
            name='unique_run_per_round_team',
        ),
        migrations.RemoveConstraint(
            model_name='roundrun',
            name='unique_run_position',
        ),
        migrations.RemoveField(
            model_name='round',
            name='sandbox_params',
        ),
        migrations.AddConstraint(
            model_name='roundrun',
            constraint=models.UniqueConstraint(fields=('round', 'team', 'attempt'), name='unique_run_attempt_per_round_team'),
        ),
        migrations.AddConstraint(
            model_name='roundrun',
            constraint=models.UniqueConstraint(fields=('round', 'position', 'attempt'), name='unique_run_position_attempt'),
        ),
        migrations.RemoveField(
            model_name='roundrun',
            name='sandbox_definition',
        ),
        migrations.RemoveField(
            model_name='roundrun',
            name='sandbox_params',
        ),
        migrations.DeleteModel(
            name='GamePlanStep',
        ),
    ]
