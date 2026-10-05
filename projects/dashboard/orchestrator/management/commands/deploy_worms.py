import random

from core.models.competition import Round
from core.models.team import Team
from core.models.vm import VirtualMachine, ProductionVirtualMachine
from core.models.worms import Upload, WormDeployment, WormDeploymentPlan
from core import clock
from dashboard.views.decorators import get_current_competition
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import OuterRef, Subquery


class Command(BaseCommand):
    help = "Deploy worms"

    def add_arguments(self, parser):
        parser.add_argument("--seed", type=int, default=1)

    def handle(self, seed, *args, **options):
        rng = random.Random(seed)

        competition = get_current_competition(None)

        now = clock.now()

        try:
            round = Round.objects.get(
                competition=competition, round_start__lt=now, round_end__gt=now
            )
        except Round.DoesNotExist:
            print(now)
            print(
                [
                    (round.is_active(now))
                    for round in Round.objects.filter(competition=competition)
                ]
            )
            print("No active round")
            return

        latest_upload = Upload.objects.filter(
            user__team_memberships__team=OuterRef("pk"),
            created_at__lt=round.round_start,
        ).order_by("-created_at", "-id")

        teams = (
            Team.objects.filter(competition=round.competition)
            .exclude(worm_deployment_plans__round=round)
            .annotate(
                latest_upload_id=Subquery(latest_upload.values("id")[:1]),
            )
        )

        virtual_machines = VirtualMachine.objects.filter(
            virtual_machine__round=round,
            worm_deployment_plans__isnull=True,
        )
        virtual_machine_ids = list(range(len(virtual_machines)))
        rng.shuffle(virtual_machine_ids)

        print("Virtual machine ids: ", len(virtual_machine_ids))

        new_deployment_plans = []
        new_deployments = []

        with transaction.atomic():
            existing_plans = {
                plan.team_id: plan
                for plan in WormDeploymentPlan.objects.filter(round=round)
            }

            existing_deployments = {
                deployment.worm_deployment_plan_id: deployment
                for deployment in WormDeployment.objects.filter(
                    worm_deployment_plan__round=round
                )
            }

            print("Existing plan: ", existing_plans)
            print("Existing depl: ", existing_deployments)

            for team in teams:
                if team.latest_upload_id is None:
                    print("Skip team: ", team.name, team.id)
                    continue

                upload = Upload.objects.get(pk=team.latest_upload_id)

                worm_deployment_plan = existing_plans.get(team.id)

                if worm_deployment_plan is None:
                    prod_vm_id = virtual_machine_ids.pop()
                    virtual_machine = virtual_machines[prod_vm_id]

                    assert round == virtual_machine.round

                    worm_deployment_plan = WormDeploymentPlan(
                        round=round,
                        team=team,
                        upload=upload,
                        virtual_machine=virtual_machine,
                    )

                    new_deployment_plans.append(worm_deployment_plan)

                # Deployment creation is deferred until after the plans have
                # been bulk-created.
                if worm_deployment_plan.pk is None:
                    new_deployments.append(worm_deployment_plan)

                elif worm_deployment_plan.pk not in existing_deployments:
                    new_deployments.append(worm_deployment_plan)

            print(new_deployment_plans)

            # Create all new plans in one query.
            if False:
                WormDeploymentPlan.objects.bulk_create(new_deployment_plans)

                # Convert plans into deployment objects.
                new_deployments = [
                    WormDeployment(worm_deployment_plan=plan)
                    for plan in new_deployments
                ]

                # Create all new deployments in one query.
                WormDeployment.objects.bulk_create(new_deployments)

            print("END ROUND EARLY")
            assert False
