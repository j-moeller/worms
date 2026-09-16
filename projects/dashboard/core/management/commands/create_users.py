from django.core.management.base import BaseCommand
from django.db import transaction
from core.models import User
import csv


class Command(BaseCommand):
    help = "Imports users from a CSV file"

    def add_arguments(self, parser):
        parser.add_argument("csv_file", type=str)

    def handle(self, csv_file, *args, **options):
        with transaction.atomic():
            with open(csv_file) as f:
                reader = csv.DictReader(f)

                for line in reader:
                    first_name = line["Vorname"]
                    last_name = line["Nachname"]
                    email = line["E-Mail-Adresse"]

                    user = User(
                        username=email,
                        first_name=first_name,
                        last_name=last_name,
                        email=email,
                        is_staff=False,
                        is_active=True,
                    )
                    user.save()
