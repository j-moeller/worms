from django.core.management.base import BaseCommand
from core.models import (
    Competition,
    ApplicationSetting,
    Round,
    Team,
    User,
    Student,
    VirtualMachine,
    FlagCaptureAttempt,
    FlagCapture,
    LoggingAttempt,
    Logging,
    MalwareArtifact,
    Upload,
    WormDeployment,
)


class Command(BaseCommand):
    help = "Dump database"

    def add_arguments(self, parser):
        pass

    def handle(self, *args, **options):
        print("Competition:")
        for a in Competition.objects.all():
            print(a)
        print()
        print("ApplicationSetting:")
        for a in ApplicationSetting.objects.all():
            print(a)
        print()
        print("Round:")
        for a in Round.objects.all():
            print(a)
        print()
        print("Team:")
        for a in Team.objects.all():
            print(a)
        print()
        print("User:")
        for a in User.objects.all():
            print(a)
        print()
        print("Student:")
        for a in Student.objects.all():
            print(a)
        print()
        print("VirtualMachine:")
        for a in VirtualMachine.objects.all():
            print(a)
        print()
        print("FlagCaptureAttempt:")
        for a in FlagCaptureAttempt.objects.all():
            print(a)
        print()
        print("FlagCapture:")
        for a in FlagCapture.objects.all():
            print(a)
        print()
        print("LoggingAttempt:")
        for a in LoggingAttempt.objects.all():
            print(a)
        print()
        print("Logging:")
        for a in Logging.objects.all():
            print(a)
        print()
        print("MalwareArtifact:")
        for a in MalwareArtifact.objects.all():
            print(a)
        print()
        print("Upload:")
        for a in Upload.objects.all():
            print(a)
        print()
        print("WormDeployment:")
        for a in WormDeployment.objects.all():
            print(a)
        print()
