from datetime import timedelta

from django.utils import timezone

from core import clock
from core.models import User
from core.models.competition import ApplicationSetting, Competition
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Create root user"

    def add_arguments(self, parser):
        pass

    def handle(self, *args, **options):
        User.objects.create_superuser(
            username="root",
            password="root",
        ).save()

        assert (
            not ApplicationSetting.objects.exists()
        ), "ApplicationSetting is already initialized."

        with transaction.atomic():
            print("Initialize competition")
            print("----------------------")

            name = input("Competition name: ").strip()
            if not name:
                raise ValueError("Competition name cannot be empty.")

            starts_at = clock.now()
            ends_at = starts_at + timedelta(weeks=1)

            if timezone.is_naive(starts_at):
                starts_at = timezone.make_aware(starts_at)

            if timezone.is_naive(ends_at):
                ends_at = timezone.make_aware(ends_at)

            if ends_at <= starts_at:
                raise ValueError("Registration end must be after registration start.")

            competition = Competition.objects.create(
                name=name,
                registration_starts_at=starts_at,
                registration_ends_at=ends_at,
            )

            ApplicationSetting.objects.create(
                key="current",
                current_competition=competition,
            )

            print(f"Initialized competition: {competition.name}")
