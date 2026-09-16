from django.core.management.base import BaseCommand
from core.models import Competition, ApplicationSetting, User, VirtualMachine, Round


class Command(BaseCommand):
    help = "Closes the specified poll for voting"

    def add_arguments(self, parser):
        pass

    def handle(self, *args, **options):
        try:
            setting = ApplicationSetting.objects.select_related(
                "current_competition"
            ).get(key="current")
            current_competition = setting.current_competition
        except ApplicationSetting.DoesNotExist:
            current_competition = None

        print("Competitions:")
        for i, competition in enumerate(Competition.objects.all()):
            print(f" - [{i}]", competition.name, end="")
            if current_competition == competition:
                print(" <--", end="")
            print()
            print("   -", "Users")
            for user in User.objects.all().filter(team__competition=competition):
                print("      -", user)
            print(
                "   -",
                len(
                    VirtualMachine.objects.all().filter(round__competition=competition)
                ),
                "VMs",
            )
