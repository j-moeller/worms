"""Drop the worm deployment tables (models WormDeploymentPlan and
WormDeployment, removed: nothing but the removed demo-data generator ever
wrote them; which upload a team ran in a round is RoundRun.upload).

The squashed 0001 does not create them, so on a fresh database this does
nothing; on a database created before the squash it drops them."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0001_squashed_0013_team_api_indexes")]

    operations = [
        # One table per statement: SQLite's DROP TABLE takes a single table,
        # so a combined "DROP TABLE a, b" (valid on PostgreSQL) fails there.
        migrations.RunSQL(
            [
                "DROP TABLE IF EXISTS worm_deployments",
                "DROP TABLE IF EXISTS worm_deployment_plans",
            ],
            migrations.RunSQL.noop,
        ),
    ]
