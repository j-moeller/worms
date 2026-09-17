from core.views.decorators import get_current_competition
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone


class Command(BaseCommand):
    help = "Close registration"

    def add_arguments(self, parser):
        pass

    def handle(self, *args, **options):
        # No competition state

        # Before competition registration

        # During competition registration

        # After competition registration, no rounds

        # After competition registration, next round about to start

        # During round

        # After round, next round about to start

        # After round, no next round

        with transaction.atomic():
            competition = get_current_competition()
            competition.registration_ends_at = timezone.now()
            competition.save()
